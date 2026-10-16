# 滑索节点与默认下一站

启动后端并完整捕获一次登录后，`GET /api/travel-poles` 返回本次会话里服务器实际下发的工厂滑索架。
普通、长距、无需供电等类型由 `templateId` 区分；解析以组件为准，不靠模板名字白名单。
不需要启用官方地图 WS，也不依赖 ok-end-field。

```powershell
uv run --locked endfield-capture-backend serve --game-dir 'D:/Games/Endfield Game' --no-map-ws
# 后端开始监听后再登录；以下请求在另一终端执行
Invoke-RestMethod http://127.0.0.1:8765/api/travel-poles
Invoke-WebRequest http://127.0.0.1:8765/api/travel-poles -OutFile travel-poles.json
```

游戏已经进入世界时，可保持后端监听，游戏内退出到标题页，再用同一账号继续进入世界。
2026-10-16 的当前客户端实测会产生新 TCP 连接并补齐握手，无需结束游戏进程。
普通切图或开关菜单不保证产生新握手。`flows[]` 的新连接应进入“正在解码”；只有后端运行着还不够。
客户端文件指纹检查仍然生效，未知版本不会因为增加此接口而绕过校验。

## 返回值

```json
{
  "flow": 1,
  "flowStatus": "正在解码",
  "connectionActive": true,
  "updatedAt": 1700000000.0,
  "revision": 3,
  "syncedChapters": ["domain_2"],
  "nodeCount": 1,
  "error": null,
  "nodes": [{
    "chapterId": "domain_2",
    "nodeId": 10,
    "componentId": 100,
    "templateId": "travel_pole_2",
    "mapId": 85,
    "position": {"x": 1.0, "y": 2.0, "z": 3.0},
    "rotation": {"x": 0.0, "y": 270.0, "z": 0.0},
    "defaultNext": 20,
    "flag": 16,
    "shared": true,
    "origin": "shared",
    "isDeactive": false,
    "power": null,
    "social": {"ownerId": "9007199254740993", "socialNodeId": 12, "preset": false}
  }],
  "defaultLinks": [{
    "chapterId": "domain_2",
    "sourceNodeId": 10,
    "sourceComponentId": 100,
    "targetNodeId": 20,
    "targetResolved": false,
    "distance3D": null,
    "yawToTarget": null
  }],
  "defaultLinkCount": 1,
  "resolvedTargetCount": 0
}
```

- 节点键是 `(chapterId, nodeId)`，组件索引也是按 chapter 隔离；不要混用 NodeId 和 ComponentId。
- `defaultNext` 是**目标 NodeId**；零表示没有配置默认下一站。它是有向单一引用，不是距离候选或全部可达邻居，A→B 不表示 B→A。
- 默认目标没有下发、或之后被删除时，引用仍保留，`targetResolved=false`；收到目标节点后自动补齐。
- `position` 是建筑的世界坐标，`y` 是高度；`rotation.y` 是建筑朝向，不能用作镜头朝向。
  未收到世界坐标/旋转时是 `null`，不拿整数网格坐标或零坐标猜测。
- `distance3D` 和 `yawToTarget` 根据两端建筑坐标计算，yaw 为 `atan2(dx,dz)` 转成 [0,360) 度；
  没有两端坐标时为 `null`，水平位置重合时没有 yaw。它们不含客户端挂点偏移，不等于精确屏幕对中角。
- `mapId` 是工厂 Transform 的数字地图编号，不是角色移动消息的 `sceneNumId`，也不是 `map01/map02` 字符串。
- `flag & 0x10` 表示共享节点；没有共享标记只标记为 `self_or_preset`，不据此认定由当前账号建造。
- `social` 在收到相应组件时存在，`ownerId` 用十进制字符串保留 uint64 精度；零表示协议缺省值，不能视为已确认所有者。
- `power` 在消息有该子消息时存在，包含 `inPower`、`needInPower`、`powerCost`、`powerCostShow`；`null` 不等于已确认未供电。

## 完整性和生命周期

`/api/state` 只增加 `travelPoles` 摘要（flow、updatedAt、revision、syncedChapters、nodeCount、error），
避免高频坐标轮询和 SSE 携带整份建筑数据。完整列表按需请求 `/api/travel-poles`；`--no-dashboard` 下也可用。
该数据不通过角色坐标命名管道发送，`--no-http` 时没有滑索查询接口。

每个 chapter 必须先收到一次 **221** 完整同步；更早收到的 **222/223** 被忽略，避免把不完整增量冒充全量。
221 替换该 chapter，222 替换/删除指定节点，223 按来源 ComponentId 替换组件 payload；空 TravelPole payload 会把默认下一站清零。
新连接握手成功时清空上一会话的快照；旧 flow 的迟到消息不能重新写入。新账号未完成同步时，不会显示上一账号节点。

`syncedChapters` 表示本会话已经观察到完整同步的区域，**不保证所有区域都已下发**。空列表表示尚无基线；
列表非空但 `nodes=[]` 可以表示已同步区域没有工厂滑索。固定场景预定义设施可能走其他协议，不包含在此列表。
服务端未下发的建筑、全服建筑和静态设备参数不在本接口范围内。

连接结束后保留最后快照供离线查看，但 `connectionActive=false`；调用方应同时检查 `flowStatus`、
`syncedChapters`、`error` 和 `updatedAt`。滑索更新是事件驱动，长时间没有更新不等于故障。
收到无法解析的工厂消息会清空滑索快照并设置 `error`，等下次完整同步恢复，不影响角色坐标解码。

## 协议依据和验证边界

字段来自已通过后端双哈希校验的客户端 metadata；只定义所需最小 schema，未知字段不输出：

| 消息/结构 | 字段 |
| --- | --- |
| 221 SC_FACTORY_SYNC_CHAPTER | ChapterId F2、Nodes F3 |
| 222 SC_FACTORY_MODIFY_CHAPTER_NODES | ChapterId F2、Nodes F3、RemoveNodes F4（packed/unpacked） |
| 223 SC_FACTORY_MODIFY_CHAPTER_COMPONENTS | ChapterId F2、Components F3 |
| SCD_FAC_NODE | NodeId F1、TemplateId F3、Transform F4、IsDeactive F5、Power F8、Flag F10、Components F12 |
| SCD_FAC_TRANSFORM | MapId F4、WorldPosition F9、WorldRotation F10 |
| SCD_FAC_COM | ComponentId F1、ComponentType F2、TravelPole F24、SocialValue F44 |
| SCD_FAC_COM_TRAVEL_POLE | DefaultNext F11 uint32 |
| SCD_FAC_COM_SOCIAL | OwnerId F1 uint64、SocialNodeId F2、Preset F6 |

当前版本 2026-10-16 真实退出重进抓包包含 224 个滑索架和 49 条默认链接（48 个目标可解析）。
完整 PCAP 原序/乱序加重复包回放，并与独立历史解析器的逐节点结果对比。
222/223 更新、删除、清零、跨 chapter 同号、账号切换和异常报文由人工构造 wire 样本测试；
这次真实抓包只有 221，未现场设置默认链接或验证完整乘坐路线。测试不提交账号数据、PCAP 或密钥材料。
