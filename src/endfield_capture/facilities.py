"""Observed factory facilities and explicit component-to-component map wires.

This is a session snapshot, not a static world catalogue. Component payloads use
protobuf JSON conventions (including decimal strings for all 64-bit integers).
"""

from __future__ import annotations

import math
from collections import Counter

from google.protobuf.json_format import MessageToDict

from .factory_nodes import FactoryNodeStore, _vector


def _payload(message):
    return MessageToDict(message, always_print_fields_with_no_presence=True)


def _validate_floats(message):
    """Validate even component payloads before exposing them as strict JSON."""
    for field, value in message.ListFields():
        if field.message_type and field.message_type.GetOptions().map_entry:
            value_field = field.message_type.fields_by_name["value"]
            if value_field.type == field.TYPE_MESSAGE:
                for entry in value.values():
                    _validate_floats(entry)
            elif value_field.type in (field.TYPE_FLOAT, field.TYPE_DOUBLE) and not all(
                math.isfinite(entry) for entry in value.values()
            ):
                raise ValueError("non-finite factory payload")
        elif field.type == field.TYPE_MESSAGE:
            for entry in value if field.is_repeated else (value,):
                _validate_floats(entry)
        elif field.type in (field.TYPE_FLOAT, field.TYPE_DOUBLE):
            if not all(math.isfinite(entry) for entry in (value if field.is_repeated else (value,))):
                raise ValueError("non-finite factory payload")


class FacilityStore(FactoryNodeStore):
    def __init__(self):
        super().__init__()
        self._maps: dict[tuple[str, int], dict] = {}

    def _clear(self):
        super()._clear()
        self._maps.clear()

    def _drop_chapter(self, chapter):
        super()._drop_chapter(chapter)
        self._maps = {key: value for key, value in self._maps.items() if key[0] != chapter}

    def _ingest(self, message_id, chapter, message, timestamp):
        _validate_floats(message)
        if message_id != 221 and chapter not in self._chapters:
            return
        super()._ingest(message_id, chapter, message, timestamp)
        if message_id == 221:
            for entry in message.maps:
                self._maps[chapter, entry.map_id] = {wire.index: wire for wire in entry.wires}
        elif message_id == 227:
            wires = self._maps.setdefault((chapter, message.map_id), {})
            for wire in message.wires:
                wires[wire.index] = wire
            for wire_id in message.remove_wires:
                wires.pop(wire_id, None)

    def summary(self):
        return {
            "flow": self.flow,
            "updatedAt": self.updated_at,
            "revision": self.revision,
            "syncedChapters": sorted(self._chapters),
            "nodeCount": sum(len(nodes) for nodes in self._chapters.values()),
            "wireCount": sum(len(wires) for wires in self._maps.values()),
            "error": self.error,
        }

    def snapshot(self):
        nodes = []
        for chapter, entries in sorted(self._chapters.items()):
            for node_id, node in sorted(entries.items()):
                data = _payload(node)
                transform = node.transform
                nodes.append(
                    {
                        **data,
                        "chapterId": chapter,
                        "nodeId": node_id,
                        "mapId": transform.map_id if node.HasField("transform") else None,
                        "position": _vector(transform.position) if transform.HasField("position") else None,
                        "rotation": _vector(transform.rotation) if transform.HasField("rotation") else None,
                        "transform": data.get("transform"),
                        "power": data.get("power"),
                        "interactiveObject": data.get("interactiveObject"),
                        "shared": bool(node.flag & 0x10),
                        "origin": "shared" if node.flag & 0x10 else "self_or_preset",
                    }
                )
        wires = []
        for (chapter, map_id), entries in sorted(self._maps.items()):
            for wire_id, wire in sorted(entries.items()):
                source = self._components.get((chapter, wire.from_com_id))
                target = self._components.get((chapter, wire.to_com_id))
                wires.append(
                    {
                        "chapterId": chapter,
                        "mapId": map_id,
                        "wireId": str(wire_id),
                        "sourceComponentId": str(wire.from_com_id),
                        "targetComponentId": str(wire.to_com_id),
                        "sourceNodeId": source,
                        "targetNodeId": target,
                        "sourceResolved": source is not None,
                        "targetResolved": target is not None,
                    }
                )
        return {
            **self.summary(),
            "componentStateMode": "last_node_or_component_sync",
            "templateCounts": dict(sorted(Counter(node["templateId"] for node in nodes).items())),
            "nodes": nodes,
            "wires": wires,
        }
