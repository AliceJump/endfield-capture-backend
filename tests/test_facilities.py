"""Independent factory wire fixtures for facilities beyond travel poles."""

import json
import math
import struct
import unittest

from endfield_capture.decoder import Flow, State
from endfield_capture.facilities import FacilityStore
from endfield_capture.factory_schema import FACTORY_MESSAGES


def varint(value):
    result = bytearray()
    while value > 127:
        result.append(value & 127 | 128)
        value >>= 7
    return bytes(result + bytes([value]))


def integer(number, value):
    return varint(number << 3) + varint(value)


def blob(number, value):
    return varint(number << 3 | 2) + varint(len(value)) + value


def chapter(nodes=(), *, name=b"domain_2", maps=()):
    return blob(2, name) + b"".join(blob(3, node) for node in nodes) + b"".join(blob(7, entry) for entry in maps)


def node(nid, cid, payload, *, template=b"furnance_1"):
    return integer(1, nid) + blob(3, template) + blob(12, integer(1, cid) + payload)


def wire(wid=2**63 + 7, source=100, target=200):
    return integer(1, wid) + integer(2, source) + integer(3, target)


class FacilityTest(unittest.TestCase):
    def setUp(self):
        self.store = FacilityStore()
        self.store.begin_session(1)

    def apply(self, mid, body, flow=1):
        self.store.apply(flow, mid, body, 12.5)
        return self.store.snapshot()

    def baseline(self):
        return self.apply(
            221,
            chapter(
                [
                    node(10, 100, blob(14, blob(1, b"formula") + integer(2, 2**60) + integer(5, 1))),
                    node(20, 200, blob(20, b""), template=b"power_pole_3"),
                ],
                maps=[integer(1, 85) + blob(2, wire())],
            ),
        )

    def test_non_zipline_components_and_64_bit_values(self):
        data = self.baseline()
        self.assertEqual(data["nodeCount"], 2)
        self.assertEqual(data["templateCounts"], {"furnance_1": 1, "power_pole_3": 1})
        producer = data["nodes"][0]["components"][0]["producer"]
        self.assertEqual(producer["formulaId"], "formula")
        self.assertEqual(producer["currentProgress"], str(2**60))
        self.assertTrue(producer["inProduce"])
        self.assertFalse(producer["inBlock"])
        self.assertEqual(data["nodes"][1]["components"][0]["powerPole"], {})
        self.assertIsNone(data["nodes"][0]["position"])
        self.assertEqual(data["wires"][0]["wireId"], str(2**63 + 7))
        self.assertEqual(data["wires"][0]["sourceNodeId"], 10)
        json.dumps(data, allow_nan=False)

    def test_grid_and_world_positions_are_distinct_and_map_values_are_typed(self):
        grid = integer(1, 12)
        world = b"\x0d" + struct.pack("<f", 12.5)
        transform = blob(1, grid) + integer(4, 85) + blob(9, world)
        component_map = integer(1, 3) + integer(2, 100)
        entity = blob(6, integer(1, 2**63 + 9))
        payload = node(10, 100, blob(20, b"")) + blob(4, transform) + blob(11, component_map) + entity
        data = self.apply(221, chapter([payload]))["nodes"][0]
        self.assertEqual(data["transform"]["gridPosition"]["x"], 12)
        self.assertEqual(data["position"]["x"], 12.5)
        self.assertEqual(data["componentPos"], {"3": 100})
        self.assertEqual(data["interactiveObject"]["objectId"], str(2**63 + 9))

    def test_real_component_ports_and_conveyor_repeated_items(self):
        # Factory components link through ports as well as the map wire list.
        port = integer(1, 3) + integer(2, 100) + integer(3, 200)
        ports = blob(9, blob(1, port))
        item = blob(1, b"ore") + integer(2, 7)
        conveyor = blob(15, blob(2, item))
        data = self.apply(221, chapter([node(10, 100, ports + conveyor)]))
        component = data["nodes"][0]["components"][0]
        self.assertEqual(component["portManager"]["ports"][0]["touchComId"], 200)
        self.assertEqual(component["boxConveyor"]["items"][0]["count"], 7)

    def test_component_delta_updates_non_zipline_and_empty_payload_resets_defaults(self):
        self.baseline()
        update = integer(1, 100) + blob(14, b"")
        data = self.apply(223, blob(2, b"domain_2") + blob(3, update))
        self.assertEqual(data["nodes"][0]["components"][0]["producer"]["currentProgress"], "0")
        self.assertEqual(data["nodeCount"], 2)

    def test_wire_deltas_and_deleted_endpoints_preserve_unresolved_references(self):
        self.baseline()
        self.apply(222, blob(2, b"domain_2") + blob(4, varint(20)))
        link = self.store.snapshot()["wires"][0]
        self.assertFalse(link["targetResolved"])
        self.assertIsNone(link["targetNodeId"])
        self.apply(222, chapter([node(20, 200, blob(20, b""))]))
        self.assertTrue(self.store.snapshot()["wires"][0]["targetResolved"])
        delta = blob(2, b"domain_2") + integer(3, 85)
        data = self.apply(227, delta + blob(11, wire(9, target=999)))
        self.assertEqual(data["wireCount"], 2)
        data = self.apply(227, delta + blob(12, varint(2**63 + 7)))
        self.assertEqual([w["wireId"] for w in data["wires"]], ["9"])
        self.assertFalse(data["wires"][0]["targetResolved"])
        data = self.apply(227, delta + integer(12, 9))
        self.assertEqual(data["wires"], [])

    def test_chapter_and_map_scoping_and_empty_resync(self):
        self.baseline()
        self.apply(
            221, chapter([node(10, 100, blob(20, b""))], name=b"domain_1", maps=[integer(1, 2) + blob(2, wire())])
        )
        self.apply(227, blob(2, b"domain_2") + integer(3, 2) + blob(11, wire(8)))
        data = self.apply(221, chapter())
        self.assertEqual(data["nodeCount"], 1)
        self.assertEqual(data["wireCount"], 1)
        self.assertEqual(data["wires"][0]["chapterId"], "domain_1")
        self.assertFalse(data["wires"][0]["targetResolved"])

    def test_invalid_messages_clear_both_maps_and_nodes_and_deltas_need_a_baseline(self):
        self.baseline()
        data = self.apply(227, b"\x5a\xff")
        self.assertIsNotNone(data["error"])
        self.assertEqual(data["nodes"], [])
        self.assertEqual(data["wires"], [])
        data = self.apply(227, blob(2, b"domain_2") + integer(3, 85) + blob(11, wire()))
        self.assertEqual(data["syncedChapters"], [])
        self.assertEqual(data["wires"], [])
        self.assertIsNone(self.baseline()["error"])

    def test_non_finite_component_value_invalidates_before_json_encoding(self):
        self.baseline()
        # Soil neighbor increase rate, F14 float, independent from Transform.
        invalid = varint(14 << 3 | 5) + struct.pack("<f", math.inf)
        data = self.apply(222, chapter([node(30, 300, blob(37, invalid))]))
        self.assertIn("ValueError", data["error"])
        self.assertEqual(data["wires"], [])
        json.dumps(data, allow_nan=False)

    def test_partial_update_keeps_components_and_replacement_drops_old_index(self):
        self.baseline()
        self.apply(222, chapter([integer(1, 10) + integer(10, 16)]))
        self.assertTrue(self.store.snapshot()["nodes"][0]["shared"])
        data = self.apply(222, chapter([node(10, 110, blob(20, b""))]))
        self.assertFalse(data["wires"][0]["sourceResolved"])

    def test_new_session_clears_maps_and_ignores_old_flow(self):
        self.baseline()
        self.store.begin_session(2)
        data = self.apply(221, chapter([node(10, 100, blob(20, b""))]))
        self.assertEqual(data["flow"], 2)
        self.assertEqual(data["nodes"], [])
        self.assertEqual(data["wires"], [])
        self.store.begin_session(1)
        self.assertEqual(self.store.flow, 2)
        self.apply(221, chapter([node(10, 100, blob(20, b""))]), flow=2)
        self.store.begin_session(2)
        self.assertEqual(self.store.summary()["nodeCount"], 0)

    def test_scope_removal_drops_nodes_maps_and_baseline_only_for_listed_chapters(self):
        self.baseline()
        self.apply(221, chapter([node(10, 100, blob(20, b""))], name=b"domain_1"))
        for mid, name in ((211, b"domain_2"), (212, b"domain_1")):
            data = self.apply(mid, blob(11, name))
            self.assertNotIn(name.decode(), data["syncedChapters"])
            self.assertEqual(data["wires"], [])
            data = self.apply(222, chapter([node(10, 100, blob(20, b""))], name=name))
            self.assertNotIn(name.decode(), data["syncedChapters"])
        self.assertEqual(data["nodes"], [])

    def test_flow_routes_factory_updates_without_breaking_positions(self):
        state = State()
        state.facilities.begin_session(1)
        state.travel_poles.begin_session(1)
        flow = Flow(1, ("client", 1), ("server", 30000), state, None)
        body = chapter([node(10, 100, blob(20, b""))], maps=[integer(1, 85) + blob(2, wire())])
        flow.business(221, body, "in", False)
        self.assertEqual(state.facilities.summary()["nodeCount"], 1)
        self.assertEqual(state.travel_poles.summary()["nodeCount"], 0)
        flow.business(227, blob(2, b"domain_2") + integer(3, 85) + integer(12, 2**63 + 7), "out", False)
        self.assertEqual(state.facilities.summary()["wireCount"], 1)
        flow.business(227, b"\x5a\xff", "in", False)
        flow.business(81, b"", "out", False)
        self.assertEqual(state.moves, 1)
        self.assertFalse(flow.failed)


class SchemaTest(unittest.TestCase):
    def test_public_schema_recognizes_map_delta(self):
        self.assertEqual(FACTORY_MESSAGES[227].FromString(blob(2, b"domain_2")).chapter_id, "domain_2")
