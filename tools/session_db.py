#!/usr/bin/env python3
"""场次（session）选择公共模块，供 sqlite 版脚本共用。

分表 events.db 每行的 session 列 = app 启动时刻后缀（同 logs/app-<session>.log），
与 stats 日志文件名后缀不同（那是房间连接时刻）。"天"按 session 日期前缀
分组，跨午夜场归启动日；天内序号按 session 启动时间升序，1 为最早、-1 为最晚。

--session 语法（resolve_session）：
  2026-10-11_22-04-21   精确 session id
  2026-10-11:1          当天第 1 场
  2026-10-11:-1         当天最后一场（负数从后往前数）
  2026-10-11            裸日期：当天恰好 1 场时直接选中，否则报错列候选
"""
import re
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

DEFAULT_DB_PATH = (
    Path(__file__).resolve().parent.parent / "data" / "events.db"
)

_SESSION_ID_RE = re.compile(r"^\d{4}-\d{2}-\d{2}_\d{2}-\d{2}-\d{2}$")
_DAY_INDEX_RE = re.compile(r"^(\d{4}-\d{2}-\d{2}):(-?\d+)$")
_DAY_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_TS_FMT = "%Y-%m-%d %H:%M:%S,%f"


def parse_ts(text):
    """ev_*.ts（'2026-10-11 13:01:39,768'）-> datetime。"""
    return datetime.strptime(text, _TS_FMT)


def open_db(db_path=None):
    """只读打开 events.db 并校验分表已迁移。"""
    path = Path(db_path or DEFAULT_DB_PATH).expanduser()
    if not path.is_file():
        print(f"错误: 事件库 '{path}' 不存在。", file=sys.stderr)
        sys.exit(1)
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    migrated = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE name = 'ev_chat'"
    ).fetchone()
    if not migrated:
        print(
            f"错误: '{path}' 中没有 ev_chat 分表。分表迁移已完成，"
            "请检查 db_path 是否指向正确的库；库结构说明见 "
            "docs/adr/0001-sqlite-split-migration.md。",
            file=sys.stderr,
        )
        sys.exit(1)
    return conn


def list_sessions(conn, rooms=None):
    """列出全部场次，按 session 升序。

    场次的存在性以**用户活动**为准：ev_chat ∪ ev_gift ∪ ev_member 任一
    表有行才算（直播结束后直播间往往仍可连接，只会产生 ev_rank/ev_stats
    的房间统计行，那种"空场次"不算数）。时间跨度取活动行的 min/max ts。
    rooms 给定时先过滤，天内序号在过滤后的集合上重排（-r 影响 :N 编号）。
    返回 dict 列表：session / room_id / start / end / chat_rows / day_index。
    """
    rows = conn.execute(
        """
        SELECT session,
               MAX(room_id) FILTER (WHERE room_id <> '') AS room_id,
               MIN(ts) AS start_ts,
               MAX(ts) AS end_ts,
               SUM(is_chat) AS chat_rows
        FROM (
            SELECT session, room_id, ts, 1 AS is_chat FROM ev_chat
            UNION ALL
            SELECT session, room_id, ts, 0 FROM ev_gift
            UNION ALL
            SELECT session, room_id, ts, 0 FROM ev_member
        )
        GROUP BY session
        ORDER BY session
        """
    ).fetchall()
    sessions = []
    for session, room_id, start_ts, end_ts, chat_rows in rows:
        if rooms and room_id not in rooms:
            continue
        sessions.append(
            {
                "session": session,
                "room_id": room_id or "",
                "start": parse_ts(start_ts),
                "end": parse_ts(end_ts),
                "chat_rows": chat_rows or 0,
                "day_index": 0,
            }
        )
    counters = {}
    for item in sessions:  # 天内序号：1 = 当天最早，随 -r 过滤重排
        day = item["session"][:10]
        counters[day] = counters.get(day, 0) + 1
        item["day_index"] = counters[day]
    return sessions


def format_session_table(sessions):
    """人类可读的场次清单（含天内序号，可直接抄进 --session）。"""
    if not sessions:
        return "（没有匹配的场次）"
    lines = [
        f"{'day':<10} {'#':>2}  {'session':<20} {'room_id':<13} "
        f"{'start':<14} {'end':<15} {'chats':>5}"
    ]
    for s in sessions:
        start = s["start"]
        end = s["end"]
        end_mark = " (+1)" if end.date() > start.date() else ""
        lines.append(
            f"{s['session'][:10]:<10} {s['day_index']:>2}  "
            f"{s['session']:<20} {s['room_id']:<13} "
            f"{start.strftime('%H:%M:%S'):<14} "
            f"{end.strftime('%H:%M:%S') + end_mark:<15} "
            f"{s['chat_rows']:>5}"
        )
    return "\n".join(lines)


def print_session_list(sessions):
    print(format_session_table(sessions))


def _die(message):
    print(f"错误: {message}", file=sys.stderr)
    sys.exit(1)


def resolve_session(spec, sessions):
    """把 --session 写法解析成 sessions 里的一个 dict。

    sessions 应已按 -r 过滤并按 session 升序。匹配不到或有歧义时报错退出，
    并列出候选，绝不静默猜测。
    """
    if not sessions:
        _die("当前过滤条件下没有可用场次。")
    spec = spec.strip()

    if _SESSION_ID_RE.match(spec):
        for item in sessions:
            if item["session"] == spec:
                return item
        _die(
            f"场次 '{spec}' 不存在"
            "（可能被 -r 过滤掉，或该场次无数据）。用 --list-sessions 查看。"
        )

    m = _DAY_INDEX_RE.match(spec)
    if m:
        day, idx = m.group(1), int(m.group(2))
        candidates = [s for s in sessions if s["session"].startswith(day)]
        if not candidates:
            _die(f"{day} 没有可用场次。")
        if idx == 0:
            _die(f"'{spec}' 序号不能为 0（1=当天第一场，-1=最后一场）。")
        try:
            return candidates[idx - 1 if idx > 0 else idx]
        except IndexError:
            _die(
                f"{day} 只有 {len(candidates)} 场，'{idx}' 越界。"
                f"当天场次：\n{format_session_table(candidates)}"
            )

    if _DAY_RE.match(spec):
        candidates = [s for s in sessions if s["session"].startswith(spec)]
        if len(candidates) == 1:
            return candidates[0]
        if not candidates:
            _die(f"{spec} 没有可用场次。")
        _die(
            f"{spec} 有 {len(candidates)} 场，请用 {spec}:1 / {spec}:-1 "
            f"（或精确 session id）指定：\n{format_session_table(candidates)}"
        )

    _die(
        f"无法识别的场次写法 '{spec}'。支持：精确 id、YYYY-MM-DD:N、"
        "YYYY-MM-DD:N（N 可为负）、裸日期 YYYY-MM-DD。"
    )


def latest_session(sessions):
    """无 -S/--start/--end 时的缺省行为：最新场次（对齐旧脚本"取最新日志"）。"""
    if not sessions:
        _die("当前过滤条件下没有可用场次。")
    return sessions[-1]
