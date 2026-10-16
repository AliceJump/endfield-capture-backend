"""Minimal factory wire schema recovered from the verified client metadata.

Only fields needed for travel poles are described. Unknown gameplay fields are
discarded after parsing; no login/authentication messages use this schema.
"""

from google.protobuf import descriptor_pb2, descriptor_pool, message_factory

_fd = descriptor_pb2.FileDescriptorProto(name="travel_poles.proto", package="travel_poles", syntax="proto3")


def _message(name, fields):
    result = _fd.message_type.add(name=name)
    for number, field_name, field_type, type_name, repeated in fields:
        field = result.field.add(name=field_name, number=number, type=field_type, label=3 if repeated else 1)
        if type_name:
            field.type_name = ".travel_poles." + type_name


_message("Vector", [(1, "x", 2, None, False), (2, "y", 2, None, False), (3, "z", 2, None, False)])
_message(
    "Transform",
    [(4, "map_id", 5, None, False), (9, "position", 11, "Vector", False), (10, "rotation", 11, "Vector", False)],
)
_message(
    "Power",
    [
        (1, "in_power", 8, None, False),
        (2, "power_cost", 3, None, False),
        (11, "need_in_power", 8, None, False),
        (12, "power_cost_show", 3, None, False),
    ],
)
_message("TravelPole", [(11, "default_next", 13, None, False)])
_message(
    "Social", [(1, "owner_id", 4, None, False), (2, "social_node_id", 13, None, False), (6, "preset", 8, None, False)]
)
_message(
    "Component",
    [
        (1, "component_id", 13, None, False),
        (2, "component_type", 5, None, False),
        (24, "travel_pole", 11, "TravelPole", False),
        (44, "social", 11, "Social", False),
    ],
)
_message(
    "Node",
    [
        (1, "node_id", 13, None, False),
        (3, "template_id", 9, None, False),
        (4, "transform", 11, "Transform", False),
        (5, "is_deactive", 8, None, False),
        (8, "power", 11, "Power", False),
        (10, "flag", 13, None, False),
        (12, "components", 11, "Component", True),
    ],
)
_message("SyncChapter", [(2, "chapter_id", 9, None, False), (3, "nodes", 11, "Node", True)])
_message(
    "ModifyNodes",
    [(2, "chapter_id", 9, None, False), (3, "nodes", 11, "Node", True), (4, "remove_nodes", 13, None, True)],
)
_message("ModifyComponents", [(2, "chapter_id", 9, None, False), (3, "components", 11, "Component", True)])

_pool = descriptor_pool.DescriptorPool()
_pool.Add(_fd)
FACTORY_MESSAGES = {
    mid: message_factory.GetMessageClass(_pool.FindMessageTypeByName("travel_poles." + name))
    for mid, name in ((221, "SyncChapter"), (222, "ModifyNodes"), (223, "ModifyComponents"))
}
