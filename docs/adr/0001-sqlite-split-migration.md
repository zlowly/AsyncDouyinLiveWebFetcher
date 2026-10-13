# ADR-0001：events 单表 → 10 张 `ev_*` 分表

- **状态**：已执行完毕（Completed）
- **窗口**：正式库先行；测试库 2026-10-13 补做同一迁移并通过对账
- **代码归属**：`src/store.py` 是运行时的 schema 归属方
- **设计原稿**：`~/.opencode/plan/douyin-log-sqlite.md`（未入库，F1–F10 实测依据）
- **历史实现**：见文末「取回历史实现」

> 本文件取代迁移脚本本身。脚本是一次性工具，已删除；它承载的**知识**记录在此，
> 它的**全量代码**仍在 git 里。

---

## 1. 背景

早期事件库是单表 `events`，把 method、投影列、用户画像、原始 payload 全塞在一起，
两个后果：

1. **体积**：投影后只保留原始字节的 2.9%（34 倍缩减），但单表里 58% 的行是
   `WebcastLikeMessage`，有效载荷只有 `count` + `total` 两个整数
   （`douyin-log-sqlite.md` F1）。
2. **全文检索不可用**：FTS5 的 external-content 表只能绑一张表，无法按 method 分区。

拆成 10 张互斥分区表（按 method 路由）+ 独立的 `chat_fts`（绑 `ev_chat`）
+ `ip_cache` 附属表后，检索与统计都按分区走，且能保住 34 倍压缩比。

---

## 2. 决策

| 项 | 选择 | 理由 |
|---|---|---|
| 存储引擎 | SQLite（WAL + `synchronous=NORMAL`） | DuckDB 读写仅限单进程，直播进程持锁时无法跨进程查询 —— 硬阻塞 |
| 表划分 | 10 张 `ev_*` 互斥分区 + `ev_other` 兜底 | 按 method 路由，查询只扫相关分区 |
| 正文检索 | `chat_fts`，`content='ev_chat'` external-content | 正文只存一份，索引靠触发器同步 |
| 迁移安全 | 单大事务 + backup API 快照 + 逐表对账 | 见 §3 |
| 投影口径 | 实时写与迁移共用 `store.py` 的 DDL/路由/投影常量 | 零漂移，见 §5.3 |
| 迁移脚本 | 一次性，执行完即删除 | 已完成，无长期价值 |

---

## 3. 安全模型：迁移如何做到不丢数据

这是本 ADR 最值得保留的部分 —— **未来任何一次数据迁移都应照抄这套模型**。

```
BEGIN IMMEDIATE            ① 立刻拿写锁，确保没有并发写者
  ├─ backup API → 快照      ② 第二道保险，事务外可回滚
  ├─ DROP 旧触发器/chat_fts
  ├─ 建 10 张新表 + 索引 + 账本 + ip_cache
  ├─ 按 id 区间分批 INSERT...SELECT（每批 50,000 行）
  ├─ 逐表逐 method 对账      ③ 不一致直接 SystemExit → ROLLBACK
  ├─ 总行数校验              ④ migrated != total_old → RuntimeError
  ├─ DROP TABLE events
  ├─ 建 chat_fts + rebuild   ⑤ 重建倒排索引
  └─ ANALYZE
COMMIT
```

四个关键点：

1. **`BEGIN IMMEDIATE` 而非默认的 deferred**：迁移全程持写锁，杜绝"边迁边写"。
2. **快照必须开第二个连接**：持有 `BEGIN IMMEDIATE` 的连接上调
   `sqlite3_backup_step` 会**永久 BUSY 死锁**。做法是另开一个读连接 backup
   到新文件（写锁已在手，不会有并发写者，数据即当前快照）。
3. **对账在事务内做**，不一致时 `SystemExit` 触发 `ROLLBACK`，原库无损。
4. **`SQLITE_FULL` 时 SQLite 可能已自动回滚**，再发 `ROLLBACK` 会报
   "no transaction is active" 并掩盖原始错误 —— 所以回滚包在
   `try/except sqlite3.Error: pass` 里。

---

## 4. 两个非显然的技法

### 4.1 用 UDF 在 SQL 里解开 zlib payload

`short_id` / `display_id` / `sec_uid` / `gift` 投影在旧表里**没有对应列**，
只能从压缩 BLOB 里现场抽。而 `INSERT ... SELECT` 解不了 zlib。

解法：注册 Python UDF 解压，再用 SQLite 的 `json_extract` 走 SQL：

```python
conn.create_function("ident", 1, _user_ident, deterministic=True)
conn.create_function("gift_json", 1, _gift_json, deterministic=True)
conn.create_function("combo_json", 1, _combo_json, deterministic=True)

# 目标列别名 → SQL 侧表达式
_IDENT_EXPRS = (
    ("json_extract(ident(payload), '$[0]')", "short_id"),
    ("json_extract(ident(payload), '$[1]')", "display_id"),
    ("json_extract(ident(payload), '$[2]')", "sec_uid"),
)
```

要点：

- `deterministic=True` 允许 SQLite 把 UDF 调用提升到 WHERE/索引侧。
- UDF 必须**逐行容错**：坏行返回 `None` 而不是抛异常，否则整批 50,000 行回滚。
- `comboCount` 在 payload **顶层**（不在 `gift` 子对象里），路径要写对。

### 4.2 不能用 `executescript`

`isolation_level=None` 下，`executescript` 会隐式 COMMIT，直接破坏大事务。
迁移必须**逐句 `execute(TABLES_DDL)`**。这也是 `store.py` 把 DDL 做成
**语句元组** `TABLES_DDL` 而非一整段字符串的原因 —— 一份数据，两种执行方式。

---

## 5. 遗留的三条约束

### 5.1 `executescript(SCHEMA)` 只认"全旧 / 全新"两种 schema

`SqliteEventStore.__init__` 会无条件跑 `SCHEMA`。若库处于**混合态**（旧 `events`
表还在、`ev_*` 已建出），`CREATE VIRTUAL TABLE IF NOT EXISTS chat_fts` 会被跳过，
于是 `chat_fts` 仍绑在 `content='events'` 上 —— **写入不报错、integrity-check 通过，
但新弹幕从此不进全文索引，零告警**。

→ 迁移完成后遗留的代码缺口。正解是 `store.__init__` 加结构守卫 +
`PRAGMA user_version` 版本锚（见 `docs/refactor-phases.md` P0.1 / P1.4）。

### 5.2 `payload.common.roomId` 不可用于房间归属

payload 里的 `roomId` 是直播间内部 ID，**每次会话都变**（F6）。
`web_rid` 只能从 `extra` / logger 名 / 日志正文三处取，
见 `store.room_id_from_record`。

### 5.3 DDL 与路由常量的归属

`TABLES_DDL` / `_METHOD_TABLES` / `_COMMON_INSERT_COLS` / `_TABLE_EXTRA_COLS`
定义在 `store.py`，运行时写入口与迁移脚本都从这里 import。**任何 schema 演进都只能
改这一处**，否则两边口径漂移且难以察觉。

---

## 6. 执行结果

| 项目 | 迁移前 | 迁移后 |
|---|---|---|
| 表结构 | 单表 `events` + `events_ai/ad/au` | 10 张 `ev_*`，旧表与旧触发器已删 |
| 总行数 | 118,334 | **118,334**（逐表对账一致） |
| `chat_fts` | `content='events'`，11 万+ 文档 | `content='ev_chat'`，11,314 文档，`MATCH '打call'` → 2,451 |
| `ip_cache` 表 | 不存在 | 已建（0 行） |
| 体积 | 178.5 MB | 170.4 MB（`--vacuum` 回收，`freelist=0`） |
| 耗时 | — | 23.3 s |

`room_id=''` 占比 **0.00%**（归属无损）。
`ev_gift` 为 0 行 —— 测试数据采集于登录 Cookie 支持之前，游客模式收不到礼物推送。

---

## 7. 取回历史实现

迁移脚本已删除，全文仍在 git 里：

```bash
git show 060abae:src/migrate_split_events.py    # 348 行完整实现
git log --oneline --follow -- src/migrate_split_events.py
```

引入它的提交：

```
060abae feat: 登录 Cookie 采集礼物 + --sinks 覆盖存储方式
8e0198c 增加本地数据库
```

本 ADR 之后的 schema 演进请在 `src/store.py` 的 DDL 段直接进行，
并同步更新本 ADR 的 §5.1 约束。
