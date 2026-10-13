# AsyncDouyinLiveWebFetcher

Async douyin live stream danmaku fetcher. Python 3.12+, no packaging (run via `python src/main.py`).

## Quick start

```bash
conda activate AsyncDouyinLiveWebFetcher
pip install -r requirements.txt
```

## Run

| Mode | Command |
|------|---------|
| Watch (SSE) | `python src/main.py` |
| Single room | `python src/main.py -r <room_id>` |

## Lint & format (Ruff only — no black, isort, flake8, mypy)

```bash
ruff check src/          # lint
ruff check --fix src/    # lint + autofix
ruff format src/         # format
```

Config: line-length=79, double quotes everywhere. `ruff format` required before checkin.

## No tests, no CI, no pre-commit, no type checking

No test framework, no test directory, no CI workflows, no pre-commit hooks, no mypy/pyright.

## Architecture

```
main.py (CLI/信号/任务编排)
  → session.py (RoomContext + 重连状态机)
    → DoyinLiveRoom (liveroom.py)
      → DouyinChatWebSocketClient (websocket.py)
        → protobuf/douyin.py
```

- `DoyinLiveRoom` is spelled "Doyin" (not "Douyin") in code — search accordingly.
- Support modules: `config.py` (读取 `config.json`，白名单 + 日志存放形式), `whitelist.py` (白名单匹配), `ntfy.py` (SSE 监听，含 `NTFY_URL`), `ui.py` (Rich 控制台输出，`print` 全部走这里), `logging_config.py` (app/room/stat 日志), `constants.py`, `utils.py` (签名 + `wait_first`).
- `SqliteEventStore`（`store.py`）已拆表：10 张 `ev_*` 分表（按 method 路由）+ `chat_fts`（trigram 全文）+ `ip_cache`，单表 `events` 已废弃。**分表迁移已完成**，迁移脚本已删除，决策与安全模型见 `docs/adr/0001-sqlite-split-migration.md`；`payload_of(table, event_id)` 取原始 payload。
- Standalone tools in `src/`: `ass.py` (ASS subtitles；独立 CLI，保留自己的 `print`，cv2 惰性导入), `ip_cache.py` (ip_cache 表 CLI：import-csv/pending/user/status), `scripts/compact_logs.py` (日志瘦身/归档), `import_logs.py` (离线补录), `doctor.py`(计划新增：库自检).

## Key quirks

- **Watch mode** requires a local [ntfy.sh](https://ntfy.sh/) server at `http://localhost:10380/mytopic/sse` (defined as `NTFY_URL` in `ntfy.py`). Without it, watch mode won't crash but no room will ever connect — rooms start only when an ntfy SSE message reports a whitelisted streamer is live.
- **`config.json`**（项目根目录，取代旧的 `config/whitelist.json`）由 `src/config.py` 读取：
  - `whitelist` — 主播名 → 房间 ID 映射，驱动 watch mode。`ntfy.py` 解析 SSE 消息并回调 `main.py`，后者为每个白名单房间起/停一个 `session.py` 任务。
  - `logging.sinks` — 日志去处，`"file"`（日志文件）和/或 `"sqlite"`（事件库），**缺省两种都存**；脏值只告警不崩，非法项忽略、全空时回到缺省。
  - `logging.log_dir` / `logging.db_path` — 缺省 `logs/` 和 `data/events.db`，相对路径按项目根目录解析。
  - 统计日志（`setup_room_logger`）跟着 `log_dir` 走；`sinks` 不含 `file` 时挂 `NullHandler`，不落盘。
- **watch 与单房间模式的日志去处完全一致**，都由 `sinks` 决定，缺省都是 log + sqlite 双写。弹幕行的 `room_id` 靠 `websocket._dump_debug` 的 `extra={"room_id": ...}` 归属（`store.room_id_from_record` 先读 extra，再退回 logger 名/正文前缀），所以 watch 模式下多房间交错也能各自归属。`import_logs.py` 退成补录工具，不是必需步骤。
- **`__ac_nonce` cookie** is defined as `CONSTANTS.AC_NONCE` in `constants.py` — may expire, causing connection failures.
- **`src/scripts/sign.js`** is obfuscated Douyin `byted_acrawler` signing code executed via `mini-racer` (V8) in `utils.py`. May break when Douyin updates their signature algorithm.
- **`protobuf/readme.md`** is outdated (references `betterproto==2.0.0b6`; installed is `>=1.2.5`). Regenerate via: `protoc -I . --python_betterproto_out=. douyin.proto` from `src/protobuf/`.
- **`src/protobuf/douyin.py`** is generated — not hand-edited; `ruff.toml` per-file-ignores exempts its E501.
- **Logs** accumulate in `logs/` — both app-level and per-room stats. Not cleaned automatically.
- **`requests`** in `requirements.txt` is vestigial (original sync project). The app uses `aiohttp`.

## graphify

This project has a knowledge graph at graphify-out/ with god nodes, community structure, and cross-file relationships.

When the user types `/graphify`, use the installed graphify skill or instructions before doing anything else.

Rules:
- For codebase questions, first run `graphify query "<question>"` when graphify-out/graph.json exists. Use `graphify path "<A>" "<B>"` for relationships and `graphify explain "<concept>"` for focused concepts. These return a scoped subgraph, usually much smaller than GRAPH_REPORT.md or raw grep output.
- Dirty graphify-out/ files are expected after hooks or incremental updates; dirty graph files are not a reason to skip graphify. Only skip graphify if the task is about stale or incorrect graph output, or the user explicitly says not to use it.
- If graphify-out/wiki/index.md exists, use it for broad navigation instead of raw source browsing.
- Read graphify-out/GRAPH_REPORT.md only for broad architecture review or when query/path/explain do not surface enough context.
- After modifying code, run `graphify update .` to keep the graph current (AST-only, no API cost).
