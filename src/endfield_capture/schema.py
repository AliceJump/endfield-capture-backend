"""Independent Google protobuf wire schema for the recovered position messages."""
from google.protobuf import descriptor_pb2, descriptor_pool, message_factory

fd = descriptor_pb2.FileDescriptorProto(name='recovered_position.proto', package='recovered', syntax='proto3')

def message(name, fields):
    result = fd.message_type.add(name=name)
    for number, field_name, field_type, type_name, repeated in fields:
        field = result.field.add(name=field_name, number=number, type=field_type,
            label=3 if repeated else 1)
        if type_name:
            field.type_name = '.recovered.'+type_name

message('VECTOR', [(1,'x',2,None,False),(2,'y',2,None,False),(3,'z',2,None,False)])
message('MOTION_INFO', [(1,'position',11,'VECTOR',False),(2,'rotation',11,'VECTOR',False),
    (3,'speed',11,'VECTOR',False),(4,'state',5,None,False)])
message('MOVE_OBJECT_MOVE_INFO', [(1,'objid',4,None,False),(2,'motion_info',11,'MOTION_INFO',False),
    (3,'server_time',13,None,False),(4,'scene_num_id',5,None,False)])
message('CS_MOVE_OBJECT_MOVE', [(1,'move_info',11,'MOVE_OBJECT_MOVE_INFO',True),
    (2,'leader_move_tag_mask',5,None,False),(3,'leader_index',5,None,False),(4,'leader_jump_count',5,None,False)])
pool = descriptor_pool.DescriptorPool()
pool.Add(fd)
PositionMessage = message_factory.GetMessageClass(pool.FindMessageTypeByName('recovered.CS_MOVE_OBJECT_MOVE'))

def validate_positions(body, rows):
    parsed = PositionMessage.FromString(body)
    assert len(parsed.move_info) == len(rows)
    for entry,row in zip(parsed.move_info,rows,strict=True):
        assert entry.objid == row['objectId']
        assert parsed.leader_index == row['leaderIndex']
        assert entry.scene_num_id == row['sceneNumId']
        assert entry.motion_info.state == row['state']
        assert {axis:getattr(entry.motion_info.position,axis) for axis in 'xyz'} == row['position']
    return len(rows)
