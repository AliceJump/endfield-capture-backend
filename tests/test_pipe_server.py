"""命名管道推送的契约测试。

消费方（ok-end-field）按这份契约读流，所以这里钉住四件事：连上先给 ``hello``、紧跟着补一条
当前坐标、之后是实时流、以及**没客户端时不排队**（重连不该收到一堆积压的旧位置）。

管道是文件对象，测试直接用 ``open(r"\\\\.\\pipe\\...", "r+b")`` 当客户端读——这也顺便
证明别的语言/Qt 能读。
"""

from __future__ import annotations

import json
import os
import struct
import time
import unittest

from endfield_capture.decoder import Flow, State
from endfield_capture.pipe_server import DEFAULT_PIPE_NAME, PROTOCOL_VERSION, PositionPipe, encode_position


def _varint(amount: int) -> bytes:
    out = bytearray()
    while True:
        byte = amount & 0x7F
        amount >>= 7
        out.append(byte | (0x80 if amount else 0))
        if not amount:
            return bytes(out)


def _field(number: int, payload: bytes) -> bytes:
    return _varint((number << 3) | 2) + _varint(len(payload)) + payload


def _vector(x: float, y: float, z: float) -> bytes:
    return b"".join(
        _varint((axis << 3) | 5) + struct.pack("<f", value)
        for axis, value in ((1, x), (2, y), (3, z))
    )


def _move_message(scene: int) -> bytes:
    """手工拼一条消息 81 的 body（与 test_decoder 的做法一致，只有一条 move_info）。"""

    motion = (
        _field(1, _vector(-243.2, 259.47, -228.39))
        + _field(2, _vector(0.0, 250.586, 0.0))
        + _field(3, _vector(0.0, 0.0, 0.0))
        + _varint((4 << 3) | 0)
        + _varint(3)
    )
    move = (
        _varint((1 << 3) | 0)
        + _varint(4628366647055237120)
        + _field(2, motion)
        + _varint((3 << 3) | 0)
        + _varint(0)
        + _varint((4 << 3) | 0)
        + _varint(scene)
    )
    return _field(1, move) + _varint((2 << 3) | 0) + _varint(0) + _varint((3 << 3) | 0) + _varint(0)


def _sample(x: float = 1.0, scene: int = 361) -> dict:
    return {
        "time": time.time(),
        "flow": 1,
        "objectId": "1",
        "scene": scene,
        "levelId": "map02_lv009",
        "levelName": "雪松林",
        "dungeonName": "",
        "mapId": "map02",
        "mapName": "武陵",
        "x": x,
        "y": 100.0,
        "z": -20.0,
        "yaw": 12.5,
        "speed": 3.0,
        "state": 3,
        "merged": False,
    }


def _connect(name: str, timeout: float = 5.0):
    """等管道实例建出来再连；返回可读写的文件对象。"""

    deadline = time.monotonic() + timeout
    last: OSError | None = None
    while time.monotonic() < deadline:
        try:
            return open(name, "r+b", buffering=0)
        except OSError as exc:  # 服务端还没 CreateNamedPipe
            last = exc
            time.sleep(0.02)
    raise AssertionError(f"连不上管道 {name}：{last}")


def _read_line(handle, timeout: float = 5.0) -> dict:
    """读一行 NDJSON；超时抛断言，避免测试挂死。"""

    deadline = time.monotonic() + timeout
    buffer = bytearray()
    while time.monotonic() < deadline:
        chunk = handle.read(1)
        if not chunk:
            time.sleep(0.005)
            continue
        if chunk == b"\n":
            return json.loads(bytes(buffer).decode("utf-8"))
        buffer += chunk
    raise AssertionError(f"读管道超时（已收到 {bytes(buffer)!r}）")


@unittest.skipUnless(os.name == "nt", "命名管道只在 Windows 上可用")
class PositionPipeTest(unittest.TestCase):
    def setUp(self):
        self.name = rf"\\.\pipe\endfield-capture-test-{os.getpid()}-{id(self)}"
        self.pipe = PositionPipe(self.name, run_directory="runs/test")
        self.pipe.start()

    def tearDown(self):
        self.pipe.stop()

    def test_hello_then_live_positions(self):
        with _connect(self.name) as client:
            hello = _read_line(client)
            self.assertEqual(hello["type"], "hello")
            self.assertEqual(hello["protocol"], PROTOCOL_VERSION)
            self.assertEqual(hello["pipe"], self.name)
            self.assertEqual(hello["runDirectory"], "runs/test")

            self.pipe.publish(_sample(x=11.0))
            first = _read_line(client)
            self.assertEqual(first["type"], "position")
            self.assertEqual(first["x"], 11.0)
            self.assertEqual(first["scene"], 361)
            self.assertEqual(first["levelId"], "map02_lv009")
            self.assertEqual(first["mapName"], "武陵")

            self.pipe.publish(_sample(x=12.0))
            self.assertEqual(_read_line(client)["x"], 12.0)

    def test_late_client_gets_the_latest_position_first(self):
        """没客户端时只留最新一条：重连不该收到一堆积压的旧位置。"""

        for x in (1.0, 2.0, 3.0):
            self.pipe.publish(_sample(x=x))
        with _connect(self.name) as client:
            self.assertEqual(_read_line(client)["type"], "hello")
            snapshot = _read_line(client)
            self.assertEqual(snapshot["type"], "position")
            self.assertEqual(snapshot["x"], 3.0, "补发的应是最后一条")
            self.pipe.publish(_sample(x=4.0))
            self.assertEqual(_read_line(client)["x"], 4.0)

    def test_client_can_reconnect(self):
        with _connect(self.name) as client:
            _read_line(client)
        with _connect(self.name) as client:
            self.assertEqual(_read_line(client)["type"], "hello")
            self.pipe.publish(_sample(x=5.0))
            self.assertEqual(_read_line(client)["x"], 5.0)
        self.assertGreaterEqual(self.pipe.stats()["connections"], 2)

    def test_stats_report_the_stream(self):
        with _connect(self.name) as client:
            _read_line(client)
            stats = self.pipe.stats()
            self.assertTrue(stats["connected"])
            self.assertEqual(stats["pipe"], self.name)
            self.assertIsNone(stats["error"])


class EncodingTest(unittest.TestCase):
    def test_line_is_ndjson_with_type_and_payload_fields(self):
        line = encode_position(_sample())
        self.assertTrue(line.endswith(b"\n"))
        payload = json.loads(line.decode("utf-8"))
        self.assertEqual(payload["type"], "position")
        self.assertEqual(payload["mapId"], "map02")
        self.assertEqual(len(line.splitlines()), 1, "一条坐标只能是一行")

    def test_default_pipe_name_is_absolute(self):
        self.assertTrue(DEFAULT_PIPE_NAME.startswith(r"\\.\pipe" + "\\"))


@unittest.skipUnless(os.name == "nt", "命名管道只在 Windows 上可用")
class DecoderToPipeTest(unittest.TestCase):
    """端到端：报文解出一条坐标，应当顺着 sink 走到管道客户端手里。"""

    def test_decoded_position_reaches_the_client(self):
        name = rf"\\.\pipe\endfield-capture-e2e-{os.getpid()}"
        pipe = PositionPipe(name, run_directory="runs/test")
        state = State()
        state.subscribe(pipe.publish)
        pipe.start()
        try:
            flow = Flow(1, ("10.0.0.1", 51000), ("10.0.0.2", 30000), state, bootstrap=None)
            with _connect(name) as client:
                self.assertEqual(_read_line(client)["type"], "hello")
                flow._extract_positions(_move_message(361), merged=False)  # 雪松林
                line = _read_line(client)
                self.assertEqual(line["type"], "position")
                self.assertEqual(line["scene"], 361)
                self.assertEqual(line["levelId"], "map02_lv009")
                self.assertEqual(line["mapName"], "武陵")
                self.assertAlmostEqual(line["y"], 259.47, places=2)
        finally:
            pipe.stop()

    def test_sink_failure_does_not_break_decoding(self):
        """推给消费方失败不能拖垮解码：坐标照样进 state。"""

        state = State()

        def broken(_payload):
            raise RuntimeError("消费方炸了")

        state.subscribe(broken)
        flow = Flow(1, ("10.0.0.1", 51000), ("10.0.0.2", 30000), state, bootstrap=None)
        flow._extract_positions(_move_message(232), merged=False)
        self.assertEqual(state.last["scene"], 232)
        self.assertEqual(state.leaders, 1)
        self.assertTrue(any("推送坐标失败" in event["message"] for event in state.events))


if __name__ == "__main__":
    unittest.main()
