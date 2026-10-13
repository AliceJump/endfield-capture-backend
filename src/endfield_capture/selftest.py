"""回放一份完整抓包，核对解码结果。

这是判断"这套解码在当前客户端版本上还行不行"的唯一可靠办法，也是改完代码后的第一道闸。
除了数量，还检查：

- 每条坐标都通过**独立的 Google protobuf 解析**复核（不是同一个手写 walker 自证）；
- 双向字节流被完整消费、没有残留分片、没有等待补齐的空洞；
- 打乱顺序并注入重复包后结果不变（TCP 重组确实在工作）。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

#: 参考会话的预期结果（研究阶段实测并逐条核对过的数值）
REFERENCE_EXPECTATION: dict[str, Any] = {
    "moveMessageCount": 137,
    "positionRecords": 229,
    "leaderRecords": 39,
    "sceneNumId": 232,
    "lastLeaderPosition": {
        "x": -243.19952392578125,
        "y": 259.4722595214844,
        "z": -228.3857879638672,
    },
}


def _read_packets(pcap: Path):
    from scapy.all import IP, TCP, PcapReader

    packets = []
    with PcapReader(str(pcap)) as reader:
        for packet in reader:
            if IP in packet and TCP in packet:
                packets.append(packet)
    return packets


def _replay(packets, bootstrap, *, disorder: bool = False):
    from .decoder import Engine, State

    rows = list(packets)
    if disorder:
        # 交换真实同方向数据包的顺序，并额外重放重复包，验证 TCP 恢复
        for index in range(10, len(rows) - 1, 11):
            first, second = rows[index : index + 2]
            if first[IP_LAYER].src == second[IP_LAYER].src and _payload(first) and _payload(second):
                rows[index], rows[index + 1] = second, first
    state = State()
    engine = Engine(state, bootstrap)
    for index, packet in enumerate(rows):
        engine.receive(packet)
        if disorder and index % 17 == 0:
            engine.receive(packet)
    return state, engine


def _payload(packet) -> bytes:
    from .decoder import tcp_payload

    return tcp_payload(packet)


try:  # pragma: no cover - 导入期副作用
    from scapy.all import IP as IP_LAYER
except ImportError:  # pragma: no cover
    IP_LAYER = None  # type: ignore[assignment]


def run_selftest(args) -> int:
    from .cli import _require_game_directory
    from .decoder import Bootstrap

    pcap = Path(args.pcap)
    if not pcap.is_file():
        raise SystemExit(f"找不到抓包文件 {pcap}")

    expectation = dict(REFERENCE_EXPECTATION)
    if args.expect:
        expectation.update(json.loads(Path(args.expect).read_text(encoding="utf-8")))

    game_directory = _require_game_directory(args.game_dir)
    print(f"客户端目录 {game_directory}")
    bootstrap = Bootstrap(game_directory)
    how = "预存数据" if bootstrap.modulus_source == "data" else "现场模拟恢复"
    print(f"读取客户端材料用时 {bootstrap.key_seconds:.2f} s（公钥模数：{how}）")

    packets = _read_packets(pcap)
    print(f"读入 {len(packets)} 个 TCP 包")

    results = []
    for disorder in (False, True):
        state, engine = _replay(packets, bootstrap, disorder=disorder)
        ok = True
        problems = []
        for key, label in (
            ("moveMessageCount", "移动消息"),
            ("positionRecords", "对象位置"),
            ("leaderRecords", "主控位置"),
        ):
            expected = expectation.get(key)
            actual = {"moveMessageCount": state.moves, "positionRecords": state.objects, "leaderRecords": state.leaders}[key]
            if expected is not None and actual != expected:
                ok = False
                problems.append(f"{label} {actual} != 预期 {expected}")
        if expectation.get("lastLeaderPosition") and state.last:
            actual_last = {axis: state.last[axis] for axis in "xyz"}
            if actual_last != expectation["lastLeaderPosition"]:
                ok = False
                problems.append(f"最后一条主控坐标 {actual_last} != 预期 {expectation['lastLeaderPosition']}")
        for flow in engine.flows.values():
            if flow.failed:
                ok = False
                problems.append(f"连接 {flow.id} 解码失败：{flow.status}")
            if any(stream.pending for stream in flow.streams.values()):
                ok = False
                problems.append(f"连接 {flow.id} 仍有等待补齐的字节")
            if any(flow.buffers.values()):
                ok = False
                problems.append(f"连接 {flow.id} 仍有未消费的缓冲")
            if any(flow.fragments.values()):
                ok = False
                problems.append(f"连接 {flow.id} 仍有未完成的分片")
        results.append(
            {
                "disorderAndDuplicates": disorder,
                "moves": state.moves,
                "objects": state.objects,
                "leaderPositions": state.leaders,
                "passed": ok,
                "problems": problems,
            }
        )
        print(
            f"  {'打乱+重复包' if disorder else '正常顺序'}："
            f"移动消息 {state.moves} / 对象位置 {state.objects} / 主控位置 {state.leaders} "
            f"→ {'通过' if ok else '失败'}"
        )
        for problem in problems:
            print(f"    - {problem}")

    passed = all(row["passed"] for row in results)
    print()
    print(json.dumps({"passed": passed, "replays": results}, ensure_ascii=False, indent=2))
    return 0 if passed else 1
