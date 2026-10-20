"""Synthetic wire tests: no client files, PCAPs or account data required."""

from __future__ import annotations

import math
import struct
import unittest
from types import SimpleNamespace
from unittest import mock

from endfield_capture.decoder import Flow, State
from endfield_capture.factory_schema import FACTORY_MESSAGES
from endfield_capture.travel_poles import TravelPoleStore


def varint(value):
    out = bytearray()
    while value > 127:
        out.append((value & 127) | 128)
        value >>= 7
    return bytes(out + bytes([value]))


def integer(field, value):
    return varint(field << 3) + varint(value)


def blob(field, value):
    return varint(field << 3 | 2) + varint(len(value)) + value


def vector(x, y, z):
    return b"".join(varint(field << 3 | 5) + struct.pack("<f", value) for field, value in enumerate((x, y, z), 1))


def component(cid, target=0):
    return integer(1, cid) + blob(24, integer(11, target) if target else b"")


def node(nid, cid, target=0, *, position=(0, 0, 0), owner=None, shared=False):
    transform = integer(4, 85) + blob(10, vector(0, 270, 0))
    if position is not None:
        transform += blob(9, vector(*position))
    out = (
        integer(1, nid)
        + blob(3, b"travel_pole_2")
        + blob(4, transform)
        + integer(10, 16 if shared else 0)
        + blob(12, component(cid, target))
    )
    if owner is not None:
        out += blob(12, integer(1, cid + 1) + blob(44, integer(1, owner) + integer(2, 12) + integer(6, 1)))
    return out


def chapter(name="domain_2", *, nodes=(), components=(), removed=(), packed=True):
    out = blob(2, name.encode()) + b"".join(blob(3, entry) for entry in (*nodes, *components))
    if packed:
        if removed:
            out += blob(4, b"".join(varint(nid) for nid in removed))
    else:
        out += b"".join(integer(4, nid) for nid in removed)
    return out


class TravelPoleTest(unittest.TestCase):
    def setUp(self):
        self.store = TravelPoleStore()
        self.store.begin_session(1)

    def apply(self, mid, body, flow=1):
        self.store.apply(flow, mid, body, 12.5)
        return self.store.snapshot()

    def baseline(self):
        return self.apply(221, chapter(nodes=(node(10, 100, 20), node(20, 200, position=(3, 4, 0)))))

    def test_full_sync_exposes_node_fields_and_directed_links(self):
        data = self.apply(
            221, chapter(nodes=(node(10, 100, 20, owner=2**63 + 7, shared=True), node(20, 200, position=(3, 4, 0))))
        )
        self.assertEqual(data["nodeCount"], 2)
        source = data["nodes"][0]
        self.assertEqual(source["rotation"], {"x": 0.0, "y": 270.0, "z": 0.0})
        self.assertEqual(source["social"], {"ownerId": str(2**63 + 7), "socialNodeId": 12, "preset": True})
        self.assertTrue(source["shared"])
        self.assertIsNone(source["power"])
        self.assertEqual(source["mapId"], 85)
        self.assertEqual(data["defaultLinkCount"], 1)
        link = data["defaultLinks"][0]
        self.assertEqual((link["sourceComponentId"], link["targetNodeId"]), (100, 20))
        self.assertEqual((link["distance3D"], link["yawToTarget"]), (5.0, 90.0))

    def test_component_delta_clears_default_next_with_empty_payload(self):
        self.baseline()
        data = self.apply(223, chapter(components=(component(100),)))
        self.assertEqual(data["defaultLinks"], [])
        self.assertEqual(data["nodes"][0]["defaultNext"], 0)

    def test_component_delta_targets_component_id_not_node_id(self):
        self.baseline()
        data = self.apply(223, chapter(components=(component(100, 30), component(10, 99))))
        self.assertEqual(data["nodes"][0]["defaultNext"], 30)
        self.assertFalse(data["defaultLinks"][0]["targetResolved"])

    def test_social_component_updates_without_overwriting_travel_payload(self):
        self.apply(221, chapter(nodes=(node(10, 100, 20, owner=1),)))
        social = integer(1, 101) + blob(44, integer(1, 2**63))
        data = self.apply(223, chapter(components=(social,)))
        self.assertEqual(data["nodes"][0]["social"]["ownerId"], str(2**63))
        self.assertEqual(data["nodes"][0]["defaultNext"], 20)

    def test_empty_social_and_power_messages_preserve_presence(self):
        payload = node(10, 100) + blob(8, b"") + blob(12, integer(1, 101) + blob(44, b""))
        data = self.apply(221, chapter(nodes=(payload, node(20, 200))))
        self.assertEqual(data["nodes"][0]["social"], {"ownerId": "0", "socialNodeId": 0, "preset": False})
        self.assertEqual(
            data["nodes"][0]["power"], {"inPower": False, "needInPower": False, "powerCost": 0, "powerCostShow": 0}
        )
        self.assertIsNone(data["nodes"][1]["social"])
        self.assertIsNone(data["nodes"][1]["power"])

    def test_replaced_non_travel_payload_removes_node_and_its_index(self):
        self.baseline()
        data = self.apply(223, chapter(components=(integer(1, 100) + blob(14, b""),)))
        self.assertEqual(data["nodeCount"], 1)
        self.assertEqual(data["defaultLinks"], [])
        data = self.apply(223, chapter(components=(component(100, 20),)))
        self.assertEqual(data["nodeCount"], 1)

    def test_delete_target_keeps_unresolved_reference_then_resolves_added_target(self):
        self.baseline()
        data = self.apply(222, chapter(removed=(20,)))
        self.assertEqual(data["nodeCount"], 1)
        self.assertFalse(data["defaultLinks"][0]["targetResolved"])
        data = self.apply(222, chapter(nodes=(node(20, 201, position=(0, 0, 8)),)))
        self.assertEqual(data["defaultLinks"][0]["distance3D"], 8.0)
        self.assertEqual(data["defaultLinks"][0]["yawToTarget"], 0.0)

    def test_unpacked_node_removal_is_supported(self):
        self.baseline()
        data = self.apply(222, chapter(removed=(10, 20), packed=False))
        self.assertEqual(data["nodes"], [])

    def test_node_update_without_components_merges_instead_of_dropping(self):
        self.baseline()
        # 只带节点字段、不带组件的 222：节点、组件索引与默认下一站都必须保留，
        # 明确出现的字段（这里是 flag）才覆盖。
        data = self.apply(222, chapter(nodes=(integer(1, 10) + integer(10, 16),)))
        self.assertEqual(data["nodeCount"], 2)
        self.assertEqual(data["defaultLinkCount"], 1)
        source = next(n for n in data["nodes"] if n["nodeId"] == 10)
        self.assertTrue(source["shared"])
        self.assertEqual(source["defaultNext"], 20)
        self.assertEqual(source["templateId"], "travel_pole_2")
        self.assertEqual(source["mapId"], 85)
        self.assertEqual(source["rotation"], {"x": 0.0, "y": 270.0, "z": 0.0})

    def test_node_update_without_components_does_not_resurrect_removed_node(self):
        self.baseline()
        self.apply(222, chapter(removed=(20,)))
        data = self.apply(222, chapter(nodes=(integer(1, 20),)))
        self.assertEqual([n["nodeId"] for n in data["nodes"]], [10])

    def test_deletion_and_node_replacement_remove_old_component_index(self):
        self.baseline()
        self.apply(222, chapter(nodes=(node(10, 110, 20),)))
        self.apply(223, chapter(components=(component(100, 99),)))
        self.assertEqual(self.store.snapshot()["nodes"][0]["defaultNext"], 20)
        self.apply(222, chapter(removed=(10,)))
        data = self.apply(223, chapter(components=(component(110, 99),)))
        self.assertEqual(data["nodeCount"], 1)

    def test_same_component_and_node_ids_are_scoped_by_chapter(self):
        self.baseline()
        self.apply(221, chapter("domain_1", nodes=(node(10, 100, 99),)))
        data = self.apply(223, chapter("domain_1", components=(component(100),)))
        self.assertEqual(data["syncedChapters"], ["domain_1", "domain_2"])
        self.assertEqual(data["defaultLinkCount"], 1)
        self.assertEqual(data["defaultLinks"][0]["chapterId"], "domain_2")

    def test_full_sync_replaces_only_its_chapter_even_when_empty(self):
        self.baseline()
        self.apply(221, chapter("domain_1", nodes=(node(30, 300),)))
        data = self.apply(221, chapter())
        self.assertEqual(data["nodeCount"], 1)
        self.assertEqual(data["nodes"][0]["chapterId"], "domain_1")
        self.assertEqual(data["syncedChapters"], ["domain_1", "domain_2"])

    def test_no_baseline_deltas_are_not_presented_as_complete(self):
        self.apply(222, chapter(nodes=(node(10, 100),)))
        data = self.apply(223, chapter(components=(component(100, 20),)))
        self.assertEqual(data["syncedChapters"], [])
        self.assertEqual(data["nodes"], [])

    def test_new_authenticated_flow_clears_old_data_and_ignores_late_old_flow(self):
        self.baseline()
        self.store.begin_session(2)
        self.store.begin_session(1)
        data = self.apply(221, chapter(nodes=(node(40, 400),)))
        self.assertEqual(data["flow"], 2)
        self.assertEqual(data["nodes"], [])
        data = self.apply(221, chapter(nodes=(node(50, 500),)), flow=2)
        self.assertEqual([n["nodeId"] for n in data["nodes"]], [50])

    def test_same_flow_rehandshake_clears_previous_snapshot(self):
        self.baseline()
        self.store.begin_session(1)  # 同一条连接重新握手：flow 号不变也算新会话
        data = self.apply(221, chapter("domain_3", nodes=(node(40, 400),)))
        self.assertEqual(data["syncedChapters"], ["domain_3"])
        self.assertEqual([n["nodeId"] for n in data["nodes"]], [40])
        self.assertEqual(data["defaultLinks"], [])

    def test_missing_world_position_is_not_invented_as_origin(self):
        data = self.apply(221, chapter(nodes=(node(10, 100, 20, position=None), node(20, 200))))
        self.assertIsNone(data["nodes"][0]["position"])
        self.assertTrue(data["defaultLinks"][0]["targetResolved"])
        self.assertIsNone(data["defaultLinks"][0]["distance3D"])

    def test_coincident_horizontal_positions_have_no_bearing(self):
        data = self.apply(221, chapter(nodes=(node(10, 100, 20), node(20, 200, position=(0, 5, 0)))))
        self.assertIsNone(data["defaultLinks"][0]["yawToTarget"])
        self.assertEqual(data["defaultLinks"][0]["distance3D"], 5.0)

    def test_unrelated_nodes_and_unknown_fields_are_not_exported(self):
        unrelated = integer(1, 77) + blob(3, b"producer") + blob(12, integer(1, 777) + blob(14, b""))
        data = self.apply(221, chapter(nodes=(unrelated, node(10, 100))) + blob(99, b"ignored"))
        self.assertEqual(data["nodeCount"], 1)

    def test_non_finite_transform_is_rejected_and_invalidates_the_snapshot(self):
        self.baseline()
        for invalid in (math.inf, math.nan):
            data = self.apply(221, chapter(nodes=(node(10, 100, position=(invalid, 0, 0)),)))
            self.assertEqual(data["nodes"], [])
            self.assertEqual(data["syncedChapters"], [])
            self.assertIn("ValueError", data["error"])

    def test_component_delta_for_unseen_component_id_is_ignored(self):
        # 已知边界：223 只对 221/222 索引过的 ComponentId 生效，之后才新挂上去的
        # 组件得等下一次完整同步。
        self.apply(221, chapter(nodes=(node(10, 100, 20),)))
        data = self.apply(223, chapter(components=(component(999, 30),)))
        self.assertEqual([n["defaultNext"] for n in data["nodes"]], [20])


class FlowTravelPoleTest(unittest.TestCase):
    def test_handshake_starts_new_snapshot(self):
        state = State()
        state.travel_poles.begin_session(1)
        state.travel_poles.apply(1, 221, chapter(nodes=(node(10, 100),)), 0)
        state.facilities.begin_session(1)
        state.facilities.apply(1, 221, chapter(nodes=(node(10, 100),)), 0)
        bootstrap = SimpleNamespace(keys=lambda body: (b"k" * 32, b"n" * 12), modulus_seconds=0)
        flow = Flow(2, ("client", 1), ("server", 30000), state, bootstrap)
        head = integer(1, 1)
        buffer = bytearray(bytes([len(head), 0, 0]) + head)
        flow._consume_initial_response(buffer, len(head), len(buffer))
        self.assertEqual(state.travel_poles.snapshot()["nodes"], [])
        self.assertEqual(state.travel_poles.flow, 2)
        self.assertEqual(state.facilities.snapshot()["nodes"], [])
        self.assertEqual(state.facilities.flow, 2)

    def test_invalid_factory_message_does_not_break_coordinate_decoder(self):
        state = State()
        state.travel_poles.begin_session(1)
        flow = Flow(1, ("client", 1), ("server", 30000), state, None)
        flow.business(221, chapter(nodes=(node(10, 100),)), "in", False)
        flow.business(221, b"\x1a\xff", "in", False)
        self.assertFalse(flow.failed)
        self.assertEqual(state.travel_poles.snapshot()["nodes"], [])
        self.assertIsNotNone(state.travel_poles.error)
        flow.business(81, b"", "out", False)
        self.assertEqual(state.moves, 1)
        flow.business(221, chapter(nodes=(node(20, 200),)), "in", False)
        self.assertIsNone(state.travel_poles.error)
        self.assertEqual(state.travel_poles.summary()["nodeCount"], 1)

    def test_outbound_factory_messages_are_ignored(self):
        state = State()
        state.travel_poles.begin_session(1)
        flow = Flow(1, ("client", 1), ("server", 30000), state, None)
        flow.business(221, chapter(nodes=(node(10, 100),)), "out", False)
        self.assertEqual(state.travel_poles.summary()["nodeCount"], 0)

    def test_unexpected_error_never_reaches_the_decoder(self):
        """解析层出意料之外的 bug 时，滑索快照作废，但连接与坐标解码照旧。"""

        state = State()
        state.travel_poles.begin_session(1)
        flow = Flow(1, ("client", 1), ("server", 30000), state, None)

        class Exploding:
            @staticmethod
            def FromString(body):
                raise RuntimeError("boom")

        with mock.patch.dict(FACTORY_MESSAGES, {221: Exploding}):
            flow.business(221, chapter(nodes=(node(10, 100),)), "in", False)
        self.assertFalse(flow.failed)
        self.assertIn("RuntimeError", state.travel_poles.summary()["error"])
        flow.business(81, b"", "out", False)
        self.assertEqual(state.moves, 1)
