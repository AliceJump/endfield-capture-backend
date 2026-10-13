# 与 ok-end-field 协作：怎么调用这个后端

本文说明 `endfield-capture-backend`（本仓库）与 [ok-end-field](https://github.com/AliceJump/ok-end-field)
之间的调用约定。两边的实现都已经就位，这里记录的是**契约、顺序与边界**。

- 后端侧：打包成 `dist/endfield-capture-backend.exe`（见 [../packaging/README.md](../packaging/README.md)）
- 应用侧：`src/localization/capture_position_source.py`（只读后端）与
  `src/localization/capture_backend_launcher.py`（把后端拉起来）

## 职责边界

| | endfield-capture-backend | ok-end-field |
|---|---|---|
| 依赖 | scapy / pycryptodome / lz4 / protobuf + **Npcap 驱动** | 无新增依赖（只用标准库 `urllib`） |
| 干什么 | 被动抓包、离线解密、解出世界坐标 | 读坐标、翻成定位契约、喂给导航 |
| 不干什么 | 不做导航、不碰游戏进程 | 不抓包、不解密 |

应用是打包发布的，把它拉进来会让每个用户都背上抓包依赖和 Npcap 安装负担，所以后端
**必须单独运行**。应用侧只消费结果。

## 启动顺序（最容易搞错的一点）

**后端必须先于游戏的主连接就绪。**

游戏会话的密钥/nonce 只在连接开头出现一次（登录握手）。后端中途才启动就拿不到它，
于是整条流都推不出正确的解密位置——它会明确报「缺少连接起点，请重新启动游戏」，
而**不会**解出看似合理实则错位的数据。

正确顺序：

```
后端启动  →  游戏启动并登录  →  应用（ok-end-field）开始消费坐标
```

### 为什么后端不能由"定位任务"来拉起

看起来最自然的做法是在定位任务里拉起后端，但**那永远太晚**：

ok 框架的触发任务要等到**游戏窗口可以截屏**才会被执行（`TaskExecutor.execute()` 先要
`next_frame()`，而 `can_capture()` 要求设备已连上并能抓帧）。等 `MinimapPositionTask.run()`
跑起来，玩家早就登录完了。

所以应用把拉起挂在了**"启动游戏"这个动作之前**（`src/patches/capture_backend_autostart_patch.py`
包装 `StartController.do_start`）——那是游戏进程存在之前的最后时机。定位任务里那次调用只作兜底
（后端中途挂了，或玩家在游戏已经跑起来之后才打开开关）。

三种可行走法：

1. **让应用自己拉起**（默认，推荐）：在 ok-end-field 里勾上「使用实验技术」，然后**由应用点
   「启动游戏」**。关闭游戏后重新启用定位时，后端也会被重新拉起。
2. **手动先开后端**，再开游戏，再开应用。应用发现端口已在应答就直接采用，不会重复启动。
3. **游戏已经跑起来了**：用后端的 `--seed-pcap` 指向一份从登录前开始的完整抓包，
   先把状态接上再继续抓新包。被重放的样本会标 `replayed`，不计入在线延迟统计。

> **唯一覆盖不到的**：玩家自己先开游戏、再开 ok-end-field。这时后端一定晚于登录握手，
> 只能重开游戏或用上面第 3 条。

## 传输方式

### 主用：HTTP `GET /api/state`

应用轮询 `http://127.0.0.1:<端口>/api/state`（默认端口 **8765**）。这是唯一的必需接口。

后端把它绑在回环地址上。响应里应用真正读的是这些字段：

| 字段 | 含义 |
|---|---|
| `lastPosition.x` / `.y` / `.z` | 世界坐标。`y` 是**真实高度**（里程计那条路给不出） |
| `lastPosition.yaw` | 朝向（度，正北 0、顺时针）。与 `src/nav/route_follower.bearing_to_point` 同一套约定，直接用 |
| `lastPosition.speed` | 速度模长（世界单位/秒）。用来判静止，**不需要**从位移反推 |
| `lastPosition.time` | 抓包时刻的 **Unix 墙钟**。应用用它算陈旧度，所以必须用同一时钟相减 |
| `lastPosition.scene` | `sceneNumId`（= 客户端 `LevelBasicInfoTable.idNum`） |
| `lastPosition.levelId` / `.levelName` | 场景表查出的关卡。**`levelId` 非空 ⟺ 这个场景在表里** |
| `lastPosition.mapId` / `.mapName` | 场景表查出的所属大地图 |
| `lastPosition.dungeonName` | 副本名 |
| `ws.lastPosition.mapId` | 可选的官方地图 WS 对照；仅在场景表给不出地图时当兜底 |

`lastPosition` 为 `null` 表示后端已连上但还没解出坐标（多半是抓包启动太晚，或玩家还没动）。

### 备用：Windows 命名管道

不想占端口就换这条：后端起 `serve --pipe --no-http`，应用侧用 `QLocalSocket` 直连
`\\.\pipe\endfield-capture-positions`。

协议是**换行分隔的 UTF-8 JSON，每行一个对象、带 `type`**：

```json
{"type": "hello", "protocol": 1, "pid": 1234, "pipe": "\\\\.\\pipe\\endfield-capture-positions", "runDirectory": "runs/..."}
{"type": "position", "time": 1791870088.9, "scene": 361, "levelId": "map02_lv009", "levelName": "雪松林",
 "mapId": "map02", "mapName": "武陵", "x": 172.04, "y": 105.25, "z": 47.89, "yaw": 308.8, "speed": 5.75,
 "state": 3, "merged": false}
```

`position` 行的字段与 `lastPosition` **完全一致**，消费方换传输不用改解析。连上先给 `hello`
再补一条当前最新坐标，然后是实时流；没有客户端时不排队（只留最新一条），写不过来时丢新样本
而不阻塞解码。

> 应用侧目前实现的是 HTTP 轮询那条（`CapturePositionSource`）。管道被后端支持并有测试钉着，
> 但应用侧还没接——要用的话需要新增一个 `QLocalSocket` 数据源。

## 应用侧配置

都在「小地图定位」任务的实验技术分组下（打开「使用实验技术」后出现）：

| 配置 | 默认 | 说明 |
|---|---|---|
| 使用实验技术 | 关 | 总开关。打开需过一遍风险确认 |
| 实验技术后端端口 | 8765 | 探活与拉起都用它 |
| 实验技术后端程序路径 | 空 | exe 路径，也可以填放着它的目录。**留空则自动探测** |
| 实验技术自动启动后端 | 开 | 后端没在跑时自动拉起 |
| 实验技术停止定位时关闭后端 | **关** | 见下 |

自动探测的顺序：配置 → 环境变量 `EF_CAPTURE_BACKEND` → 同级
`endfield-capture-backend/dist/` → 同级 `endfield-capture-backend/` → 本仓库目录 → `PATH`。

拉起时的参数固定为 `serve --no-dashboard --no-map-ws --port <端口>`：

- `--no-dashboard`：应用只读 `/api/*`，不需要网页看板；
- `--no-map-ws`：官方地图 WS 对照要 ok-end-field 加 ok-script 的依赖，冻结的后端带不了；
  而地图名本来就走后端包内的场景表，够用。

拉起的时机是**「启动游戏」之前**（见上文），不是由定位任务触发。

### 为什么默认「停止定位时关闭后端」是关的

后端的全部状态都绑在**当前这一局连接**上。关掉再拉起就等于丢了登录握手，只能重启游戏才能
恢复。而 ok-end-field 的任务会被频繁启停，默认停后端会变成一个很讨厌的失败模式。

所以默认不动它——后端会一直活到你自己关掉（任务管理器，或打开上面那个开关让它跟任务一起退）。

### 拉起器的几条边界

- **只拉起、不接管**：已经在跑的后端（用户自己起的）直接采用，绝不重复启动；
- **只停自己起的**：`stop()` 不会去终止不是本应用拉起的进程；
- **同一个端口只有一份拉起器**（进程内共享）：游戏启动前的自动拉起与定位任务兜底用的是同一个
  实例，否则探活会重复、`stop()` 还会因为"不是我起的"拒绝收尾；
- 拉起失败会进入 15 秒冷却，不会每个采样周期都去起一次进程；
- 起不来时把后端输出写进 `logs/capture_backend/backend-<端口>.log`（窗口用
  `CREATE_NO_WINDOW` 藏掉），并明确报出「找不到程序 / 起来就退 / 不应答」是哪一种；
- **拉起失败不阻断游戏启动**——补丁把整个调用包在 try/except 里，起不来只是没有坐标。

后端被拉起时的工作目录是 **exe 所在的目录**，所以它的运行产物（`runs/<时间戳>/` 里的
`capture.pcap`、`positions.jsonl`）会落在 exe 旁边。要换地方用后端的 `--runs-dir`。

> **onefile 是两层进程**：PyInstaller 打出来的 exe 有一个外层 bootloader 和一个里层真正跑
> Python 的进程。只 `terminate()` 外层会留下里层继续占着端口——所以 `stop()` 在 Windows 上走
> `taskkill /F /T` 收整棵树。这条有回归测试钉着。

## 坐标语义与已知限制（消费方必须处理）

- **上报是事件驱动的，不是周期性的**：移动中约每 0.27 秒一条；**完全静止时没有位置包**。
  所以消费方必须保存「最后已知坐标 + 时间戳」，并按陈旧度处理。
- **同图传送会造成数秒到数十秒的空档**，此时沿用旧坐标会偏出很远。
  `CaptureSample.age_seconds` 就是给你做这个判断的（后端记录的 Unix 墙钟减去当前墙钟）。
  注意**必须用墙钟**：曾经用任务单调钟去减 Unix 时间戳，差值恒为负被夹成 0，让这个安全提示
  完全失效——`tests/TestCapturePositionSource.py` 里有这条的回归测试。
- **服务器权威性未验证**：给出的是「客户端在网络上说了什么」，不是「服务器最终裁定你在哪」。
- **客户端版本强绑定**：后端按已核对的客户端哈希写死了函数 RVA 与静态材料位置。哈希不符时
  它**拒绝启动**并打印实际哈希。客户端每次更新都要重新核对并重跑
  `tools/generate_rsa_modulus.py`。

## 地图名怎么定

`sceneNumId` **不是**地图的一一对应键——实测同一个地图可以有多个场景号，而且行走途中就会变。
应用侧的判定顺序（`CapturePositionSource._resolve_map_id`）刻意做成"不猜"：

1. 场景在表里且有大地图 → **用场景表**。这是客户端表给的答案，不依赖 WS；
2. 场景在表里但**没有**大地图（试炼场 `blackbox_*`、独立场景 `indie_*` 等）→ 看 WS 的实时
   观测；没有就**返回空串**，让导航明确报「没有网格」。**绝不按坐标范围去猜**——这些场景有
   自己的坐标系，万一落在某张大地图的外接矩形里，猜出来的地图会让导航加载错网格、照着错
   坐标规划路线；
3. 场景不在表里（客户端更新后新增的场景）→ WS → 手写兜底表 → 按坐标范围推断。

## 故障排查

| 现象 | 原因 / 处理 |
|---|---|
| 应用日志「连接不到抓包后端」 | 后端没起。检查自动启动是否被关、路径配置是否正确、后端日志里的报错 |
| 后端日志「缺少 Npcap 驱动」 | 没装 Npcap，或安装时没勾 "WinPcap API-compatible Mode" |
| 后端起来就退（返回码非 0） | 多半是 Npcap 缺失或 `--game-dir` 指错；看 `logs/capture_backend/` 下的日志 |
| `缺少连接起点，请重新启动游戏` | 抓包晚于登录握手。重开游戏，或用 `--seed-pcap` 接管 |
| 「还没有解出角色坐标」 | 后端在跑但没数据：玩家还没动，或抓包启动太晚 |
| 「不属于任何大地图，没有对应的导航网格」 | **这是正确结果**：该场景本就不属于任何大地图。不是故障 |
| 坐标看起来是旧的 | 后端上报是事件驱动的，静止时没有新包。用 `age_seconds` 判断 |

## 部署清单

目标机器上需要：

1. `endfield-capture-backend.exe`（放在 ok-end-field 同级目录即可被自动探测到，或指定路径）
2. **Npcap 驱动**（安装时勾 "WinPcap API-compatible Mode"）
3. 本机安装的终末地客户端（`GameAssembly.dll` + `global-metadata.dat`）
4. ok-end-field 里打开「使用实验技术」并过一遍风险确认

exe 自带场景表与公钥模数数据，不需要额外的数据文件；也不需要在那台机器上装 Python。
