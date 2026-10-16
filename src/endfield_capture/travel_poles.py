"""Factory travel-pole snapshots and directed configured next-stop references.

The caller holds State.lock. Each chapter needs a complete 221 baseline before
222/223 deltas are accepted; every authenticated connection starts a new store.
A 222 node payload is a complete node description, except that a payload
carrying no components at all is merged into the stored node. Any parse or
validation failure drops the whole snapshot instead of exporting a half-updated
one, and never propagates to the decoder.
"""

from __future__ import annotations

import math

from .factory_schema import FACTORY_MESSAGES


def _vector(vector):
    values = {axis: getattr(vector, axis) for axis in "xyz"}
    if not all(math.isfinite(value) for value in values.values()):
        raise ValueError("non-finite factory transform")
    return values


def _validate_transform(node) -> None:
    """Transform 可以缺失或为空；出现非有限坐标时整条消息作废。"""

    if not node.HasField("transform"):
        return
    if node.transform.HasField("position"):
        _vector(node.transform.position)
    if node.transform.HasField("rotation"):
        _vector(node.transform.rotation)


class TravelPoleStore:
    def __init__(self):
        self.flow: int | None = None
        self.updated_at: float | None = None
        self.revision = 0
        self.error: str | None = None
        self._chapters: dict[str, dict] = {}
        self._components: dict[tuple[str, int], int] = {}

    def begin_session(self, flow: int) -> None:
        """开始新会话并清空上一会话的快照；旧 flow 的迟到握手忽略。

        同一条连接重新握手（flow 相同）也算新会话——重连后仍沿用旧 flow 号，
        不在这里清空的话，上一账号/上一会话的节点会混进新会话。
        """

        if self.flow is not None and flow < self.flow:
            return
        self.flow = flow
        self._clear()

    def _clear(self):
        self._chapters.clear()
        self._components.clear()
        self.updated_at = None
        self.error = None
        self.revision += 1

    def invalidate(self, flow: int, message_id: int, exc: BaseException | None = None) -> None:
        """作废整份快照。调用方只能看到"整份可信/整份作废"，不输出半份工厂数据。"""

        if flow == self.flow:
            self._clear()
            # Do not put message bodies or account identifiers into diagnostics.
            reason = type(exc).__name__ if exc is not None else "unknown error"
            self.error = f"invalid factory message {message_id} ({reason}); waiting for full chapter sync"

    def apply(self, flow: int, message_id: int, body: bytes, timestamp: float) -> None:
        """解析并合并一条 221/222/223。

        221/222/223 是解码主循环的旁路：这里的任何失败都只作废滑索快照，
        不许把异常抛回去——``Flow.receive`` 收到异常会把整条连接标记为解码停止，
        连带角色坐标一起丢。
        """

        if flow != self.flow:
            return
        try:
            message = FACTORY_MESSAGES[message_id].FromString(body)
            message.DiscardUnknownFields()
            chapter = message.chapter_id
            if not chapter:
                raise ValueError("factory chapter missing")
            self._ingest(message_id, chapter, message, timestamp)
        except Exception as exc:
            self.invalidate(flow, message_id, exc)

    def _ingest(self, message_id: int, chapter: str, message, timestamp: float) -> None:
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
                # 索引只覆盖 221/222 节点 payload 里出现过的组件。之后才新挂上去的
                # ComponentId 无从归属到节点，只能等下一次完整同步。
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
        stored = self._chapters.get(chapter, {}).get(node.node_id)
        if stored is not None and not node.components:
            # 222 只带节点字段、不带任何组件时按部分更新处理：组件表保持原样，
            # 只覆盖明确出现的字段。否则一次不带组件的节点更新就会让滑索连同
            # 指向它的默认下一站一起从快照里消失。
            # 注意 proto3 标量没有 presence：值为 0/空串时无法区分"没下发"和
            # "下发为 0"，这类字段只在非零时覆盖。删除仍由 RemoveNodes 表达。
            if node.template_id:
                stored.template_id = node.template_id
            if node.HasField("transform"):
                stored.transform.CopyFrom(node.transform)
            if node.HasField("power"):
                stored.power.CopyFrom(node.power)
            if node.flag:
                stored.flag = node.flag
            if node.is_deactive:
                stored.is_deactive = True
            _validate_transform(stored)
            return
        self._drop_node(chapter, node.node_id)
        if not any(component.HasField("travel_pole") for component in node.components):
            return
        _validate_transform(node)
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
