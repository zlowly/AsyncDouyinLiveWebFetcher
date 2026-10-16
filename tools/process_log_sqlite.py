#!/usr/bin/env python3
"""从 events.db 的分表提取聊天、礼物、关注/分享、粉丝团消息。

覆盖 4 张表：ev_chat（弹幕）、ev_gift（礼物）、ev_social（关注/分享）、
ev_notice（粉丝团升级/加入，非空正文）。tsv 输出按类型着色
（gift 黄 / social 绿 / notice 青，chat 无色）；仅 stdout 是 TTY 时着色，
管道/重定向保持纯净 TSV，--no-color 可强制关闭。

与旧版读 stats 日志的实现输出格式完全一致（tsv/json），
便于逐行 diff 对比；数据源换成 SQLite 后不再依赖 logs/。

场次指定（-S/--session）：
  -S 2026-10-11:1     10-11 号第一场
  -S 2026-10-11:-1    10-11 号最后一场
  -S 2026-10-11       裸日期：当天仅 1 场直选，多场报错列候选
  -S 2026-10-11_11-46-56   精确 session id
  不给 -S 时取最新场次；--start/--end 给定时取与区间相交的所有场次。
"""
import argparse
import csv
import fnmatch
import json
import sys
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import dbcli  # noqa: E402
import session_db  # noqa: E402
# 参数解析与时间过滤统一在 dbcli，search_chat.py 也 import 同一份。
from dbcli import (  # noqa: E402
    DATETIME_FORMATS,
    build_time_filter,
    parse_log_timestamp,
    parse_room_arg,
    parse_time_arg,
)

DEFAULT_ROOMS = ("74083423272", "249069870647")

# tsv 输出的按类型着色（ANSI）：chat 无色，其余三类上色。
KIND_COLORS = {
    "gift": "\x1b[33m",   # 黄
    "social": "\x1b[32m",  # 绿
    "notice": "\x1b[36m",  # 青
}
_COLOR_RESET = "\x1b[0m"


def match_user(user_name, pattern):
    """
    判断 userName 是否匹配模式，不区分大小写。
    模式含 * 或 ? 时按通配符匹配；否则按子串模糊匹配。
    """
    if not user_name:
        return False
    name = user_name.lower()
    pat = pattern.lower()
    if "*" in pat or "?" in pat:
        return fnmatch.fnmatchcase(name, pat)
    return pat in name


def select_sessions(conn, start, end, rooms, session_spec):
    """
    选择要处理的场次，返回 [(room_id, [session...])]。

    优先级：-S 显式指定 > --start/--end 区间相交 > 缺省最新场次。
    """
    sessions = session_db.list_sessions(conn, rooms=rooms)

    if session_spec:
        chosen = session_db.resolve_session(session_spec, sessions)
        return [(chosen["room_id"], [chosen])]

    if start is not None or end is not None:
        start_dt, start_gran = start if start else (None, None)
        end_dt, end_gran = end if end else (None, None)
        if end_gran == "date" and end_dt is not None:
            end_dt = end_dt + timedelta(days=1) - timedelta(microseconds=1)
        if "time" in (start_gran, end_gran):
            # 仅时分时无法按场次裁剪，取全部场次，行级再过滤
            matched = sessions
        else:
            matched = [
                s
                for s in sessions
                if not (
                    (end_dt is not None and s["start"] > end_dt)
                    or (start_dt is not None and s["end"] < start_dt)
                )
            ]
        if not matched:
            print(
                "错误: 指定时间范围内没有可处理的场次。",
                file=sys.stderr,
            )
            sys.exit(1)
    else:
        matched = [session_db.latest_session(sessions)]

    groups = []
    for room in rooms:
        room_sessions = [s for s in matched if s["room_id"] == room]
        if room_sessions:
            groups.append((room, room_sessions))
    return groups


def _extract_messages(conn, sessions, time_filter, user):
    """
    从 ev_chat / ev_gift / ev_social / ev_notice 提取指定场次的消息。

    逐表查询后归一化合并，每条带 kind（chat/gift/social/notice），
    按时间排序返回；输出仍是 timestamp / speaker / content 三列，
    kind 仅供着色使用。
    Member 与旧版一致跳过；EmojiChat 旧版不输出（method 不匹配），保持。
    """
    extracted = []
    for sess in sessions:
        session_id = sess["session"]

        # chat：弹幕正文（表情不匹配 ChatMessage，保持不输出）
        for ts_str, user_name, content in conn.execute(
            "SELECT ts, user_name, content FROM ev_chat"
            " WHERE session = ? AND method = 'WebcastChatMessage'",
            (session_id,),
        ):
            extracted.append((ts_str, user_name, content, "chat"))

        # gift：礼物名 x 连击数
        for ts_str, user_name, gift_name, gift_count in conn.execute(
            "SELECT ts, user_name, gift_name, gift_count"
            " FROM ev_gift WHERE session = ?",
            (session_id,),
        ):
            if not gift_name:
                continue
            if gift_count is not None:
                text = f"送出 {gift_name}x{gift_count}"
            else:
                text = f"送出 {gift_name}"
            extracted.append((ts_str, user_name, text, "gift"))

        # social：action=1 关注 / action=3 分享（库内实测仅此两种）
        for ts_str, user_name, action in conn.execute(
            "SELECT ts, user_name, action FROM ev_social"
            " WHERE session = ? AND action IN ('1', '3')",
            (session_id,),
        ):
            text = (
                "关注了主播" if action == "1" else "分享了直播间"
            )
            extracted.append((ts_str, user_name, text, "social"))

        # notice：正文原样输出；非空过滤排除 action 6/7 与空白 RoomMessage
        for ts_str, user_name, content in conn.execute(
            "SELECT ts, user_name, content FROM ev_notice"
            " WHERE session = ? AND TRIM(COALESCE(content, '')) <> ''",
            (session_id,),
        ):
            extracted.append((ts_str, user_name, content, "notice"))

    def keep(ts_str, user_name, content, kind):
        """套用时间/用户过滤；返回排序键或 None。"""
        if not user_name or content is None or not str(content).strip():
            return None
        if user is not None and not match_user(user_name, user):
            return None
        ts_dt = parse_log_timestamp(ts_str)
        if time_filter is not None:
            if ts_dt is None or not time_filter(ts_dt):
                return None
        return (ts_dt or datetime.min, ts_str, content, user_name, kind)

    merged = []
    for ts_str, user_name, content, kind in extracted:
        row = keep(ts_str, user_name, content, kind)
        if row is not None:
            merged.append(row)

    merged.sort(key=lambda x: x[0])
    return [
        {
            "timestamp": ts_str,
            "speaker": user_name,
            "content": content_text,
            "kind": kind,
        }
        for _, ts_str, content_text, user_name, kind in merged
    ]


def process_sessions(conn, groups, output_format="tsv", start=None, end=None,
                     user=None, color=True):
    """
    按房间分块输出消息，块内按时间顺序，与旧版 process_log_file 一致。

    Args:
        groups: [(room_id, [session...])]。
        color: tsv 输出是否按 kind 着色（chat 无色）。
    """
    time_filter = build_time_filter(start, end)

    results = [
        (room, _extract_messages(conn, sesss, time_filter, user))
        for room, sesss in groups
    ]
    for room, messages in results:
        if not messages:
            print(
                f"警告: 房间 {room} 未提取到任何消息"
                "（场次可能为空，或被时间/用户条件过滤）。",
                file=sys.stderr,
            )

    if output_format == "json":
        payload = [
            {
                "room": room,
                "messages": [
                    {
                        "timestamp": m["timestamp"],
                        "speaker": m["speaker"],
                        "content": m["content"],
                    }
                    for m in messages
                ],
            }
            for room, messages in results
        ]
        print(json.dumps(payload, indent=2, ensure_ascii=False))
    elif output_format == "tsv":
        writer = csv.writer(sys.stdout, delimiter='\t', lineterminator='\n')
        if any(messages for _, messages in results):
            writer.writerow(["timestamp", "speaker", "content"])
        for _, messages in results:
            for msg in messages:
                content = msg.get("content", "")
                if color:
                    tint = KIND_COLORS.get(msg.get("kind"))
                    if tint:
                        content = f"{tint}{content}{_COLOR_RESET}"
                writer.writerow([
                    msg.get("timestamp", ""),
                    msg.get("speaker", ""),
                    content,
                ])
    else:
        print(
            f"错误: 不支持的输出格式 '{output_format}'。"
            "可选 'tsv' 或 'json'。",
            file=sys.stderr,
        )
        sys.exit(1)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="从 events.db 的分表中提取聊天、礼物、关注/分享、"
                    "粉丝团消息，支持场次指定、时间段过滤、单用户过滤，"
                    "按房间分块并按时间顺序输出。"
    )
    parser.add_argument(
        "-S", "--session",
        dest="session",
        default=None,
        metavar="SPEC",
        help="场次：'2026-10-11:1'（当天第1场）、'2026-10-11:-1'（最后一场）、"
             "'2026-10-11'（仅1场时直选）、或精确 session id。"
             "缺省取最新场次；给 --start/--end 时取相交场次。"
             "用 --list-sessions 查看清单。"
    )
    parser.add_argument(
        "--list-sessions",
        action="store_true",
        help="列出全部场次（含天内序号）后退出。",
    )
    parser.add_argument(
        "-r", "--room",
        dest="rooms",
        type=parse_room_arg,
        default=list(DEFAULT_ROOMS),
        metavar="ROOMS",
        help="房间号，逗号分隔，"
             "如 -r 74083423272,249069870647。"
             f"缺省: {','.join(DEFAULT_ROOMS)}。"
             "也用于过滤场次池和 -S 的天内序号编号。"
    )
    parser.add_argument(
        "-o", "--output-format",
        dest="output_format",
        choices=["tsv", "json"],
        default="tsv",
        help="输出格式，可选 'tsv' 或 'json' (默认为 'tsv')。"
    )
    parser.add_argument(
        "--start",
        type=parse_time_arg,
        metavar="TIME",
        help="起始时间，如 '2025-08-20 20:00'、'20:00'；"
             "仅给时分时按一天中的时刻过滤（任意日期生效）。"
    )
    parser.add_argument(
        "--end",
        type=parse_time_arg,
        metavar="TIME",
        help="结束时间；仅给日期 (如 '2025-08-20') 表示当天 23:59:59。"
    )
    parser.add_argument(
        "-u", "--user",
        dest="user",
        metavar="USERNAME",
        help="只输出匹配该用户的发言：支持 * 和 ? 通配符；"
             "不含通配符时按子串模糊匹配（不区分大小写）。"
    )
    parser.add_argument(
        "--no-color",
        action="store_true",
        help="tsv 输出不按类型着色（管道/重定向本就自动不着色）。",
    )
    parser.add_argument(
        "--db",
        dest="db",
        default=None,
        metavar="PATH",
        help=f"events.db 路径（缺省 {session_db.DEFAULT_DB_PATH}）。",
    )

    args = parser.parse_args()

    conn = session_db.open_db(args.db)

    if args.list_sessions:
        session_db.print_session_list(
            session_db.list_sessions(conn, rooms=args.rooms)
        )
        sys.exit(0)

    groups = select_sessions(
        conn, args.start, args.end, args.rooms, args.session
    )

    for room, sesss in groups:
        for s in sesss:
            print(
                f"房间 {room} 场次 {s['session']}"
                f"（{s['start']:%Y-%m-%d %H:%M:%S} ~"
                f" {s['end']:%Y-%m-%d %H:%M:%S}）",
                file=sys.stderr,
            )

    process_sessions(
        conn,
        groups,
        args.output_format,
        start=args.start,
        end=args.end,
        user=args.user,
        color=(
            not args.no_color
            and args.output_format == "tsv"
            and sys.stdout.isatty()
        ),
    )
