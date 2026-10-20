# 工厂设施快照

`GET /api/facilities` 返回本次连接中服务器下发的**全部工厂节点**，包括滑索、电力设施、传送带、管道、
生产设备、采集设备、仓储和装饰等。按节点和组件描述解析，不维护建筑模板白名单。
与只返回滑索的 `/api/travel-poles` 并存，不需要官方地图 WS 或 ok-end-field。

```powershell
uv run --locked endfield-capture-backend serve --no-map-ws --no-dashboard
# 监听就绪后进入游戏世界，再在另一终端查询
$facilities = Invoke-RestMethod http://127.0.0.1:8765/api/facilities
$facilities.templateCounts
$facilities.nodes | Where-Object templateId -Like 'power_*'
Invoke-WebRequest http://127.0.0.1:8765/api/facilities -OutFile facilities.json
```

已进入世界时可以先开监听，再退出到游戏标题页，用同一账号继续进入。
必须捕获新连接的完整登录握手；接口不绕过客户端 DLL/metadata 指纹校验。

## 返回内容

顶层包含 `flow`、`flowStatus`、`connectionActive`、`updatedAt`、`revision`、`syncedChapters`、`error`、
`nodeCount`、`wireCount`、`templateCounts`、`nodes` 和 `wires`。
`/api/state.facilities` 只含 flow、时间、revision、区域、节点/线路数量和错误摘要；坐标轮询和 SSE 不附带完整设施表。
完整表按需查询，`--no-dashboard` 下也可用，`--no-http` 时不提供；角色坐标命名管道不传设施表。

每个节点包含：

| 字段 | 含义 |
| --- | --- |
| `chapterId`、`nodeId` | 联合节点键，不能只按 NodeId 跨区域合并 |
| `templateId`、`nodeType`、`instKey` | 服务器给出的模板、节点类型和实例键，未转换成猜测的显示名称 |
| `mapId` | 工厂 Transform 地图编号，不是角色移动的 sceneNumId |
| `position`、`rotation` | 世界坐标和建筑旋转；没有下发对应子消息时为 null，不用网格坐标补成世界坐标 |
| `transform` | 完整 Transform；`gridPosition` 为整数网格位置，`position` 为世界坐标，另含方向、Mesh 和带式传送端口 |
| `interactiveObject` | 服务器下发的场景对象 ID；没有子消息时为 null |
| `flag`、`shared`、`origin` | 原始标志及 `flag & 0x10`；非共享只标记 self_or_preset，不推断当前账号所有权 |
| `isDeactive`、`power` | 停用、供电/耗电字段；未下发 Power 子消息时为 null |
| `componentPos` | 协议中的组件槽位映射，键为十进制字符串 |
| `components` | 组件 ID、类型及已下发的类型化 payload |

组件 payload 保留已核对 metadata 中的字段，包括：

- `producer` / `formulaMan`：配方、进度和生产/阻塞标记。
- `collector` / `cache` / `inventory`：采集、缓存和物品映射。
- `portManager.ports`：端口、来源 ownerComId 和接触组件 touchComId。
- `boxConveyor` / `fluidConveyor` / 路由器、桥接器、阀门：物品、流体和连接状态。
- `powerPole` / `stablePower` / `burnPower` / `batteryBurn`：电力组件 payload。
- `soil`、水/气体采集、环境组件、战斗组件，以及 `social` 的来源和共享字段。

字段使用 protobuf JSON 约定：lowerCamelCase、所有 int64/uint64 均为十进制字符串、map 键为字符串。
存在的子消息会包含其中标量的协议默认值，未下发的子消息不伪造成空对象；真正下发的空消息会保留为 `{}`。
例如 `powerPole: {}` 说明存在电线杆组件，不表示缺少解析。
未知字段不导出为未验证的任意原始字节。

`componentStateMode` 固定为 `last_node_or_component_sync`：**生产进度、库存等是最近一次节点/组件同步里的状态**。
此接口没有运行客户端工厂模拟，也没有合并 231/232 工厂 HS 的差分事件，不能把这些值当作逐 tick 的最新库存。
`updatedAt` 是本快照最后接收的同步时间，不代表每个组件都在那一刻重报了全部字段。

## 连接

`wires` 保留工厂地图明确下发的线路，每条有：

```json
{
  "chapterId": "domain_2",
  "mapId": 85,
  "wireId": "7",
  "sourceComponentId": "100",
  "targetComponentId": "200",
  "sourceNodeId": 10,
  "targetNodeId": null,
  "sourceResolved": true,
  "targetResolved": false
}
```

线路键是 `(chapterId, mapId, wireId)`，两端首先是 **ComponentId**，通过 chapter 内的组件索引关联节点。
wireId 和端点在协议里是 uint64，输出字符串保留精度。节点删除或端点尚未下发时保留线路，节点关联为 null；
收到目标后自动补齐，不推断未解析原因。删除线路必须收到明确的 RemoveWires。

2026-10-20 样本的 730 条线路两端均为 PowerPole 组件。这些记录不能当作所有物流可达边，
传送带/管道还需要读取相应端口组件；滑索默认下一站仍读取 TravelPole.defaultNext（目标 **NodeId**），
其有向几何链接可继续使用 `/api/travel-poles.defaultLinks`。不把供电线或近距离候选冒充滑索连接。

## 生命周期与范围

| 下行消息 | 处理 |
| --- | --- |
| 221 SC_FACTORY_SYNC_CHAPTER | 替换指定 chapter 的节点和地图线路，空同步也生效 |
| 222 SC_FACTORY_MODIFY_CHAPTER_NODES | 替换/删除指定节点，清理被替换的旧组件索引 |
| 223 SC_FACTORY_MODIFY_CHAPTER_COMPONENTS | 按已索引 ComponentId 替换完整组件 payload，空 payload 恢复协议默认值 |
| 227 SC_FACTORY_MODIFY_CHAPTER_MAP | 按地图更新线路，支持 packed/unpacked uint64 RemoveWires |
| 211 / 212 RELEASE_SCOPE / MODIFY_SCOPE | 按 DeleteChapterIdList 移除对应 chapter 的节点、线路和同步基线 |

每个 chapter 必须先有 221 基线，否则忽略其节点、组件和线路增量。
222 带组件时按完整节点描述处理；完全不带组件时沿用原滑索接口的字段合并规则：保留组件表，覆盖有 presence 的子消息和
非默认标量。proto3 标量没有 presence，因此这类部分更新不能区分“未下发”与“明确置零”；节点删除由 RemoveNodes 表达。
223 的未知 ComponentId 不凭空挂到节点，需要后续节点同步建立归属。端口 ID 为零时也不推断邻居。

新 flow 或同 flow 的重新握手会清空旧快照，旧 flow 迟到消息不能覆盖新会话。
解析失败或非有限浮点值使当前设施快照整体失效（节点/线路/基线清空、error 写明消息号和异常类型），等待新基线恢复；
坐标解码继续运行。滑索投影和通用设施使用同一节点生命周期实现，各自保留快照和错误状态。
连接结束后保留最后快照供查看，connectionActive=false；调用方应检查连接状态、syncedChapters 和 error。

这里是**本次服务器已下发的工厂数据**：

- syncedChapters 不保证所有区域都已下发。无实体位置的工厂辅助节点（例如 `__inventory__`）也保留，不算作可导航建筑。
- 固定场景设施、宝箱、门、采集物等可能走 25/21 场景对象协议，不会为了凑全量混入工厂表。
- 没有还原所有建筑静态参数、工厂模拟或全部场景设施；不会把 Mesh 直接当作已验证的导航障碍。

## 验证

2026-10-20 本机从登录前开始抓包并进入试验园区，地图核对了息壤中继器。
两片区域的初始同步共有 **3700 个节点、82 种模板、730 条线路**；另收到 133 条 222 和 1 条 223。
用独立 metadata wire 解析器逐节点、逐组件核对全部已定义字段和线路关联，结果一致；
6503 个主连接 TCP 包原序、乱序/重复包回放的设施结果一致，均无剩余 TCP 缺口、帧缓冲或分片，角色坐标 selftest 通过。
211/212、227、删除、清零、异常、跨 chapter/map 同号等边界由独立合成 wire 测试覆盖；本轮未现场改线或拆建设施。
本地 PCAP、账号数据、客户端文件和密钥材料均不进入仓库。
