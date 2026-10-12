"""被动抓包 + 本地 HTTP 看板。

抓包与解码都在后台线程，HTTP 只读快照；收包回调只做入队，绝不在这里做解密或解析
——初始化时那次原生模拟要数秒，放在回调里会丢包。

默认绑定回环地址。看板不把任何密钥材料发给页面，也不写入运行产物。
"""

from __future__ import annotations

import contextlib
import csv
import io
import json
import os
import queue
import signal
import struct
import threading
import time
import traceback
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from .decoder import Bootstrap, Engine, State

#: 反压队列：满了计入丢包，不阻塞抓包回调
QUEUE_LIMIT = 20000

#: 抓包过滤器。游戏主连接实测用过 30000 与 40000
CAPTURE_FILTER = "tcp port 30000 or tcp port 40000"

DASHBOARD_HTML = Path(__file__).with_name("dashboard.html")


def pick_interface(wanted: str | None):
    """选择抓包网卡。

    显式指定时按网卡名或 IP 匹配，找不到就把可用项列出来报错——**不静默回退**，
    否则会安静地抓到一张没有游戏流量的网卡上。
    """

    from scapy.all import conf

    candidates = [
        iface
        for iface in conf.ifaces.values()
        if getattr(iface, "ip", None) and not str(iface.ip).startswith(("127.", "169.254."))
    ]
    if wanted:
        for iface in conf.ifaces.values():
            if wanted in (str(iface.name), str(getattr(iface, "ip", ""))):
                return iface
        available = ", ".join(f"{i.name}({i.ip})" for i in candidates)
        raise SystemExit(f"找不到网卡 {wanted!r}；可用：{available}")
    try:
        iface, _, _ = conf.route.route("223.5.5.5")  # 默认路由出口，通常就是游戏走的那张
        if getattr(iface, "ip", None) and not str(iface.ip).startswith(("127.", "169.254.")):
            return iface
    except Exception:
        pass
    if not candidates:
        raise SystemExit("找不到可用于抓包的网卡（已排除 127.* 与 169.254.*）")
    return candidates[0]


def find_ok_end_field(explicit: str | None) -> Path | None:
    """定位 ok-end-field 仓库（仅用于地图 WS 对照）。找不到就返回 None。"""

    if explicit:
        path = Path(explicit)
        return path if (path / "src" / "localization" / "ws_position_mixin.py").is_file() else None
    for candidate in Path(__file__).resolve().parents:
        for name in ("ok-end-field",):
            sibling = candidate / name
            if (sibling / "src" / "localization" / "ws_position_mixin.py").is_file():
                return sibling
    return None


def truncate_pcap_prefix(source: bytes) -> bytes:
    """把一份 pcap 截到"最后一个完整包"，用于 seed 重放（避免尾部半包）。"""

    endian = "<" if source[:4] in (b"\xd4\xc3\xb2\xa1", b"\x4d\x3c\xb2\xa1") else ">"
    offset = 24
    while offset + 16 <= len(source):
        length = struct.unpack_from(endian + "I", source, offset + 8)[0]
        if offset + 16 + length > len(source):
            break
        offset += 16 + length
    return source[:offset]


#: CSV 导出列顺序
CSV_FIELDS = [
    "time", "flow", "objectId", "scene", "x", "y", "z", "yaw", "speed", "state", "merged",
    "sampleId", "captureAt", "callbackAt", "processingStartedAt", "decodedAt",
    "decodeMs", "queueMs", "captureToDecodedMs", "replayed", "gameServerTime",
]


class DashboardHandler(BaseHTTPRequestHandler):
    """看板的 HTTP 端点。

    做成模块级类而不是闭包，是为了能被测试真的请求一遍——SSE 分支曾经因为一个方法名
    写错而在每次页面连接时抛异常，闭包写法下没法覆盖到。
    依赖通过 :func:`make_handler` 注入。
    """

    state: State
    run: Path
    record: Any
    stop: threading.Event
    quiet: bool = True

    def log_message(self, *args):
        if not self.quiet:
            super().log_message(*args)

    def _send(self, data: bytes, kind: str = "application/json; charset=utf-8", code: int = 200) -> None:
        self.send_response(code)
        self.send_header("Content-Type", kind)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        with contextlib.suppress(BrokenPipeError, ConnectionResetError):
            self.wfile.write(data)

    def _snapshot(self) -> dict:
        snapshot = self.state.snapshot()
        snapshot["runDirectory"] = str(self.run)
        snapshot["serverSentAt"] = time.time()
        return snapshot

    def _positions_csv(self) -> bytes:
        with self.state.lock:
            self.record.flush()
            data = (self.run / "positions.jsonl").read_text(encoding="utf-8")
        out = io.StringIO()
        writer = csv.DictWriter(out, fieldnames=CSV_FIELDS, extrasaction="ignore")
        writer.writeheader()
        for line in data.splitlines():
            if line.strip():
                writer.writerow(json.loads(line))
        # 带 BOM：Excel 打开中文列名不会乱码
        return ("﻿" + out.getvalue()).encode()

    def _event_stream(self) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        previous = None
        sent = 0.0
        try:
            while not self.stop.is_set():
                snapshot = self._snapshot()
                fingerprint = (
                    snapshot["leaderPositions"],
                    snapshot["ws"]["samples"],
                    snapshot["ws"]["status"],
                    snapshot["frames"] // 20,
                    snapshot["captureReady"],
                    snapshot["captureError"],
                )
                # 有变化立刻推，没变化每秒保活一次
                if fingerprint != previous or time.monotonic() - sent > 1:
                    payload = "data: " + json.dumps(snapshot, ensure_ascii=False) + "\n\n"
                    self.wfile.write(payload.encode())
                    self.wfile.flush()
                    previous = fingerprint
                    sent = time.monotonic()
                self.stop.wait(0.02)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            pass

    def do_GET(self):
        path = self.path.split("?")[0]
        if path == "/":
            self._send(DASHBOARD_HTML.read_bytes(), "text/html; charset=utf-8")
        elif path == "/api/state":
            self._send(json.dumps(self._snapshot(), ensure_ascii=False).encode())
        elif path == "/api/positions.csv":
            self._send(self._positions_csv(), "text/csv; charset=utf-8")
        elif path == "/api/events":
            self._event_stream()
        else:
            self._send(b'{"error":"not found"}', code=404)


def make_handler(state: State, run: Path, record, stop: threading.Event):
    """把运行期依赖绑到一个 handler 类上。"""

    return type(
        "BoundDashboardHandler",
        (DashboardHandler,),
        {"state": state, "run": run, "record": record, "stop": stop},
    )


def serve(args) -> int:
    from scapy.all import AsyncSniffer, PcapReader, PcapWriter

    from .cli import _default_game_directory

    game_directory = Path(args.game_dir) if args.game_dir else _default_game_directory()
    if game_directory is None:
        raise SystemExit("找不到客户端目录，请用 --game-dir 或 EF_GAME_DIR 指定")
    runs_root = Path(args.runs_dir) if args.runs_dir else Path("runs")
    run = runs_root / datetime.now().strftime("%Y%m%d-%H%M%S")
    run.mkdir(parents=True, exist_ok=False)

    record = (run / "positions.jsonl").open("a", encoding="utf-8")
    state = State(record)
    incoming: queue.Queue = queue.Queue(maxsize=QUEUE_LIMIT)
    stop = threading.Event()
    writer = PcapWriter(str(run / "capture.pcap"), sync=False)
    sniffer_holder: dict[str, object] = {}

    http = ThreadingHTTPServer((args.host, args.port), make_handler(state, run, record, stop))
    iface = pick_interface(args.interface)

    def callback(packet):
        try:
            incoming.put_nowait((packet, time.time()))
        except queue.Full:
            with state.lock:
                state.drops += 1

    def capture_ready():
        with state.lock:
            state.capture_ready = True
            state.status = "监听已开启，等待启动游戏"
        state.event("抓包已开启，现在可以启动游戏")

    def worker():
        try:
            state.event("校验客户端版本并准备解码器")
            bootstrap = Bootstrap(game_directory)
            engine = Engine(state, bootstrap)
            with state.lock:
                state.bootstrap_seconds = bootstrap.key_seconds
            state.event(
                f"客户端材料就绪（{bootstrap.key_seconds:.2f} s）；"
                "公钥模数会在收到登录响应时恢复"
            )
            if not args.no_map_ws:
                _start_map_ws(state, stop, run / "ws-positions.jsonl", args)
            with state.lock:
                state.interface = str(iface.ip)
            state.event(f"抓包网卡 {iface.name} ({iface.ip})")
            sniffer = AsyncSniffer(
                iface=iface, filter=CAPTURE_FILTER, prn=callback, store=False, started_callback=capture_ready
            )
            sniffer_holder["sniffer"] = sniffer
            sniffer.start()
            flushed = time.monotonic()
            if args.seed_pcap:
                # 先开新抓包，再重放旧捕获：这样接管一个正在跑的会话时不会漏掉新包。
                stop.wait(1.5)
                seed = run / "seed.pcap"
                seed.write_bytes(truncate_pcap_prefix(Path(args.seed_pcap).read_bytes()))
                with PcapReader(str(seed)) as reader:
                    for packet in reader:
                        engine.receive(packet, replayed=True)
                state.event("已从先前抓包恢复会话状态，继续接收新包（重放样本不计入在线延迟）")
            while not stop.is_set():
                if sniffer.exception:
                    raise sniffer.exception
                try:
                    packet, callback_at = incoming.get(timeout=0.25)
                except queue.Empty:
                    continue
                writer.write(packet)
                engine.receive(packet, callback_at=callback_at)
                if time.monotonic() - flushed > 1:
                    if writer.f:
                        writer.f.flush()
                    flushed = time.monotonic()
        except Exception as exc:
            with state.lock:
                state.capture_error = str(exc)[:250]
                state.status = "服务出现错误"
            state.event("错误：" + str(exc)[:200])
            (run / "error.log").write_text(traceback.format_exc(), encoding="utf-8")
        finally:
            sniffer = sniffer_holder.get("sniffer")
            if sniffer is not None and getattr(sniffer, "running", False):
                with contextlib.suppress(Exception):
                    sniffer.stop()
            writer.close()

    thread = threading.Thread(target=worker, name="capture-decoder", daemon=True)
    thread.start()
    (run / "service.json").write_text(
        json.dumps(
            {"pid": os.getpid(), "url": f"http://{args.host}:{args.port}", "runDirectory": str(run)},
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )

    def terminate(signum, frame):
        stop.set()
        threading.Thread(target=http.shutdown, daemon=True).start()

    signal.signal(signal.SIGINT, terminate)
    signal.signal(signal.SIGTERM, terminate)
    print(f"DASHBOARD_URL http://{args.host}:{args.port}", flush=True)
    print(f"运行产物目录 {run}", flush=True)
    try:
        http.serve_forever(poll_interval=0.25)
    finally:
        stop.set()
        thread.join(timeout=5)
        http.server_close()
        record.close()
    return 0


def _start_map_ws(state: State, stop: threading.Event, output: Path, args) -> None:
    """可选的官方地图 WS 对照。

    这一步需要 ok-end-field 仓库（复用它已经做好的 WS 认证），所以是可选依赖：
    找不到就明确说明并继续，不影响抓包解码本身。
    """

    repo = find_ok_end_field(args.ok_end_field)
    if repo is None:
        with state.lock:
            state.ws.update(status="未启用（未找到 ok-end-field 仓库）")
        state.event("未找到 ok-end-field 仓库，跳过地图 WS 对照；只输出抓包解出的坐标")
        return
    if not (repo / "configs" / "account_scoped_overrides.json").is_file():
        with state.lock:
            state.ws.update(status="未启用（ok-end-field 未配置账号）")
        state.event("ok-end-field 未配置账号 content，跳过地图 WS 对照")
        return
    import sys

    if str(repo) not in sys.path:
        sys.path.insert(0, str(repo))
    from .map_compare import resolve_account_content, start_comparison

    try:
        content = resolve_account_content(repo, args.account, args.content)
    except ImportError as exc:
        # 最常见的原因是本环境没装 ok-end-field 的依赖（ok-script）。说清楚该装什么，
        # 否则"未启用"看起来会像是本程序的缺陷。
        message = (
            f"地图 WS 对照需要 ok-end-field 的依赖（{exc}）。"
            "请改用 `uv sync --extra ws-compare` 安装，或用 --no-map-ws 明确关掉对照。"
        )
        with state.lock:
            state.ws.update(status="未启用（缺少依赖）", error=message)
        state.event(message)
        return
    except Exception as exc:
        with state.lock:
            state.ws.update(status="未启用", error=str(exc)[:160])
        state.event(f"地图 WS 对照未启用：{exc}")
        return
    start_comparison(state, stop, output, repo, content)


__all__ = ["CAPTURE_FILTER", "QUEUE_LIMIT", "find_ok_end_field", "pick_interface", "serve", "truncate_pcap_prefix"]
