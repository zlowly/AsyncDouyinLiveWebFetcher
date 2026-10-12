# AsyncDouyinLiveWebFetcher 分阶段重构计划

> 状态：待审阅
> 基线：`4814b2a`（2026-10-12）· 数据实测于 2026-10-12
> 前置文档：`douyin-log-sqlite.md`（SQLite 事件库方案，本文的 F1–F10 沿用其编号）。
> 该文档尚未入库，路径：`~/.opencode/plan/douyin-log-sqlite.md`。
> 代码注释里提到的 `PLAN-sqlite-split.md` 是它的旧名。

---

## 1. 目标与原则

### 1.1 目标

1. **先止血**：修掉当前已经在造成静默数据损坏/丢失的三个口子（Phase 0）。
2. **再简化**：砍掉"protobuf → JSON 字符串 → 日志文件 → 再解析回 dict"这条回环（Phase 2），
   它是当前一半复杂度的来源。
3. **保安全网**：在任何结构性改动之前，把承载数据正确性承诺的纯函数用测试冻结（Phase 1）。
4. **后拆模块**：有测试兜底之后再做模块拆分与去重（Phase 3）。
5. **最后做运行时与运维加固**（Phase 4 / 5）。

### 1.2 原则

- **先测后改**：Phase 3+ 的每项改动都必须有 Phase 1 的测试作为回归网。
- **一次一个阶段**：每个阶段独立提交、独立可回滚，不做跨阶段的大 diff。
- **不动生成代码**：`src/protobuf/douyin.py` 仍由 protoc 生成，不手改。
- **口径零漂移**：DDL / 路由 / 投影规则只有一份定义，迁移脚本、实时写、离线导入共用。
- **不改变已入库数据**：Phase 0 的迁移以"对账 + 快照 + 大事务"为准绳，出错即回滚。

### 1.3 明确不做（本轮 out of scope）

- 不引入 Web 框架 / 前端 / HTTP API。
- 不换 SQLite（`douyin-log-sqlite.md` §3.1 已论证 DuckDB 硬阻塞）。
- 不重写 protobuf 生成流程。
- 不做 LikeMessage 的存储聚合（见 §9 待决项 D1，需要先确认分析口径）。
- 不改 `ass.py` 的字幕排版逻辑（只搬位置）。

---

## 2. 本轮新增的实测发现（R1–R14）

编号沿用既有风格，接在 `douyin-log-sqlite.md` 的 F1–F10 之后。

### R1 ★ FTS 会静默分裂（已在副本上复现）

用当前 `store.SqliteEventStore` 打开**仍为单表结构**的库，`__init__` 的
`executescript(SCHEMA)`（`store.py:546`）会：

```
ev_* 表：CREATE TABLE IF NOT EXISTS  → 10 张全部建出
ev_chat_ai/ad/au 触发器            → 全部建出
chat_fts：CREATE VIRTUAL TABLE IF NOT EXISTS → 因已存在被跳过
           仍绑定 content='events'（旧表）
```

实测结果：向 `ev_chat` 插入一行**成功、不报错**，`chat_fts MATCH 'hi'` 命中 **0**，
`integrity-check` 通过。即：**新弹幕从此不进全文索引，零告警**。

### R2 ★ 迁移脚本会被自己的前置检查锁死

`migrate_split_events.py:226 _check_preconditions` 见到任意 `ev_*` 表存在即
`sys.exit("已存在（上一次迁移未完成？先恢复快照）")`。所以一旦 R1 发生，
正规路径已堵死，只能手工 `DROP` 空表才能恢复。

### R3 正式库仍是单表结构

`data/events.db` = 171 MB / 118,334 行 / freelist=0，表为 `events` +
`events_ai|ad|au` + `chat_fts(content='events')`，**`ev_*` 一张都没有**。
说明分表迁移尚未在正式库执行，且 fetcher 也未用新 store 写过它（否则 R1 会发生）。
→ **窗口仍然开着，现在迁移是干净的。**

### R4 `ip_cache.py` 已有守卫范式可抄

```
$ python src/ip_cache.py status
中止：…/events.db 缺少分表 ['ev_chat', …, 'ip_cache']，请先跑 migrate_split_events.py
```
守卫写在 CLI 层。`store.__init__` 缺同级守卫，这是 R1 的直接成因。

### R5 ★ `SqliteLogHandler.dropped` 只写不读

`store.py:735` 初始化、`store.py:748` 自增，**全仓库无任何读取点**（grep 确认）。
队列满 100k 时静默丢记录，既不打日志也不进 stats，更不影响退出码。

### R6 `stats-*.log` 的 message 是 Python repr

```json
{"timestamp": "…", "name": "stat_949058653013",
 "message": "{'method': 'WebcastChatMessage', 'payLevel': 31, 'content': '…'}"}
```
单引号、非 JSON → `store.extract_payload` 完全解析不了。且类型与库不一致
（`totalUserCount: '1125'` 字符串 vs 库里 int）。这是 `douyin-log-sqlite.md` F7 所说的
"stats 流与 DEBUG 流重复"，但它至今仍在双写。

### R7 同一常量复制 3 份 / 2 份 / 3 份

| 常量 | 位置 |
|---|---|
| `BASE_FIELDS` | `store.py:27`、`logging_config.py:14`、`src/scripts/compact_logs.py:34` |
| `VALID_SINKS` | `config.py:42`（frozenset）、`main.py:25`（tuple） |
| 日志目录 / 库路径 | `config.py:38-39`、`store.py:29`、`logging_config.py:9` |

### R8 跨模块调用私有函数

`import_logs.py:129-130` 直接用 `store._int` / `store._dig`。

### R9 零测试 / 零 CI

`find` 确认无 `test_*.py` / `conftest.py` / `pytest.ini` / `.github`。
`ruff check src/` 通过、`ruff format --check` 通过（20 files already formatted），
但无任何自动化执行点。

### R10 LikeMessage 占 58% 的行

`events` 表 method 分布：Like 69,172 / 118,334 = 58.5%，而有效载荷只有两个整数。

### R11 每次重连都重建全部 HTTP 状态

`session.py:156 _connect_once` → `DoyinLiveRoom.new()` 每次新建 `ClientSession`
+ 2 次 GET（`live.douyin.com/` 换 nonce、`live.douyin.com/<rid>` 正则抓 roomId）。
`new()` 已支持注入 `session=`，调用方未用。

### R12 断线后无退避

`session.py:100-104`：连接一关立即进入下一轮；仅 `ClientError` 走固定 5s
（`session.py:91`），`InvalidWebridError` 才走 `_invalid_backoff` 阶梯（5/15/30）。
服务端不可用时 = 每 ~5s 一次紧循环 × N 个房间。

### R13 第 3 次 Ctrl-C 不退出

`main.py:63-64` 只打印"程序即将退出"，不 `os._exit`。
另有模块级可变全局 `shutdown_event` / `start_time` / `shutdown_count`（`main.py:20-22`）。

### R14 依赖与架构债

- `requirements.txt` 含 `requests`，全仓库零 import。
- `SqliteLogHandler` 手搓 `queue.Queue` + 线程，未用 stdlib 的 `QueueHandler`/`QueueListener`。
- `_run` 用 `time.sleep(0.02)` 轮询空队列。

---

## 3. 阶段总览

| 阶段 | 主题 | 改动面 | 风险 | 是否可独立发布 | 依赖 |
|---|---|---|---|---|---|
| **P0** | 数据止血 | `store.py` + 一次性迁移 | 低（迁移本身需演练） | 是 | — |
| **P1** | 安全网 | 新增 `tests/`，不动生产代码 | 极低 | 是 | — |
| **P2** | payload 直连 | `websocket.py` `logging_config.py` `store.py` | **中** | 是（需开关） | P1 |
| **P3** | 拆分与去重 | `store.py` → 4 模块，`websocket.py`，常量 | 中 | 是 | P1（+P2 更稳） |
| **P4** | 运行时健壮性 | `session.py` `liveroom.py` `main.py` | 中 | 是 | P1 |
| **P5** | 工具与运维 | 新增 `tools/`，改 `ntfy.py` 等 | 低 | 是 | P3 |

依赖图：

```
P0 ──────────────────────────────────────────────┐
P1 ──→ P2 ──→ P3 ──→ P5                          │
 └──→ P4 ─────────────────────────────────────────┘
```

P0/P1/P4 之间无耦合，可并行推进。

---

## 4. Phase 0 — 数据止血（最高优先级，先做）

### 0.1 `store.py` 启动守卫（修 R1）

在 `SqliteEventStore.__init__` 建表**之前**检查：若存在 legacy `events` 表且不存在
`ev_chat`，则抛错并给出可执行指引。范式抄 `ip_cache.py`。

```
检测到旧单表 events（118,334 行）且缺少 ev_chat 分表。
请先执行：python src/migrate_split_events.py --db <path>
（务必先在副本上演练；详见 douyin-log-sqlite.md 与本计划 §4.3）
```

> 为何不用"自动 DROP 空 ev_* 表后继续"：自动修复会掩盖问题，且若 `ev_*` 里已
> 有真实数据，DROP 是灾难性操作。宁可拒绝启动。

### 0.2 暴露 `dropped`（修 R5）

- `emit()` 里每累计丢弃 1000 条打一条 `WARNING`（去抖，避免刷屏）。
- `close()` / `atexit` 时把 `dropped` 总数写进 stats 与控制台。
- `main.py` 退出前打印最终 `dropped`，非 0 时把退出码语义写进文档。

> 顺带：把队列里已经 drop 掉的事实从"隐含"变成"可观测"。这一条改动极小、
> 收益直接，建议与 0.1 同一个提交。

### 0.3 执行迁移并下线迁移脚本（修 R2/R3）

步骤（**不可跳步**）：

1. `cp data/events.db /tmp/opencode/events-copy.db`（已占用 1 步磁盘）。
2. `python src/migrate_split_events.py --db /tmp/opencode/events-copy.db --vacuum`
   → 核对逐表行数、`_reconcile` 通过、`chat_fts` 已重绑到 `ev_chat`：
   ```sql
   SELECT sql FROM sqlite_master WHERE name='chat_fts';   -- content='ev_chat'
   SELECT count(*) FROM chat_fts WHERE chat_fts MATCH '打call';  -- > 0
   INSERT INTO chat_fts(chat_fts) VALUES('integrity-check');
   ```
3. 确认无写者（停 fetcher），对**正式库**跑同一命令。
4. `--vacuum` 会回收 DROP TABLE 释放的空间（需双倍磁盘，171 MB×2 无压力）。
   建议跑：迁移后 `freelist` 会很大，不 VACUUM 磁盘占用不会下降。
5. 验证 `python src/ip_cache.py status` 正常输出。
6. `git rm src/migrate_split_events.py` —— 它是一次性工具，留着只会在下次
   有人误跑时造成二次事故。迁移的"设计依据"保留在本计划 §4.3 与既有文档中。

### 0.4 新增库自检命令

`python src/doctor.py`：一次性检查并打印

- schema 版本（引入 `PRAGMA user_version`，见 P1.4）、各 `ev_*` 行数、
- `chat_fts` 的 `content=` 绑定目标（**R1 的直接检测项**）
- `room_id=''` 的行数占比（R3 归属健康度）
- `imported_sessions` 与实际 app 日志的差集
- DB 体积 / freelist / WAL 大小

它是以后所有存储层改动的第一道验证工具，比"跑一次看看"可靠。

### 0.5 DoD

- [ ] 用旧库启动 fetcher 会明确报错退出，不会写出半损坏的 FTS 索引
- [ ] 丢弃事件在控制台与 stats 中可见
- [ ] 正式库完成迁移，`ev_*` 行数合计 = 118,334，`chat_fts` 绑定 `ev_chat`
- [ ] `ip_cache.py status` / `pending` / `user` 正常
- [ ] `migrate_split_events.py` 已删除

---

## 5. Phase 1 — 安全网（与 P0 并行）

### 1.1 特征测试（characterization tests，不是"改成正确"）

先冻结**现状**行为，避免重构时误伤。以下全是纯函数，无 I/O：

| 目标 | 用例要点 |
|---|---|
| `store.project` | 每种 method 投影出的列集合；未知 method → `ev_other`；缺字段 → NULL 而非 `"null"` 字符串 |
| `store.extract_payload` | 三种载体（message JSON / flattened / LogRecord）各自的返回值 |
| `store.room_id_from_record` | extra → logger 名 → 正文前缀 三级优先级与各自正则边界 |
| `store._ranks` | 长度恒 3、`rank` 缺失时取下标 +1 |
| `store.parse_timestamp` | 三种格式 + 失败返回 None |
| `ntfy.parse_ntfy_message` | start / end / 无法识别 |
| `whitelist.match_whitelist` | 子串匹配语义（含误命中风险用例） |
| `liveroom._parse_cookie_header` / `_domain_cookies` | 值含 `=`、无 `=` 碎段、Domain 写入 |

### 1.2 黄金样本回放

从 `logs/` 抽 3 个真实文件（覆盖 single 与 watch 两种 suffix 形态），
固化为小型 fixture，断言 `build_rows` 的输出行数与关键字段。
这条把"纯函数测试"和"真实数据"接上，是 P2/P3 的主要安全网。

### 1.3 统一检查入口

- `Makefile` 或 `scripts/check.sh`：`ruff format --check` + `ruff check` + `pytest`
- 可选 GitHub Actions（仓库当前在 git 上但无远端 CI 配置，先做本地入口）

### 1.4 `PRAGMA user_version` 作为 schema 版本锚

`SqliteEventStore.__init__` 里写入/校验版本号，为 P2/P3 之后的任何 schema 演进
留一个可靠的判据（当前"表在不在"这种检查方式已经在 R1 里暴露了不足）。

### 1.5 DoD

- [ ] `pytest` 全绿，`ruff check` / `ruff format --check` 通过
- [ ] 黄金样本回放行数与现场一致
- [ ] 一条命令跑完全套检查

---

## 6. Phase 2 — payload 直连（核心简化，收益最大）

### 6.1 动机：当前为了存结构化数据绕了一整圈

```
protobuf ──to_json()──→ JSON 字符串 ──logger.debug──→ LogRecord
   └─ JsonFormatter 摊平 ──→ app-*.log 文件 ──import_logs 逐行 json.loads──→ dict ──→ SQLite
                                              └─ SqliteLogHandler 又把同一份 JSON 解析回 dict
```

这一个回环同时制造了：

| 产物 | 位置 |
|---|---|
| `extract_payload` 的三分支形态兼容 | `store.py:66` |
| `BASE_FIELDS` 的三处重复 | R7 |
| `message` 字段的冗余与删除逻辑 | `logging_config.py:31-37` |
| `compact_logs.py` 整个工具（332 行） | 只为删掉冗余 `message` |
| 135 MB `logs/` 中的绝大部分 I/O | R10 之外的主要来源 |

### 6.2 目标形态

```
protobuf ──to_dict()──→ dict ──logger.debug(extra={"payload": dict, "room_id": …})
                             ├─→ SqliteLogHandler：直接用 dict，零 JSON 往返
                             └─→ JsonFormatter：序列化成文件格式（与现状同形）
```

### 6.3 改动清单

| 文件 | 改动 |
|---|---|
| `websocket.py:30 _dump_debug` | 改用 `Message.to_dict()` 产出 dict，经 `extra={"payload":…, "room_id":…}` 传入；日志正文改为占位串 |
| `logging_config.py:17 JsonFormatter.format` | 新增分支：`getattr(record, "payload", None)` 是 dict 时直接摊平，**不依赖 message 文本** |
| `store.py:711 SqliteLogHandler.emit` | 优先取 `record.payload`；缺失时回退 `extract_payload`（保兼容） |
| `store.py` | 新增单一入口 `payload_of_record(record)`，把"直连/回退"收敛在一处 |

### 6.4 兼容性设计（关键）

- **文件格式保持不变**：payload 行仍是"顶层摊平 + 无 message"，`import_logs.py`
  与历史日志完全不受影响。
- **回退路径保留**：离线导入只有文本，没有 `record.payload`，`extract_payload`
  及其三分支逻辑**不删**，只是降级为兼容路径。
- **开关**：`config.json` 加 `logging.structured_debug: true|false`（缺省 true），
  出现异常可单点回退到旧路径。

### 6.5 待验证（动手前先做）

1. **`Message.to_dict()` 与 `to_json()` 的口径是否完全一致**（键名 camelCase？
   是否含默认值的零值字段？空列表/null 的处理？）。betterproto 版本间行为有差异。
   → 以 §1.2 的黄金样本逐行 diff 为准，不靠推断。
2. `extra={"payload": ...}` 与 `LogRecord` 既有属性无冲突（`payload` 非保留名，
   但需确认 betterproto/未来 logging 的新增属性）。
3. `QueueHandler` 的 `prepare()` 若被引入会**剥离**自定义 extra —— 本项目是自建队列，
   引入 stdlib handler 时必须重新验证这一条。

### 6.6 度量（迁移前后各跑一次 30 分钟采集）

| 指标 | 期望 |
|---|---|
| `to_json` / `to_dict` 调用次数 | 从 2N 降到 N |
| `logs/` 增量体积 | 持平或下降（不应上升） |
| 入库行数 | **必须完全相同**（这是硬断言） |
| 事件循环 CPU | 下降 |
| `chat_fts MATCH` 命中 | 与旧路径一致 |

### 6.7 DoD

- [ ] 黄金样本回放入库行数与字段逐行相同
- [ ] 关闭开关时行为与改动前完全一致
- [ ] `compact_logs.py` 对新日志判定为"无需处理"（`droppable_payload` 返回 None，不改内容）

---

## 7. Phase 3 — 拆分与去重

### 7.1 `store.py` 792 行拆分

| 新模块 | 内容 | 行数估 |
|---|---|---|
| `schema.py` | `_COMMON_DDL` / `_TABLE_EXTRA_DDL` / `_build_tables_ddl` / `FTS_DDL` / `_METHOD_TABLES` / `_METHOD_PROJECTORS` / 插入列常量 | ~230 |
| `projection.py` | `_dig` `_int` `_ranks` `project()` 与各 `_project_*`（**公开**，替掉 R8 的私有调用） | ~150 |
| `eventstore.py` | `SqliteEventStore`（连接、事务、查询） | ~130 |
| `loghandler.py` | `SqliteLogHandler` | ~110 |
| `recordfmt.py` | `BASE_FIELDS`（唯一来源）+ `JsonFormatter` | ~60 |

> `migrate_split_events.py` 已在本轮删除，不必考虑它的 import 兼容。

### 7.2 常量收敛（修 R7）

- `BASE_FIELDS` 只在 `recordfmt.py` 定义；`store` / `compact_logs` import。
- `VALID_SINKS` 只在 `config.py` 定义；`main.py:25` 的副本删除并 import。
- 日志目录 / 库路径只由 `config.py` 的 `PROJECT_ROOT` 推导；
  `store.DEFAULT_DB_PATH` / `logging_config.DEFAULT_LOG_PATH` 改为引用 `config`。
- 导入别名统一：现在 `store` 与 `store as event_store` 两种写法并存，选一种。

### 7.3 LogRecord / dict 双形态收敛

现在 `_record_field` / `_record_text` / `record_timestamp` / `extract_payload`
各自做一遍 `isinstance(record, dict)` 判断。收敛为：

- 一个 `RecordLike` 协议 + `as_record(record)` 适配器
- 离线导入侧统一先适配成同一形态，其余函数只处理一种

### 7.4 `websocket.py` handler 表驱动化

12 个 `_parseXxxMsg` 是同一套模板（时间戳 → parse → user 三件套 → dump →
stat_logger → ui 模板），约 170 行样板。改为声明式 spec：

```
_HANDLER_SPECS = {
    "WebcastChatMessage": Spec(msg_cls=ChatMessage,
                               stat=("payLevel","fansLevel","userName","content"),
                               ui="…模板…"),
    …
}
```

保留特化 handler（`_parseControlMsg` 有副作用：置 `stop_event` + `close()`）。

顺带：`_handle_binary:202` 每条消息都做 `asyncio.iscoroutinefunction(handler)`，
spec 化后 handler 类型在注册时即已知，判断可移出热路径。

### 7.5 双写消除

- `ui.info(...)` + `logger.info(...)` 成对重复约 15 处（`websocket.py` 关闭/心跳/
  接收路径最集中）→ 提供单一出口，两边同时写。
- 结合 R6：让 stats 日志的 `message` 走 JSON（与库类型对齐，`totalUserCount` 变 int），
  或在 `sinks` 不含 `file` 时彻底停掉 stats 文件双写。

### 7.6 DoD

- [ ] `store.py` 拆分后每个模块单一职责，行数与上表相符
- [ ] 全仓库 `BASE_FIELDS` / `VALID_SINKS` / 路径常量各只有一处定义
- [ ] 无跨模块私有调用（`store._x`）
- [ ] `websocket.py` 净减行数 ≥ 120，且行为由 P1 测试保证不变

---

## 8. Phase 4 — 运行时健壮性

### 8.1 连接复用（修 R11）

- `RoomContext` 或 app 级持有共享 `aiohttp.ClientSession`（含连接池与超时），
  `_connect_once` 改用 `DoyinLiveRoom.new(session=…)`（该参数已存在）。
- cookie 播种从"每次重连"降为"每 session 一次"。
- 副作用：`DoyinLiveRoom` 的 `_owns_session` 分支可以简化。

### 8.2 `web_rid → room_id` 缓存

页面抓取的 `roomId` 对同一 `web_rid` 稳定。加带 TTL 的缓存，
仅在 `InvalidWebridError` 时失效重取。收益：重连路径省 2 次 HTTP。

### 8.3 统一退避策略（修 R12）

```
BackoffPolicy(base=5, factor=2, max=300, jitter=True)
```
取代"固定 5s"与 `_invalid_backoff` 的 5/15/30 阶梯；加抖动避免多房间同步重连
打在服务端同一瞬间。

### 8.4 超时常量集中

现在散落：`5`（重试）、`600`（giveup）、`60`（心跳超时）、`10`（检查间隔）、
`5`（ws close）、`10.0`（ClientTimeout）、`300`/`60`（ntfy）。集中为
`Timeouts` dataclass，可读可测。

### 8.5 `main.py` 全局收敛（修 R13）

- 模块级 `shutdown_event` / `start_time` / `shutdown_count` → `AppState`
- 第 3 次信号真正 `os._exit(130)`（当前只打印）

### 8.6 DoD

- [ ] 重连路径的 HTTP 请求数下降（用 `_log_ws_cookies` 的 DEBUG 日志计数核对）
- [ ] 断开 ntfy 时的日志呈现为指数退避而非固定间隔
- [ ] 3 次 Ctrl-C 在 1s 内退出

---

## 9. Phase 5 — 工具与运维

### 9.1 工具目录收敛

- 新增 `tools/`：`import_logs.py` / `ip_cache.py` / `compact_logs.py` / `doctor.py`
  / `ass/`，共享一个 CLI bootstrap（argparse 基类、`open_store()`、
  路径默认值一律取自 `config`）。
- `ass.py`（574 行，弹幕→ASS 字幕）与采集主链路无关，整体搬出。
- `ip_cache` 的 DDL 目前在 `store.py`、CLI 在 `ip_cache.py`、查询又自带一套
  `sqlite3.connect` 与锁 —— 统一到 store 的连接管理，或明确文档化"独立只读连接"的取舍。

### 9.2 日志轮转

`logging.FileHandler` → `RotatingFileHandler`（按大小）。`compact_logs.py`
随之降级为纯归档工具（gzip），rewrite 逻辑随 P2/P3 逐步退休。

### 9.3 配置化 `NTFY_URL`

现在硬编码在 `ntfy.py:13`，watch 模式的硬依赖却是配置项之外的东西。
移到 `config.json`（带缺省值）。

### 9.4 cookie 失效检测

"登录了却收不到礼物推送"是注释里反复出现的人工排查项。改为自动检测：
开播 N 分钟内一条 `WebcastGiftMessage` 都没有 → 主动告警（提示 cookie 可能过期）。
顺带在 `_domain_cookies` 已有 DEBUG 排查入口的基础上，给出结论而不只是原始 cookie 名单。

### 9.5 DB 运维

保留策略（按时间/房间裁剪）、定时 WAL checkpoint、`VACUUM` 指引，
全部落到 §4 的 `doctor.py` 里可观测。

### 9.6 清理

- `requirements.txt` 去掉 `requests`（R14）
- `README.md`（692 B，上游致谢）与 `AGENTS.md` 合并，或明确指向关系

### 9.7 DoD

- [ ] `src/` 只剩采集主链路 + `scripts/sign.js`
- [ ] `logs/` 与 DB 有明确轮转/保留策略且在 `doctor.py` 中可见
- [ ] 依赖清单与实际 import 一致

---

## 10. 待决项

| 编号 | 问题 | 影响 | 建议 |
|---|---|---|---|
| D1 | LikeMessage 占 58% 行（69,172 行 / 两个整数，R10），是否只存累计计数的变化点？ | 影响 P5 之后的存储规划 | **本轮不做**。先确认分析口径（是否需要逐条点赞序列），有结论再单开 |
| D2 | stats 流（R6）与 DEBUG 流重复，是否彻底停掉 stats 文件？ | 影响 P3.5 范围 | 若 `sinks` 只开 sqlite，倾向停掉；有 file 需求则改为 JSON 对齐 |
| D3 | `logs/app-*.log` 归档格式是否长期保留？ | 决定 P2 的回退路径能删多少 | 倾向：保留一年，之后只留 DB |
| D4 | 是否需要对外查询接口（CLI `query` 子命令）？ | 影响是否把 store 拆得更细 | 倾向 `tools/query.py`，不动主链路 |

---

## 11. 风险登记

| 风险 | 阶段 | 缓解 |
|---|---|---|
| 迁移中途失败导致数据损坏 | P0 | 单大事务 + 快照 + 逐表对账；先副本演练 |
| `to_dict()` 与 `to_json()` 口径不一致 | P2 | 黄金样本逐行 diff（§6.5），不一致则退回 `to_json()+loads` |
| 拆模块时漏改某处常量引用 | P3 | P1 的 grep/测试 + 每模块单独提交 |
| 重构后归属漂移（事件归错房间） | P2/P3 | `room_id_from_record` 测试 + `doctor.py` 的空 `room_id` 占比监控 |
| 共享 ClientSession 引入连接串扰 | P4 | 单房间单 session 起（不跨房间共享），只跨重连复用 |
| 阶段过多导致半途而废 | 全局 | 每个阶段 DoD 独立可交付，**允许只做 P0+P1 就停** |

---

## 12. 建议的落地批次

| 批次 | 内容 | 说明 |
|---|---|---|
| 第 1 批 | P0 全部 + P1 全部 | 纯止血与安全网，不改采集行为，收益立即可见 |
| 第 2 批 | P2 | 最大的一次简化，带开关与度量 |
| 第 3 批 | P3 + P4 | 可拆成两个并行分支（模块拆分 / 运行时） |
| 第 4 批 | P5 | 清理与运维，随时可插队 |

> 最小可行终点：**只做第 1 批**。R1/R2/R3/R5 四个已验证的问题即被消除，
> 且此后所有重构都有测试可依。
