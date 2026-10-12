"""看板 HTTP 端点的集成测试。

这些用例真的起一个 HTTP 服务并请求每个端点。加它们的直接原因：SSE 分支曾经因为
写错一个方法名（``_send_response`` vs ``send_response``）而在**每次浏览器连接**时抛异常，
而当时的冒烟测试只看了 ``/`` 与 ``/api/state``，完全没碰到 SSE。

做法上先把 handler 提成模块级类（原先写在 ``serve()`` 里的闭包），否则没法测。
"""

from __future__ import annotations

import json
import threading
import time
import unittest
from http.client import HTTPConnection
from http.server import ThreadingHTTPServer
from pathlib import Path
from tempfile import TemporaryDirectory

from endfield_capture.decoder import State
from endfield_capture.server import DashboardHandler, make_handler


class _RunningServer:
    """在临时端口上跑真实的 handler。"""

    def __init__(self):
        self._tmp = TemporaryDirectory()
        self.run = Path(self._tmp.name)
        self.record = (self.run / "positions.jsonl").open("a", encoding="utf-8")
        self.state = State(self.record)
        self.stop = threading.Event()
        self.http = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(self.state, self.run, self.record, self.stop))
        self.port = self.http.server_address[1]
        self.thread = threading.Thread(target=self.http.serve_forever, daemon=True)

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *exc):
        self.stop.set()
        self.http.shutdown()
        self.http.server_close()
        self.record.close()
        self._tmp.cleanup()

    def get(self, path: str, timeout: float = 5.0):
        conn = HTTPConnection("127.0.0.1", self.port, timeout=timeout)
        try:
            conn.request("GET", path)
            response = conn.getresponse()
            return response.status, dict(response.getheaders()), response.read()
        finally:
            conn.close()


class EndpointTest(unittest.TestCase):
    def test_dashboard_page(self):
        with _RunningServer() as server:
            status, headers, body = server.get("/")
        self.assertEqual(status, 200)
        self.assertIn("text/html", headers["Content-Type"])
        self.assertIn(b"scene", body.lower())

    def test_state_endpoint(self):
        with _RunningServer() as server:
            status, headers, body = server.get("/api/state")
        self.assertEqual(status, 200)
        self.assertIn("application/json", headers["Content-Type"])
        payload = json.loads(body)
        for key in ("lastPosition", "sceneMaps", "flows", "captureReady", "ws", "runDirectory"):
            self.assertIn(key, payload)

    def test_positions_csv_has_header_and_bom(self):
        with _RunningServer() as server:
            status, headers, body = server.get("/api/positions.csv")
        self.assertEqual(status, 200)
        self.assertIn("text/csv", headers["Content-Type"])
        self.assertTrue(body.startswith("﻿".encode()), "CSV 应带 BOM，否则 Excel 打开中文列名会乱码")
        header = body.decode("utf-8-sig").splitlines()[0]
        for column in ("x", "y", "z", "yaw", "speed", "scene"):
            self.assertIn(column, header)

    def test_positions_csv_renders_written_rows(self):
        """写盘过的坐标必须能原样变成 CSV 行（这里正是序列化出过错的地方）。"""

        payload = {
            "time": 1.0, "flow": 1, "objectId": "1", "scene": 232, "x": -243.2, "y": 259.47, "z": -228.39,
            "yaw": 250.586, "speed": 0.0, "state": 3, "merged": False, "sampleId": 1,
        }
        with _RunningServer() as server:
            server.record.write(json.dumps(payload, ensure_ascii=False) + "\n")
            server.record.flush()
            status, _, body = server.get("/api/positions.csv")
        self.assertEqual(status, 200)
        rows = body.decode("utf-8-sig").splitlines()
        self.assertEqual(len(rows), 2, "应有一行表头 + 一行数据")
        self.assertIn("-243.2", rows[1])

    def test_event_stream_starts_and_sends(self):
        """SSE 必须能建立并推出至少一帧——这是当初漏测的那条分支。"""

        with _RunningServer() as server:
            conn = HTTPConnection("127.0.0.1", server.port, timeout=5)
            try:
                conn.request("GET", "/api/events")
                response = conn.getresponse()
                self.assertEqual(response.status, 200)
                self.assertIn("text/event-stream", response.getheader("Content-Type"))
                # 读一帧就够：够证明 handler 没在建立连接时炸掉
                line = response.fp.readline()
                while line and not line.startswith(b"data: "):
                    line = response.fp.readline()
                self.assertTrue(line.startswith(b"data: "), "SSE 应推出 data: 帧")
                payload = json.loads(line[len(b"data: ") :].decode())
                self.assertIn("leaderPositions", payload)
            finally:
                conn.close()

    def test_event_stream_stops_with_flag(self):
        with _RunningServer() as server:
            conn = HTTPConnection("127.0.0.1", server.port, timeout=5)
            conn.request("GET", "/api/events")
            response = conn.getresponse()
            self.assertEqual(response.status, 200)
            server.stop.set()
            deadline = time.time() + 5
            while time.time() < deadline and not response.isclosed():
                time.sleep(0.05)
            conn.close()

    def test_unknown_path_is_404(self):
        with _RunningServer() as server:
            status, _, body = server.get("/nope")
        self.assertEqual(status, 404)
        self.assertIn(b"not found", body)

    def test_query_string_is_ignored(self):
        with _RunningServer() as server:
            status, _, _ = server.get("/api/state?cachebust=1")
        self.assertEqual(status, 200)


class HandlerWiringTest(unittest.TestCase):
    def test_make_handler_binds_dependencies(self):
        state = State()
        stop = threading.Event()
        with TemporaryDirectory() as tmp:
            handler = make_handler(state, Path(tmp), None, stop)
        self.assertTrue(issubclass(handler, DashboardHandler))
        self.assertIs(handler.state, state)
        self.assertIs(handler.stop, stop)

    def test_csv_fields_cover_everything_positions_write(self):
        from endfield_capture.decoder import PositionRecord
        from endfield_capture.server import CSV_FIELDS

        written = set(PositionRecord(
            time=0.0, flow=1, object_id="1", scene=1, x=0.0, y=0.0, z=0.0,
            heading=0.0, speed=0.0, state=0, merged=False,
        ).to_dict())
        missing = written - set(CSV_FIELDS)
        self.assertEqual(missing, set(), f"CSV 缺列：{missing}")


if __name__ == "__main__":
    unittest.main()
