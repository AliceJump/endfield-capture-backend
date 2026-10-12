"""不需要游戏、网卡与 Qt 的单元测试。

解码正确性由 ``selftest`` 回放真实抓包来保证；这里覆盖的是各个小部件的行为契约，
尤其是那些"错了会静默产出错误数据"的地方。
"""

from __future__ import annotations

import struct
import unittest

from endfield_capture.decoder import Engine, Reassembler, State, protobuf_fields, value
from endfield_capture.server import QUEUE_LIMIT, find_ok_end_field, truncate_pcap_prefix


def _varint(number: int) -> bytes:
    out = bytearray()
    while True:
        byte = number & 0x7F
        number >>= 7
        out.append(byte | (0x80 if number else 0))
        if not number:
            return bytes(out)


def _field(number: int, payload: bytes) -> bytes:
    return _varint((number << 3) | 2) + _varint(len(payload)) + payload


class ProtobufFieldsTest(unittest.TestCase):
    def test_reads_nested_and_repeated(self):
        inner = _field(1, struct.pack("<f", 1.5)) + _field(2, struct.pack("<f", -2.5))
        body = _field(1, inner) + _field(1, inner) + _varint((3 << 3) | 0) + _varint(7)
        fields = protobuf_fields(body)
        self.assertEqual(len(fields[1]), 2, "重复字段应保留全部取值")
        self.assertEqual(value(fields, 3), 7)
        self.assertEqual(protobuf_fields(fields[1][0])[1][0], struct.pack("<f", 1.5))

    def test_last_value_wins(self):
        body = _varint((2 << 3) | 0) + _varint(1) + _varint((2 << 3) | 0) + _varint(9)
        self.assertEqual(value(protobuf_fields(body), 2), 9)

    def test_rejects_bad_input(self):
        for raw in (b"\x08", b"\x0a\x05ab", b"\x00", b"\x07"):
            with self.assertRaises(ValueError, msg=raw):
                protobuf_fields(raw)

    def test_truncated_varint_is_rejected(self):
        with self.assertRaises(ValueError):
            protobuf_fields(b"\x80" * 12)


class ReassemblerTest(unittest.TestCase):
    """乱序、重传、重叠都必须拼出正确字节流——这里是静默错位的高发区。"""

    # 注意：带 syn 的那次调用会把基线设成 seq+1，所以后续负载要从 1001 开始编号。
    SYN_SEQ = 1000
    BASE = 1001

    def _started(self) -> Reassembler:
        stream = Reassembler()
        self.assertEqual(stream.feed(self.SYN_SEQ, b"", syn=True), b"")
        return stream

    def test_out_of_order_then_fill(self):
        stream = self._started()
        self.assertEqual(stream.feed(self.BASE + 6, b"ghi"), b"", "先到的后段要缓存住")
        self.assertEqual(stream.feed(self.BASE, b"abc"), b"abc")
        self.assertEqual(stream.feed(self.BASE + 3, b"def"), b"defghi")

    def test_duplicate_is_ignored(self):
        stream = self._started()
        self.assertEqual(stream.feed(self.BASE, b"abc"), b"abc")
        self.assertEqual(stream.feed(self.BASE, b"abc"), b"")

    def test_overlapping_retransmit_keeps_tail(self):
        stream = self._started()
        self.assertEqual(stream.feed(self.BASE, b"abc"), b"abc")
        # 从偏移 2 重传，覆盖已交付的 "c"，只应补出后续的 "def"
        self.assertEqual(stream.feed(self.BASE + 2, b"cdef"), b"def", "重叠部分不能重复输出")

    def test_pending_records_gap(self):
        stream = self._started()
        self.assertEqual(stream.feed(self.BASE, b"abc"), b"abc")
        self.assertEqual(stream.feed(self.BASE + 9, b"xyz"), b"", "中间缺字节，先缓存")
        self.assertIsNotNone(stream.gap_since, "有等待补齐的字节时应记录起始时间")
        # 补齐偏移 3..8（""defghi"），正好接上偏移 9 的 "xyz"
        self.assertEqual(stream.feed(self.BASE + 3, b"defghi"), b"defghixyz")
        self.assertIsNone(stream.gap_since, "补齐后应清除空洞标记")

    def test_segment_before_base_is_dropped(self):
        stream = self._started()
        self.assertEqual(stream.feed(900, b"zzz"), b"")


class EngineDispatchTest(unittest.TestCase):
    """引擎要按端口认连接，并且**拒绝**没有起点的流。"""

    class _Packet(dict):
        """够用的假包：只实现解码器真正会碰的访问方式。"""

    def _make_packet(self, src, sport, dst, dport, seq, payload, flags=0x18):
        from scapy.all import IP, TCP, Ether

        # 过一遍序列化：ip.len 是构建时才填的，而 tcp_payload 要用它去掉链路层补齐字节
        packet = Ether() / IP(src=src, dst=dst) / TCP(sport=sport, dport=dport, seq=seq, flags=flags) / payload
        return Ether(bytes(packet))

    def _engine(self):
        state = State()
        engine = Engine(state, bootstrap=None)  # 这些用例不会走到需要 bootstrap 的分支
        return state, engine

    def test_ignores_other_ports(self):
        state, engine = self._engine()
        engine.receive(self._make_packet("10.0.0.1", 51000, "10.0.0.2", 443, 1, b"hello"))
        self.assertEqual(state.packets, 0, "非游戏端口不该被计入")
        self.assertEqual(dict(engine.flows), {})

    def test_tracks_game_port_both_directions(self):
        state, engine = self._engine()
        engine.receive(self._make_packet("10.0.0.1", 51000, "10.0.0.2", 30000, 1, b"\x00\x00\x00", flags=0x02))
        self.assertEqual(state.packets, 1)
        self.assertEqual(len(engine.flows), 1, "两个方向应归到同一条连接")
        engine.receive(self._make_packet("10.0.0.2", 30000, "10.0.0.1", 51000, 1, b"\x00\x00\x00"))
        self.assertEqual(len(engine.flows), 1, "反向包不应新建连接")
        self.assertEqual(state.packets, 2)

    def test_stream_without_syn_is_refused(self):
        """抓包晚于连接建立时必须明确拒绝，而不是解出貌似合理的数据。"""

        state, engine = self._engine()
        engine.receive(self._make_packet("10.0.0.1", 51000, "10.0.0.2", 30000, 5000, b"\x0c\x10\x01abc"))
        flow = next(iter(engine.flows.values()))
        self.assertEqual(flow.status, "缺少连接起点，请重新启动游戏")
        self.assertEqual(state.frames, 0, "拒绝的流不应产出任何帧")


class ServerHelpersTest(unittest.TestCase):
    def test_truncate_pcap_drops_partial_tail(self):
        # 24 字节头 + 一个完整包(16 字节头 + 4 字节数据) + 半个包
        head = b"\xd4\xc3\xb2\xa1" + b"\x00" * 20
        full = struct.pack("<IIII", 0, 0, 4, 4) + b"data"
        partial = struct.pack("<IIII", 0, 0, 100, 100) + b"xy"
        source = head + full + partial
        result = truncate_pcap_prefix(source)
        self.assertEqual(result, head + full, "尾部不完整的包必须丢掉")

    def test_find_ok_end_field_returns_none_for_bad_path(self):
        self.assertIsNone(find_ok_end_field("/definitely/not/a/repo"))

    def test_queue_limit_is_sane(self):
        self.assertGreaterEqual(QUEUE_LIMIT, 1000)


class StateTest(unittest.TestCase):
    def test_scene_maps_summary_groups_by_map(self):
        state = State()
        state.scene_maps["232"]["map02"] += 3
        state.scene_maps["232"]["map01"] += 1
        state.scene_maps["102"]["map02"] += 5
        rows = {row["scene"]: row for row in state.scene_maps_summary()}
        self.assertEqual(rows[232]["total"], 4)
        self.assertEqual(rows[232]["maps"][0], {"mapId": "map02", "count": 3}, "多的排前面")
        self.assertEqual(rows[102]["total"], 5)

    def test_snapshot_has_the_contract_keys(self):
        state = State()
        snapshot = state.snapshot()
        for key in ("lastPosition", "sceneMaps", "flows", "events", "now", "captureReady", "ws", "trajectory"):
            self.assertIn(key, snapshot)


class SrsaTest(unittest.TestCase):
    def test_constants_are_self_consistent(self):
        from endfield_capture.decoder import CLIENT_DLL_SHA256, CLIENT_METADATA_SHA256

        for digest in (CLIENT_DLL_SHA256, CLIENT_METADATA_SHA256):
            self.assertEqual(len(digest), 64, "哈希常量必须是完整的 64 位十六进制")
            int(digest, 16)

    def test_rejects_wrong_envelope_length(self):
        from endfield_capture.decoder import Bootstrap

        bootstrap = object.__new__(Bootstrap)
        with self.assertRaises(ValueError):
            bootstrap.unwrap(b"\x00" * 16)


if __name__ == "__main__":
    unittest.main()
