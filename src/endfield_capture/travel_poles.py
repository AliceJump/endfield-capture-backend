"""Factory travel-pole snapshots and directed configured next-stop references.

The caller holds State.lock. Each chapter needs a complete 221 baseline before
222/223 deltas are accepted; every authenticated connection starts a new store.
"""

from __future__ import annotations

import math

from .factory_schema import FACTORY_MESSAGES


def _vector(vector):
    values = {axis: getattr(vector, axis) for axis in "xyz"}
    if not all(math.isfinite(value) for value in values.values()):
        raise ValueError("non-finite factory transform")
    return values


class TravelPoleStore:
    def __init__(self):
        self.flow: int | None = None
        self.updated_at: float | None = None
        self.revision = 0
        self.error: str | None = None
        self._chapters: dict[str, dict] = {}
        self._components: dict[tuple[str, int], int] = {}

    def begin_session(self, flow: int) -> None:
        if self.flow is not None and flow <= self.flow:
            return
        self.flow = flow
        self._clear()

    def _clear(self):
        self._chapters.clear()
        self._components.clear()
        self.updated_at = None
        self.error = None
        self.revision += 1

    def invalidate(self, flow: int, message_id: int) -> None:
        if flow == self.flow:
            self._clear()
            # Do not put message bodies or account identifiers into diagnostics.
            self.error = f"invalid factory message {message_id}; waiting for full chapter sync"

    def apply(self, flow: int, message_id: int, body: bytes, timestamp: float) -> None:
        if flow != self.flow:
            return
        message = FACTORY_MESSAGES[message_id].FromString(body)
        message.DiscardUnknownFields()
        chapter = message.chapter_id
        if not chapter:
            raise ValueError("factory chapter missing")
        if message_id == 221:
            self._drop_chapter(chapter)
            self._chapters[chapter] = {}
            self.error = None
        elif chapter not in self._chapters:
            return
        if message_id in (221, 222):
            for node in message.nodes:
                self._replace_node(chapter, node)
            if message_id == 222:
                for node_id in message.remove_nodes:
                    self._drop_node(chapter, node_id)
        else:
            for component in message.components:
                node_id = self._components.get((chapter, component.component_id))
                if node_id is not None:
                    node = self._chapters[chapter][node_id]
                    # Component deltas replace the whole payload: an empty
                    # TravelPole message explicitly clears DefaultNext to zero.
                    for index, old in enumerate(node.components):
                        if old.component_id == component.component_id:
                            node.components[index].CopyFrom(component)
                            break
                    if not any(c.HasField("travel_pole") for c in node.components):
                        self._drop_node(chapter, node_id)
        self.updated_at = timestamp
        self.revision += 1

    def _drop_chapter(self, chapter):
        for node_id in list(self._chapters.get(chapter, {})):
            self._drop_node(chapter, node_id)
        self._chapters.pop(chapter, None)

    def _drop_node(self, chapter, node_id):
        node = self._chapters.get(chapter, {}).pop(node_id, None)
        if node is not None:
            for component in node.components:
                self._components.pop((chapter, component.component_id), None)

    def _replace_node(self, chapter, node):
        if not node.node_id:
            raise ValueError("factory node id missing")
        self._drop_node(chapter, node.node_id)
        if not any(component.HasField("travel_pole") for component in node.components):
            return
        if node.HasField("transform"):
            if node.transform.HasField("position"):
                _vector(node.transform.position)
            if node.transform.HasField("rotation"):
                _vector(node.transform.rotation)
        # Retain only travel-pole nodes, not every factory inventory/payload.
        self._chapters[chapter][node.node_id] = node
        for component in node.components:
            self._components[chapter, component.component_id] = node.node_id

    def summary(self) -> dict:
        return {
            "flow": self.flow,
            "updatedAt": self.updated_at,
            "revision": self.revision,
            "syncedChapters": sorted(self._chapters),
            "nodeCount": sum(len(nodes) for nodes in self._chapters.values()),
            "error": self.error,
        }

    def snapshot(self) -> dict:
        nodes = []
        for chapter, entries in sorted(self._chapters.items()):
            for node_id, node in sorted(entries.items()):
                social = next((c.social for c in node.components if c.HasField("social")), None)
                for component in node.components:
                    if not component.HasField("travel_pole"):
                        continue
                    transform = node.transform
                    nodes.append(
                        {
                            "chapterId": chapter,
                            "nodeId": node_id,
                            "componentId": component.component_id,
                            "templateId": node.template_id,
                            "mapId": transform.map_id if node.HasField("transform") else None,
                            "position": _vector(transform.position) if transform.HasField("position") else None,
                            "rotation": _vector(transform.rotation) if transform.HasField("rotation") else None,
                            "defaultNext": component.travel_pole.default_next,
                            "flag": node.flag,
                            "shared": bool(node.flag & 0x10),
                            "origin": "shared" if node.flag & 0x10 else "self_or_preset",
                            "isDeactive": node.is_deactive,
                            "power": {
                                "inPower": node.power.in_power,
                                "needInPower": node.power.need_in_power,
                                "powerCost": node.power.power_cost,
                                "powerCostShow": node.power.power_cost_show,
                            }
                            if node.HasField("power")
                            else None,
                            # Decimal strings keep uint64 owner IDs exact in JS.
                            "social": {
                                "ownerId": str(social.owner_id),
                                "socialNodeId": social.social_node_id,
                                "preset": social.preset,
                            }
                            if social is not None
                            else None,
                        }
                    )
        by_id = {(n["chapterId"], n["nodeId"]): n for n in nodes}
        links = []
        for node in nodes:
            if not node["defaultNext"]:
                continue
            target = by_id.get((node["chapterId"], node["defaultNext"]))
            link = {
                "chapterId": node["chapterId"],
                "sourceNodeId": node["nodeId"],
                "sourceComponentId": node["componentId"],
                "targetNodeId": node["defaultNext"],
                "targetResolved": target is not None,
                "distance3D": None,
                "yawToTarget": None,
            }
            if target and target["position"] is not None and node["position"] is not None:
                dx, dy, dz = (target["position"][axis] - node["position"][axis] for axis in "xyz")
                link.update(
                    distance3D=math.sqrt(dx * dx + dy * dy + dz * dz),
                    yawToTarget=math.degrees(math.atan2(dx, dz)) % 360 if dx or dz else None,
                )
            links.append(link)
        return {
            **self.summary(),
            "nodes": nodes,
            "defaultLinks": links,
            "defaultLinkCount": len(links),
            "resolvedTargetCount": sum(link["targetResolved"] for link in links),
        }
