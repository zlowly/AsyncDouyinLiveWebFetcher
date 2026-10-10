"""SQLite 事件库：DEBUG payload 的投影、压缩与实时入库。

日志里同一条 protobuf 有三种存在形态，入库前都需先还原成 payload dict：

    message JSON   payload 是 record["message"] 里的 JSON 字符串（旧格式）
    flattened      payload 被摊平进 record 顶层，message 字段不存在（新格式）
    plain text     非 payload 的普通 DEBUG 文本（如 asyncio），跳过

公共入口 extract_payload() 供实时 handler 与离线导入共用，避免两边逻辑漂移。

设计依据见 plan：/home/zsy/.opencode/plan/douyin-log-sqlite.md
"""

import json
import logging
import os
import queue
import re
import sqlite3
import threading
import time
import zlib
from datetime import datetime

# 与 logging_config.BASE_FIELDS 保持一致（JsonFormatter 写入的固定字段）。
BASE_FIELDS = frozenset({"timestamp", "level", "name", "message"})

DEFAULT_DB_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "..", "data", "events.db"
)

# JsonFormatter 的输出格式：'%Y-%m-%d %H:%M:%S' + ',' + 三位毫秒。
_TS_COMMA = "%Y-%m-%d %H:%M:%S,%f"
_TS_DOT = "%Y-%m-%d %H:%M:%S.%f"

# 只有这些 method 会携带 common；FansclubMessage 用的是 commonInfo。
_COMMON_KEYS = ("common", "commonInfo")

# 房间号的三处来源：extra 字段、logger 名（room_/stat_<rid>）、消息正文。
_ROOM_LOGGER_RE = re.compile(r"^(?:room|stat)_(\d+)$")
_ROOM_PREFIX_RE = re.compile(r"^\[(\d{6,})\]")
_ROOM_IN_TEXT_RE = re.compile(r"Room (\d{6,}) ")


def _dig(obj, *path):
    """安全地按路径取嵌套值，任一层不是 dict 就返回 None。"""
    cur = obj
    for key in path:
        if not isinstance(cur, dict):
            return None
        cur = cur.get(key)
    return cur


def _int(value):
    """protobuf 把整数都序列化成字符串，安全转 int。"""
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def extract_payload(record):
    """从日志记录还原 protobuf payload dict；不是 payload 行则返回 None。

    兼容两种载体：
      dict        离线导入时的日志行
      LogRecord   实时 handler 收到的对象，payload 还在 msg/args 里，
                  摊平只发生在 JsonFormatter.format()，对 LogRecord 不可见
    """
    message = _record_text(record)
    if isinstance(message, str):
        try:
            payload = json.loads(message)
        except (json.JSONDecodeError, TypeError):
            payload = None
        if isinstance(payload, dict):
            return payload

    if not isinstance(record, dict):
        # LogRecord 没有摊平形态，message 解析不出 dict 就不是 payload 行。
        return None

    # 新格式：payload 摊平在顶层，message 已被 JsonFormatter 删除。
    flattened = {
        key: value for key, value in record.items() if key not in BASE_FIELDS
    }
    return flattened or None


def _record_text(record):
    """取记录的原始文本消息。"""
    if isinstance(record, dict):
        return record.get("message")
    try:
        return record.getMessage()
    except Exception:  # noqa: BLE001 - 坏 args 不该让日志写入中断
        return None


def record_timestamp(record):
    """取记录时间戳，格式与 JsonFormatter 输出保持一致。

    实时路径必须自己拼 —— LogRecord 没有 timestamp 字段，
    而 projection 的 ts 是主时间键。
    """
    if isinstance(record, dict):
        return record.get("timestamp")
    created = getattr(record, "created", None)
    if created is None:
        return None
    millis = int(getattr(record, "msecs", 0))
    stamp = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(created))
    return f"{stamp},{millis:03d}"


def parse_timestamp(text):
    """把日志时间戳转成本地 epoch 秒；解析失败返回 None。"""
    if not isinstance(text, str):
        return None
    for fmt in (_TS_COMMA, _TS_DOT, "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(text, fmt).timestamp()
        except ValueError:
            continue
    return None


def _level_name(record):
    """兼容 logging.LogRecord 与离线导入用的普通 dict。"""
    if isinstance(record, dict):
        return record.get("level") or record.get("levelname") or "DEBUG"
    return getattr(record, "levelname", "DEBUG")


def _record_field(record, key, default=None):
    """兼容 logging.LogRecord 与离线导入用的普通 dict。"""
    if isinstance(record, dict):
        return record.get(key, default)
    return getattr(record, key, default)


def room_id_from_record(record, default=""):
    """推断 room_id，按可靠性依次尝试 extra -> logger 名 -> 正文。

    payload 里的 common.roomId 每次会话都变，不能用来关联房间
    （见 plan F6），所以房间号只能从这三处取。
    """
    extra = _record_field(record, "room_id")
    if extra:
        return str(extra)

    name = _record_field(record, "name") or ""
    match = _ROOM_LOGGER_RE.match(name)
    if match:
        return match.group(1)

    message = _record_text(record) or ""
    if isinstance(message, str):
        match = _ROOM_PREFIX_RE.match(message) or _ROOM_IN_TEXT_RE.search(
            message
        )
        if match:
            return match.group(1)
    return default


def _ranks(ranks_list):
    """ranksList 实测恒为长度 3，投影成 [名次, user_id, 用户名]。

    两类消息结构不同，实测 60,855 条：
      RoomUserSeqMessage  条目带 rank 字段
      RoomRankMessage     条目只有 user，名次只能取数组下标（1 起）
    """
    if not isinstance(ranks_list, list):
        return None
    result = []
    for position, entry in enumerate(ranks_list, start=1):
        if not isinstance(entry, dict):
            continue
        user = entry.get("user") or {}
        rank = _int(entry.get("rank"))
        if rank is None:
            rank = position
        result.append([rank, user.get("id"), user.get("nickName")])
    return result


def _project_like(payload, row):
    """累计点赞计数器（占 59.8% 字节，但有效载荷只有两个数）。"""
    row["like_count"] = _int(payload.get("count"))
    row["total"] = _int(payload.get("total"))


def _project_chat(payload, row):
    row["content"] = payload.get("content")


def _project_emoji(payload, row):
    row["content"] = payload.get("defaultContent")


def _project_member(payload, row):
    row["member_count"] = _int(payload.get("memberCount"))
    row["action"] = _int(payload.get("action"))


def _project_social(payload, row):
    row["action"] = _int(payload.get("action"))
    row["follow_count"] = _int(payload.get("followCount"))


def _project_rank(payload, row):
    """RoomUserSeq（在线人数）与 RoomRank（榜单）共用。"""
    row["total"] = _int(payload.get("total"))
    row["total_user"] = _int(payload.get("totalUser"))
    # ranksList 缺失时存 SQL NULL，而不是 json.dumps(None) 的字符串 "null"。
    ranks = _ranks(payload.get("ranksList"))
    row["ranks"] = json.dumps(ranks, ensure_ascii=False) if ranks else None


def _project_room_stats(payload, row):
    row["total"] = _int(payload.get("total"))


def _project_content(payload, row):
    """FansclubMessage / RoomMessage：只有正文和一个类型码。"""
    row["content"] = payload.get("content")
    row["action"] = _int(payload.get("type"))


def _project_control(payload, row):
    row["status"] = _int(payload.get("status"))


# method -> 该 method 独有的投影字段。公共字段（用户、等级）无条件抽取。
_METHOD_PROJECTORS = {
    "WebcastLikeMessage": _project_like,
    "WebcastChatMessage": _project_chat,
    "WebcastEmojiChatMessage": _project_emoji,
    "WebcastMemberMessage": _project_member,
    "WebcastSocialMessage": _project_social,
    "WebcastRoomUserSeqMessage": _project_rank,
    "WebcastRoomRankMessage": _project_rank,
    "WebcastRoomStatsMessage": _project_room_stats,
    "WebcastFansclubMessage": _project_content,
    "WebcastRoomMessage": _project_content,
    "WebcastControlMessage": _project_control,
}

# 剩余列统一置 NULL，保证 row 的 key 集合与 _COLUMNS 对齐。
_NULL_COLUMNS = (
    "content",
    "like_count",
    "total",
    "total_user",
    "member_count",
    "action",
    "follow_count",
    "status",
    "ranks",
)


def project(payload):
    """把 protobuf payload 投影成 events 表的一行。

    实测投影后只保留原始字节的 2.9%（34 倍缩减），原始 payload
    仍以压缩 BLOB 存储，长尾字段随时可还原。
    """
    common = {}
    for key in _COMMON_KEYS:
        if isinstance(payload.get(key), dict):
            common = payload[key]
            break

    user = payload.get("user") if isinstance(payload.get("user"), dict) else {}
    method = common.get("method")

    row = dict.fromkeys(_NULL_COLUMNS)

    row.update(
        {
            "method": method,
            "msg_id": common.get("msgId"),
            "create_time": _int(common.get("createTime")),
            "user_id": user.get("id"),
            "user_name": user.get("nickName"),
            "gender": _int(user.get("gender")),
            "pay_level": _int(_dig(user, "payGrade", "level")),
            "fans_level": _int(_dig(user, "fansClub", "data", "level")),
        }
    )

    projector = _METHOD_PROJECTORS.get(method)
    if projector is not None:
        projector(payload, row)
    return row


SCHEMA = """
-- events：一条 DEBUG protobuf 消息 = 一行。
-- 投影列只保留常用字段（实测占原始字节 2.9%），完整原文在 payload 列。
-- 本表的注释会原样存进 sqlite_master.sql，用 .schema events 可见。
CREATE TABLE IF NOT EXISTS events (
    -- ===== 时间与归属 =====
    -- 自增主键；同时是 chat_fts.content_rowid（external content 约定）
    id            INTEGER PRIMARY KEY,
    -- 日志时间戳 'YYYY-MM-DD HH:MM:SS,mmm'，本地时区，仅用于展示
    ts            TEXT    NOT NULL,
    -- ts 的 epoch 秒。**时间范围查询一律走这列**（有索引）
    ts_epoch      REAL    NOT NULL,
    -- web_rid 房间号。不能取 payload 的 common.roomId —— 那是直播间
    -- 内部 ID，每次会话都变，无法跨会话关联
    room_id       TEXT NOT NULL DEFAULT '',
    -- 日志后缀：logs/app-<session>.log。与 msg_id 组成唯一键做幂等
    session       TEXT NOT NULL DEFAULT '',

    -- ===== 消息标识 =====
    -- WebcastXxxMessage。决定下面"业务字段"一节哪些列有值
    method        TEXT    NOT NULL,
    -- common.msgId，抖音侧全局消息 ID；与 session 组成唯一键去重
    msg_id        TEXT,
    -- common.createTime（毫秒）。与 ts_epoch 实测差约 65 秒，
    -- 跨源对齐与精确时序分析一律以 ts_epoch 为准
    create_time   INTEGER,
    -- 日志等级。目前恒为 DEBUG（INFO 行不是 protobuf dump，已过滤）
    level         TEXT NOT NULL DEFAULT 'DEBUG',
    -- 完整 payload JSON，zlib level 1 压缩。投影列没覆盖的长尾字段
    -- 用 store.SqliteEventStore.payload_of(id) 还原
    payload       BLOB    NOT NULL,

    -- ===== 用户画像 =====
    -- user.id
    user_id       TEXT,
    -- user.nickName。观众画像通常 group by 这列
    user_name     TEXT,
    -- user.gender：0 保密 / 1 男 / 2 女（映射见 websocket.py）
    gender        INTEGER,
    -- user.payGrade.level 付费等级。**源数据是字符串**，已转 int
    pay_level     INTEGER,
    -- user.fansClub.data.level 粉丝团等级。源数据本身是 int
    fans_level    INTEGER,

    -- ===== 业务字段，含义随 method 变 =====
    -- ChatMessage.content 弹幕正文；EmojiChatMessage.defaultContent
    -- 表情文案；Fansclub/RoomMessage 的公告正文
    content       TEXT,
    -- LikeMessage.count：本条消息带来的点赞数（增量）
    like_count    INTEGER,
    -- Like.total 累计赞 / RoomUserSeq.total 在线人数 /
    -- RoomStats.total 累计观看。**同一列三种含义，先看 method**
    total         INTEGER,
    -- RoomUserSeqMessage.totalUser 累计观众（区别于 total 的在线）
    total_user    INTEGER,
    -- MemberMessage.memberCount：进场时的当前成员数
    member_count  INTEGER,
    -- 状态码，含义随 method 变：Member.action 进场动作、
    -- Social.action 关注/分享类型、Fansclub.type、Room.type
    action        TEXT,
    -- SocialMessage.followCount 关注数
    follow_count  INTEGER,
    -- ControlMessage.status：3 = 直播结束（会触发优雅关闭）
    status        INTEGER,
    -- JSON [[名次, user_id, 昵称], ...]，恒 3 条。RoomUserSeq 条目
    -- 自带 rank；RoomRank 靠数组下标(1起)推，见 store._ranks()。
    -- 查询用 json_each + json_extract 展开
    ranks         TEXT
);

-- 幂等：同一 session 内 msg_id 重复时 INSERT OR IGNORE 跳过。
CREATE UNIQUE INDEX IF NOT EXISTS ux_events_session_msg
    ON events(session, msg_id);

-- 时间范围扫描（跨年查询的主路径）
CREATE INDEX IF NOT EXISTS idx_events_ts      ON events(ts_epoch);
-- 按房间看某段时间
CREATE INDEX IF NOT EXISTS idx_events_room    ON events(room_id, ts_epoch);
-- 按消息类型过滤时间范围
CREATE INDEX IF NOT EXISTS idx_events_method  ON events(method, ts_epoch);
-- 观众画像：某用户全部记录 / 按昵称聚合
CREATE INDEX IF NOT EXISTS idx_events_user    ON events(user_id);
CREATE INDEX IF NOT EXISTS idx_events_name    ON events(user_name);

-- chat_fts：events 的 external content 全文索引 —— 正文只存 events
-- 一份，chat_fts 只存倒排索引，靠下面三个触发器同步。
-- trigram 分词器要求 >= 3 字符，**2 字 CJK 检索不到**，需 LIKE 兜底。
CREATE VIRTUAL TABLE IF NOT EXISTS chat_fts USING fts5(
    content,      -- 弹幕/公告正文，来自 events.content
    user_name,    -- 用户昵称，来自 events.user_name
    content='events',
    content_rowid='id',
    tokenize='trigram'
);

-- 同步触发器。external content 的 delete 命令必须带上旧值，否则索引
-- 损坏；update 必须先删后插，否则旧词条会残留成幽灵命中。
CREATE TRIGGER IF NOT EXISTS events_ai AFTER INSERT ON events BEGIN
    INSERT INTO chat_fts(rowid, content, user_name)
    VALUES (new.id, new.content, new.user_name);
END;

CREATE TRIGGER IF NOT EXISTS events_ad AFTER DELETE ON events BEGIN
    INSERT INTO chat_fts(chat_fts, rowid, content, user_name)
    VALUES ('delete', old.id, old.content, old.user_name);
END;

CREATE TRIGGER IF NOT EXISTS events_au AFTER UPDATE ON events BEGIN
    INSERT INTO chat_fts(chat_fts, rowid, content, user_name)
    VALUES ('delete', old.id, old.content, old.user_name);
    INSERT INTO chat_fts(rowid, content, user_name)
    VALUES (new.id, new.content, new.user_name);
END;

-- imported_sessions：导入进度账本，跑 import_logs.py 时先查这表，
-- 命中就整文件跳过，所以重复执行不会导致行数增长。
CREATE TABLE IF NOT EXISTS imported_sessions (
    session     TEXT PRIMARY KEY,   -- 日志后缀，与 events.session 同义
    room_id     TEXT,               -- 当时推断出的房间号（可能为空串）
    source      TEXT,               -- 导入时的原始文件路径
    rows        INTEGER NOT NULL DEFAULT 0,  -- 实际写入 events 的行数
    imported_at TEXT NOT NULL       -- 导入完成时刻 'YYYY-MM-DD HH:MM:SS'
);
"""

_INSERT_SQL = """
INSERT OR IGNORE INTO events (
    ts, ts_epoch, room_id, session, method, msg_id, create_time,
    level, payload, user_id, user_name, gender, pay_level, fans_level,
    content, like_count, total, total_user, member_count, action,
    follow_count, status, ranks
) VALUES (
    ?, ?, ?, ?, ?, ?, ?,
    ?, ?, ?, ?, ?, ?, ?,
    ?, ?, ?, ?, ?, ?,
    ?, ?, ?
)
"""

_COLUMNS = (
    "ts",
    "ts_epoch",
    "room_id",
    "session",
    "method",
    "msg_id",
    "create_time",
    "level",
    "payload",
    "user_id",
    "user_name",
    "gender",
    "pay_level",
    "fans_level",
    "content",
    "like_count",
    "total",
    "total_user",
    "member_count",
    "action",
    "follow_count",
    "status",
    "ranks",
)


class SqliteEventStore:
    """SQLite 存储层。所有访问都持锁，允许从多个线程调用。"""

    def __init__(self, path: str = DEFAULT_DB_PATH):
        self.path = path
        parent = os.path.dirname(os.path.abspath(path))
        os.makedirs(parent, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(
            path, check_same_thread=False, isolation_level=None
        )
        # WAL 允许写入时仍可跨进程读取。
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        with self._lock:
            try:
                self._conn.executescript(SCHEMA)
            except sqlite3.OperationalError as exc:
                self._conn.close()
                raise RuntimeError(
                    f"建表失败：{exc}\n"
                    "本库依赖 SQLite 的 FTS5（trigram 分词器）与 JSON1，"
                    f"当前 SQLite {sqlite3.sqlite_version}。"
                    "请确认所用 Python 的 SQLite 构建启用了 FTS5。"
                ) from exc

    # ---- 写入 ---------------------------------------------------

    @staticmethod
    def _to_row(record, room_id, session, level):
        """日志记录 -> events 表参数元组；非 payload 行返回 None。"""
        payload = extract_payload(record)
        if payload is None:
            return None

        projection = project(payload)
        method = projection.pop("method")
        if not method:
            return None

        ts = record_timestamp(record)
        epoch = parse_timestamp(ts)
        if epoch is None:
            epoch = time.time()

        row = {
            "ts": ts or "",
            "ts_epoch": epoch,
            "room_id": room_id or "",
            "session": session or "",
            "method": method,
            "level": level,
            "payload": zlib.compress(
                json.dumps(payload, ensure_ascii=False).encode("utf-8"), 1
            ),
        }
        row.update(projection)
        return tuple(row[col] for col in _COLUMNS)

    @classmethod
    def build_rows(
        cls,
        records,
        *,
        room_id="",
        session="",
        room_resolver=None,
    ):
        """把一批日志记录转成可入库的元组列表，跳过非 payload 行。"""
        rows = []
        for record in records:
            current = room_id
            if room_resolver is not None:
                current = room_resolver(record, room_id)
            row = cls._to_row(record, current, session, _level_name(record))
            if row is not None:
                rows.append(row)
        return rows

    def insert_rows(self, rows):
        """写入一批已构建好的元组，返回**实际**插入行数。

        必须用 cursor.rowcount 而不是 connection.total_changes ——
        chat_fts 的同步触发器会写入影子表，那部分变更会被
        total_changes 计入，导致虚高。
        """
        if not rows:
            return 0
        with self._lock:
            cursor = self._conn.cursor()
            try:
                cursor.executemany(_INSERT_SQL, rows)
                return cursor.rowcount
            finally:
                cursor.close()

    def insert_records(
        self,
        records,
        *,
        room_id="",
        session="",
        room_resolver=None,
    ):
        """批量写入日志记录。返回实际插入行数（OR IGNORE 会跳过重复）。"""
        rows = self.build_rows(
            records,
            room_id=room_id,
            session=session,
            room_resolver=room_resolver,
        )
        return self.insert_rows(rows)

    def mark_imported(self, session, room_id, source, rows):
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO imported_sessions"
                " (session, room_id, source, rows, imported_at)"
                " VALUES (?, ?, ?, ?, ?)",
                (
                    session,
                    room_id,
                    source,
                    rows,
                    time.strftime("%Y-%m-%d %H:%M:%S"),
                ),
            )

    def is_imported(self, session):
        with self._lock:
            cur = self._conn.execute(
                "SELECT 1 FROM imported_sessions WHERE session = ?", (session,)
            )
            return cur.fetchone() is not None

    # ---- 查询 ---------------------------------------------------

    def count(self, where="", params=()):
        sql = "SELECT COUNT(*) FROM events"
        if where:
            sql += f" WHERE {where}"
        with self._lock:
            return self._conn.execute(sql, params).fetchone()[0]

    def payload_of(self, event_id):
        """还原某行的原始 payload（长尾字段的兜底入口）。"""
        with self._lock:
            cur = self._conn.execute(
                "SELECT payload FROM events WHERE id = ?", (event_id,)
            )
            blob = cur.fetchone()
        if blob is None:
            return None
        return json.loads(zlib.decompress(blob[0]).decode("utf-8"))

    def close(self):
        with self._lock:
            self._conn.close()


class SqliteLogHandler(logging.Handler):
    """把 DEBUG 日志实时写入库的 handler。

    emit() 只做入队，真正的写库在后台线程里批量完成 —— 日志 handler
    是同步阻塞的，直接写 SQLite 会卡住 asyncio 事件循环。
    """

    def __init__(
        self,
        store,
        *,
        room_id="",
        session="",
        level=logging.DEBUG,
        batch=200,
        queue_size=100_000,
    ):
        super().__init__(level=level)
        self.store = store
        self.room_id = room_id
        self.session = session
        self._batch = batch
        self._queue = queue.Queue(maxsize=queue_size)
        self._stop = threading.Event()
        self.dropped = 0
        self._thread = threading.Thread(
            target=self._run, name="event-store-writer", daemon=True
        )
        self._thread.start()

    # ---- logging.Handler ----

    def emit(self, record):
        try:
            self._queue.put_nowait(record)
        except queue.Full:
            # 库写不出去时宁可丢日志，也不能阻塞采集主流程。
            self.dropped += 1

    def close(self):
        if self._stop.is_set():
            return
        self._stop.set()
        self._thread.join(timeout=5)
        super().close()

    # ---- 后台线程 ----

    def _drain(self):
        batch = []
        try:
            while len(batch) < self._batch:
                batch.append(self._queue.get_nowait())
        except queue.Empty:
            pass
        return batch

    def _flush(self, records):
        if not records:
            return
        rows = SqliteEventStore.build_rows(
            records,
            room_id=self.room_id,
            session=self.session,
            room_resolver=room_id_from_record,
        )
        self.store.insert_rows(rows)

    def _run(self):
        while not self._stop.is_set():
            records = self._drain()
            if records:
                self._flush(records)
            else:
                time.sleep(0.02)

        # 退出前把队列里剩下的刷干净。
        while True:
            records = self._drain()
            if not records:
                break
            self._flush(records)
