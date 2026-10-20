"""Factory node lifecycle shared by facility snapshots and the travel-pole projection.

The caller holds State.lock. Deltas require a chapter baseline; parse/validation
failures invalidate only this store and never stop position decoding.
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


class FactoryNodeStore:
    def __init__(self):
        self.flow: int | None = None
        self.updated_at: float | None = None
        self.revision = 0
        self.error: str | None = None
        self._chapters: dict[str, dict] = {}
        self._components: dict[tuple[str, int], int] = {}

    def _accept_node(self, node):
        return True

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
        """解析并合并一条工厂同步/更新。

        工厂同步是解码主循环的旁路：这里的任何失败都只作废当前设施快照，
        不许把异常抛回去——``Flow.receive`` 收到异常会把整条连接标记为解码停止，
        连带角色坐标一起丢。
        """

        if flow != self.flow:
            return
        try:
            message = FACTORY_MESSAGES[message_id].FromString(body)
            message.DiscardUnknownFields()
            if message_id in (211, 212):
                for chapter in message.delete_chapter_ids:
                    self._drop_chapter(chapter)
                self.updated_at = timestamp
                self.revision += 1
                return
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
        elif message_id == 223:
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
                    if not self._accept_node(node):
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
            if node.node_type:
                stored.node_type = node.node_type
            if node.inst_key:
                stored.inst_key = node.inst_key
            if node.HasField("interactive_object"):
                stored.interactive_object.CopyFrom(node.interactive_object)
            if node.component_pos:
                stored.component_pos.clear()
                stored.component_pos.update(node.component_pos)
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
        if not self._accept_node(node):
            return
        _validate_transform(node)
        # Store a detached message: callers cannot mutate the baseline after ingestion.
        stored = type(node)()
        stored.CopyFrom(node)
        self._chapters[chapter][node.node_id] = stored
        for component in node.components:
            self._components[chapter, component.component_id] = node.node_id
