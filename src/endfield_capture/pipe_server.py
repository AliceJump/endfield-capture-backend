"""把解码出的坐标推给同机的消费方（ok-end-field 等）——Windows 命名管道。

为什么用管道而不是 HTTP 轮询：消费方是 Qt 应用，``QLocalSocket`` 直接读管道，不用占端口、
不用轮询，也不会有"每次采样同步发一个 HTTP 请求"的阻塞。协议故意做得极简：

**换行分隔的 UTF-8 JSON，每行一个对象，带 ``type`` 字段。**

```
{"type": "hello", "protocol": 1, "pid": 1234, "pipe": "\\\\.\\pipe\\...", "runDirectory": "..."}
{"type": "position", "time": ..., "scene": 361, "x": ..., "y": ..., "z": ..., "yaw": ..., ...}
```

``position`` 行的字段就是 ``/api/state`` 里 ``lastPosition`` 那一份（含 ``levelId`` /
``levelName`` / ``mapId`` / ``mapName``），消费方换传输不用改解析。

- 连上先给 ``hello``，紧跟着把**当前最新坐标**补一条（有的话），然后才是实时流 ——
  不必等玩家动起来才拿得到位置；
- **没有客户端时不排队**：只留最新一条。否则断线期间攒下的旧位置会在重连瞬间冲出去，
  消费方会以为玩家刚才在瞬移；
- 反压：连着客户端但写得比读得快时，队列满了丢新样本并计数，**不阻塞解码线程**。

实现只用 ctypes 调 Win32 的 ``CreateNamedPipeW`` / ``ConnectNamedPipe`` / ``WriteFile``：
不新增依赖，也**不碰任何进程**（管道是文件对象，跟读内存无关）。
"""

from __future__ import annotations

import ctypes
import json
import os
import queue
import threading
from ctypes import wintypes
from typing import Any

__all__ = ["DEFAULT_PIPE_NAME", "PROTOCOL_VERSION", "PositionPipe", "encode_position"]

#: 默认管道名；消费方用它 ``QLocalSocket.connectToServer(...)``
DEFAULT_PIPE_NAME = r"\\.\pipe\endfield-capture-positions"

#: 流协议版本；改协议时 +1，消费方按它判断能不能解
PROTOCOL_VERSION = 1

_PIPE_ACCESS_DUPLEX = 0x00000003
_PIPE_TYPE_BYTE = 0x00000000
_PIPE_READMODE_BYTE = 0x00000000
_PIPE_WAIT = 0x00000000
_PIPE_UNLIMITED_INSTANCES = 255
_INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value
_ERROR_PIPE_CONNECTED = 535
_BUFFER_BYTES = 64 * 1024

_API: dict[str, Any] = {}


def _kernel32() -> dict[str, Any]:
    """惰性加载 kernel32：import 本模块不该在非 Windows 上炸，也让 ``--help`` 更快。"""

    if _API:
        return _API
    if os.name != "nt":  # pragma: no cover - 本项目只在 Windows 上跑
        raise RuntimeError("命名管道只在 Windows 上可用")
    lib = ctypes.WinDLL("kernel32", use_last_error=True)
    specs = {
        "create": (
            "CreateNamedPipeW",
            [
                wintypes.LPCWSTR,
                wintypes.DWORD,
                wintypes.DWORD,
                wintypes.DWORD,
                wintypes.DWORD,
                wintypes.DWORD,
                wintypes.DWORD,
                wintypes.LPVOID,
            ],
            wintypes.HANDLE,
        ),
        "connect": ("ConnectNamedPipe", [wintypes.HANDLE, wintypes.LPVOID], wintypes.BOOL),
        "write": (
            "WriteFile",
            [
                wintypes.HANDLE,
                wintypes.LPCVOID,
                wintypes.DWORD,
                ctypes.POINTER(wintypes.DWORD),
                wintypes.LPVOID,
            ],
            wintypes.BOOL,
        ),
        "flush": ("FlushFileBuffers", [wintypes.HANDLE], wintypes.BOOL),
        "peek": (
            "PeekNamedPipe",
            [
                wintypes.HANDLE,
                wintypes.LPVOID,
                wintypes.DWORD,
                ctypes.POINTER(wintypes.DWORD),
                ctypes.POINTER(wintypes.DWORD),
                ctypes.POINTER(wintypes.DWORD),
            ],
            wintypes.BOOL,
        ),
        "disconnect": ("DisconnectNamedPipe", [wintypes.HANDLE], wintypes.BOOL),
        "close": ("CloseHandle", [wintypes.HANDLE], wintypes.BOOL),
    }
    for key, (name, argtypes, restype) in specs.items():
        func = getattr(lib, name)
        func.argtypes = argtypes
        func.restype = restype
        _API[key] = func
    return _API


def encode_position(payload: dict[str, Any]) -> bytes:
    """一条坐标 → 一行 NDJSON。字段与 ``/api/state`` 的 ``lastPosition`` 一致。"""

    return (json.dumps({"type": "position", **payload}, ensure_ascii=False) + "\n").encode()


class PositionPipe:
    """命名管道服务端：单客户端、字节流、推坐标。

    :meth:`publish` 只入队、不做 IO，所以可以直接在解码线程里调。
    """

    def __init__(self, name: str = DEFAULT_PIPE_NAME, *, run_directory: str = "", capacity: int = 512):
        self.name = name
        self.run_directory = run_directory
        self._queue: queue.Queue[bytes] = queue.Queue(maxsize=capacity)
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._handle: int | None = None
        self._latest: dict[str, Any] | None = None
        self.published = 0
        self.dropped = 0
        self.connections = 0
        self.error: str | None = None

    # ------------------------------------------------------------------ #
    # 生命周期
    # ------------------------------------------------------------------ #
    def start(self) -> None:
        self._thread = threading.Thread(target=self._serve, name="position-pipe", daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 3.0) -> None:
        self._stop.set()
        # ConnectNamedPipe 正阻塞着等客户端；自己连一下把它顶醒，线程才好退
        try:
            with open(self.name, "r+b", buffering=0):
                pass
        except OSError:
            pass
        if self._thread is not None:
            self._thread.join(timeout=timeout)

    def stats(self) -> dict[str, Any]:
        with self._lock:
            connected = self._handle is not None
        return {
            "pipe": self.name,
            "connected": connected,
            "published": self.published,
            "dropped": self.dropped,
            "connections": self.connections,
            "error": self.error,
        }

    # ------------------------------------------------------------------ #
    # 推送（解码线程调用）
    # ------------------------------------------------------------------ #
    def publish(self, payload: dict[str, Any]) -> None:
        """把一条坐标推给消费方。**不做 IO、不阻塞**。"""

        line = encode_position(payload)
        with self._lock:
            self._latest = payload
            connected = self._handle is not None
        if not connected:
            return  # 没客户端就不排队，免得重连时把积压冲出去
        try:
            self._queue.put_nowait(line)
        except queue.Full:
            self.dropped += 1

    # ------------------------------------------------------------------ #
    # 服务线程
    # ------------------------------------------------------------------ #
    def _hello(self) -> bytes:
        return (
            json.dumps(
                {
                    "type": "hello",
                    "protocol": PROTOCOL_VERSION,
                    "pid": os.getpid(),
                    "pipe": self.name,
                    "runDirectory": self.run_directory,
                },
                ensure_ascii=False,
            )
            + "\n"
        ).encode()

    def _write(self, handle: int, data: bytes) -> bool:
        api = _kernel32()
        written = wintypes.DWORD()
        if not api["write"](handle, data, len(data), ctypes.byref(written), None):
            return False
        api["flush"](handle)  # 让消费方立刻看到，而不是等缓冲满
        return written.value == len(data)

    def _alive(self, handle: int) -> bool:
        """客户端还在吗。写不出去要等下一次 publish 才发现，断线就得靠这个探测。"""

        return bool(_kernel32()["peek"](handle, None, 0, None, None, None))

    def _pump(self, handle: int) -> None:
        """给一个已连上的客户端发流，直到它断开或服务停止。"""

        with self._lock:
            latest = self._latest
        self._write(handle, self._hello())
        if latest is not None:
            self._write(handle, encode_position(latest))
        while not self._stop.is_set():
            try:
                line = self._queue.get(timeout=0.25)
            except queue.Empty:
                if not self._alive(handle):
                    return
                continue
            if not self._write(handle, line):
                return
            self.published += 1

    def _accept(self) -> bool:
        """建一个管道实例并等客户端；返回是否已连上。"""

        api = _kernel32()
        handle = api["create"](
            self.name,
            _PIPE_ACCESS_DUPLEX,
            _PIPE_TYPE_BYTE | _PIPE_READMODE_BYTE | _PIPE_WAIT,
            _PIPE_UNLIMITED_INSTANCES,
            _BUFFER_BYTES,
            _BUFFER_BYTES,
            0,
            None,
        )
        if handle == _INVALID_HANDLE_VALUE:
            self.error = f"CreateNamedPipe 失败（Win32 错误 {ctypes.get_last_error()}）"
            self._stop.wait(1.0)
            return False
        connected = bool(api["connect"](handle, None)) or ctypes.get_last_error() == _ERROR_PIPE_CONNECTED
        if not connected:
            api["close"](handle)
            return False
        with self._lock:
            self._handle = handle
            self.connections += 1
            while not self._queue.empty():  # 上一个客户端留下的积压丢掉
                try:
                    self._queue.get_nowait()
                except queue.Empty:
                    break
        return True

    def _release(self, handle: int) -> None:
        with self._lock:
            if self._handle == handle:
                self._handle = None
        api = _kernel32()
        api["disconnect"](handle)
        api["close"](handle)

    def _serve(self) -> None:
        while not self._stop.is_set():
            if not self._accept():
                continue
            with self._lock:
                handle = self._handle
            if handle is None:
                continue
            try:
                self._pump(handle)
            except OSError as exc:  # pragma: no cover - 只有在管道异常时才走到
                self.error = str(exc)[:200]
            finally:
                self._release(handle)
