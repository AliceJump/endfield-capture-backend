"""Travel-pole projection over the common factory node lifecycle."""

from __future__ import annotations

import math

from .factory_nodes import FactoryNodeStore, _vector


class TravelPoleStore(FactoryNodeStore):
    def _accept_node(self, node):
        return any(component.HasField("travel_pole") for component in node.components)

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
