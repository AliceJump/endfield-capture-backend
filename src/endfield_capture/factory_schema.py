"""Typed factory wire schema; unknown fields are discarded by the snapshot store."""

from google.protobuf import descriptor_pb2, descriptor_pool, message_factory

from .factory_fields import FACTORY_FIELDS

_fd = descriptor_pb2.FileDescriptorProto(name="factory.proto", package="factory", syntax="proto3")
_SCALARS = {"int": 5, "uint": 13, "long": 3, "ulong": 4, "float": 2, "double": 1, "bool": 8, "string": 9}


def _field(message, number, name, kind, *, repeated=False):
    field = message.field.add(name=name, number=number, label=3 if repeated else 1)
    field.type = _SCALARS.get(kind, 11)
    if field.type == 11:
        field.type_name = ".factory." + kind
    return field


def _message(name, fields):
    message = _fd.message_type.add(name=name)
    for number, field_name, kind in fields:
        if kind.startswith("Map<"):
            key, value = kind[4:-1].split(",")
            entry = message.nested_type.add(name=field_name.title().replace("_", "") + "Entry")
            entry.options.map_entry = True
            _field(entry, 1, "key", key)
            _field(entry, 2, "value", value)
            _field(message, number, field_name, name + "." + entry.name, repeated=True)
        else:
            _field(message, number, field_name, kind.removesuffix("[]"), repeated=kind.endswith("[]"))


for _name, _fields in FACTORY_FIELDS.items():
    _message(_name, _fields)

_message(
    "SyncChapter", [(2, "chapter_id", "string"), (3, "nodes", "SCD_FAC_NODE[]"), (7, "maps", "SCD_FACTORY_SYNC_MAP[]")]
)
_message("ModifyNodes", [(2, "chapter_id", "string"), (3, "nodes", "SCD_FAC_NODE[]"), (4, "remove_nodes", "uint[]")])
_message("ModifyComponents", [(2, "chapter_id", "string"), (3, "components", "SCD_FAC_COM[]")])
_message("RemoveChapters", [(11, "delete_chapter_ids", "string[]")])

_pool = descriptor_pool.DescriptorPool()
_pool.Add(_fd)
FACTORY_MESSAGES = {
    mid: message_factory.GetMessageClass(_pool.FindMessageTypeByName("factory." + name))
    for mid, name in (
        (211, "RemoveChapters"),
        (212, "RemoveChapters"),
        (221, "SyncChapter"),
        (222, "ModifyNodes"),
        (223, "ModifyComponents"),
        (227, "SC_FACTORY_MODIFY_CHAPTER_MAP"),
    )
}
