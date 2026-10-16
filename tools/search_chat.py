#!/usr/bin/env python3
"""跨全部场次检索 events.db 的弹幕正文（ev_chat）。

与 process_log_sqlite.py 的分工：那个是"把某一场的消息倒出来"，
这个是"在全库 15 万条弹幕里找某个词"，缺省不加任何场次/房间限制。

两条检索路径，按词长自动切换（-k 少于 3 字走 LIKE，--like 可强制）：

  >= 3 字   chat_fts 的 trigram 倒排索引，约 10ms
            SELECT ... FROM chat_fts f JOIN ev_chat c ON c.id = f.rowid
            WHERE chat_fts MATCH 'content:"词"'
  < 3 字    ev_chat.content LIKE，全表扫约 80ms

两个必须知道的细节（都实测过，踩了就是静默给错结果）：

1. **MATCH 必须带 content: 列前缀**。chat_fts 有 content 和 user_name
   两列，不带前缀会连昵称一起搜：搜"哈哈哈"时 1655 = 正文 845 +
   昵称 831，而只看正文就该是 845。
2. **trigram 检索不到 1~2 字的词**，不是报错是静默返回 0 条，所以短词
   必须走 LIKE。两条路径的结果已验证严格相等。

MATCH 参数整体包成短语、`"` 翻倍；LIKE 参数转义 \\ % _ 并配 ESCAPE。
直接拿用户输入拼 MATCH 会抛 fts5: syntax error，拿去拼 LIKE 会把
% 和 _ 当通配符。

用法示例：
  python tools/search_chat.py -k 哈哈哈                  # 全库全房间
  python tools/search_chat.py -k 主播 --limit 0          # 2 字词走 LIKE
  python tools/search_chat.py -k 6666 -S 2026-10-15      # 限某一场
  python tools/search_chat.py -k 笑 --start 20:00 --end 22:00
  python tools/search_chat.py -k 礼物 -o json
"""
import argparse
import csv
import json
import sys
from datetime import timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import dbcli  # noqa: E402
import session_db  # noqa: E402

# trigram 分词器要求 >= 3 字符，短于此的词检索不到，必须走 LIKE。
MIN_FTS_LEN = 3

_COLUMNS = ("ts", "room_id", "user_name", "content")
_TSV_HEADER = ("timestamp", "room_id", "speaker", "content")


def escape_like(term):
    """
    把词转成 LIKE 的字面量匹配模式。
    顺序要紧：先转义反斜杠，否则后面补的反斜杠会被二次转义掉。
    """
    for ch in ("\\", "%", "_"):
        term = term.replace(ch, "\\" + ch)
    return "%" + term + "%"


def escape_fts(term):
    """包成 FTS5 短语查询（content: 列限定 + 双引号内翻倍引号）。"""
    return 'content:"' + term.replace('"', '""') + '"'


def _pushable(start, end):
    """
    起止能不能下推成 ts_epoch 区间。
    纯时刻（如 '20:00'）的语义是"一天中的时刻、跨所有日期"，
    epoch 区间表达不了，只能退回 Python 谓词。
    """
    for bound in (start, end):
        if bound is not None and bound[1] == "time":
            return False
    return True


def _epoch_bounds(start, end):
    """把 (dt, 粒度) 元组转成 SQL 的 (start_epoch, end_epoch)。"""
    low = high = None
    if start is not None:
        low = start[0].timestamp()
    if end is not None:
        end_dt, end_gran = end
        # 纯日期的 --end 含义是当天 23:59:59.999999，
        # 与 build_time_filter 一致。
        if end_gran == "date":
            end_dt = end_dt + timedelta(days=1) - timedelta(microseconds=1)
        high = end_dt.timestamp()
    return low, high


def build_filters(rooms, session_id, start, end):
    """
    拼出附加的 WHERE 片段与参数（不含关键词本身的过滤）。
    返回 (clauses, params)。
    """
    clauses = []
    params = []
    if rooms:
        placeholders = ", ".join("?" * len(rooms))
        clauses.append(f"c.room_id IN ({placeholders})")
        params.extend(rooms)
    if session_id:
        clauses.append("c.session = ?")
        params.append(session_id)
    if _pushable(start, end):
        low, high = _epoch_bounds(start, end)
        if low is not None:
            clauses.append("c.ts_epoch >= ?")
            params.append(low)
        if high is not None:
            clauses.append("c.ts_epoch <= ?")
            params.append(high)
    return clauses, params


def _select_sql(fts, columns, where, order, limit):
    """拼 SELECT。fts 与 like 两条路径共用 WHERE 片段。"""
    source = (
        "chat_fts f JOIN ev_chat c ON c.id = f.rowid"
        if fts
        else "ev_chat c"
    )
    return (
        f"SELECT {columns} FROM {source}"
        f" WHERE {where}{order}{limit}"
    )


def search(conn, term, *, rooms=None, session_id=None, start=None, end=None,
           use_like=False, order="desc", limit=200):
    """
    检索并返回 (命中总条数, 已按 order 排序的 [行, ...])。

    先算总数（只 WHERE 不排序），再取本页；总数与页大小不一致时
    调用方向 stderr 提示截断。
    """
    term = term.strip()
    if not term:
        raise ValueError("检索词不能为空")

    fts = not use_like and len(term) >= MIN_FTS_LEN
    clauses, params = build_filters(rooms, session_id, start, end)

    if fts:
        keyword_sql = "chat_fts MATCH ?"
        keyword_params = [escape_fts(term)]
    else:
        keyword_sql = "c.content LIKE ? ESCAPE '\\'"
        keyword_params = [escape_like(term)]

    where = " AND ".join([keyword_sql] + clauses)
    order_sql = f" ORDER BY c.ts_epoch {order}"
    args = keyword_params + params
    count = conn.execute(
        _select_sql(fts, "COUNT(*)", where, "", ""), args
    ).fetchone()[0]

    # 退回 Python 谓词时不能在 SQL 里 LIMIT —— 先截断再过滤会漏行。
    post_filter = None
    if not _pushable(start, end):
        post_filter = dbcli.build_time_filter(start, end)
        rows = conn.execute(
            _select_sql(
                fts,
                ", ".join(f"c.{name}" for name in _COLUMNS),
                where,
                order_sql,
                "",
            ),
            args,
        )
        rows = [row for row in rows if _keep(post_filter, row[0])]
        total = len(rows)
        return total, rows[:limit] if limit else rows

    total = count
    limit_sql = f" LIMIT {int(limit)}" if limit else ""
    rows = conn.execute(
        _select_sql(
            fts,
            ", ".join(f"c.{name}" for name in _COLUMNS),
            where,
            order_sql,
            limit_sql,
        ),
        args,
    ).fetchall()
    return total, rows


def _keep(post_filter, ts_str):
    """Python 后置时间过滤；时间戳解析不出来一律丢弃（与旧工具一致）。"""
    ts_dt = dbcli.parse_log_timestamp(ts_str)
    return ts_dt is not None and post_filter(ts_dt)


def _flatten(value):
    """TSV 是逐行文本，正文里的换行/制表符会撑坏行，统一替换成空格。"""
    if value is None:
        return ""
    return str(value).replace("\r", " ").replace("\n", " ").replace("\t", " ")


def emit(rows, output_format, stream=sys.stdout):
    """输出命中明细，stdout 保持纯净以便管道。"""
    if output_format == "json":
        payload = [
            dict(zip(_TSV_HEADER, [_flatten(v) for v in row]))
            for row in rows
        ]
        print(json.dumps(payload, indent=2, ensure_ascii=False), file=stream)
        return

    writer = csv.writer(stream, delimiter="\t", lineterminator="\n")
    writer.writerow(_TSV_HEADER)
    for row in rows:
        writer.writerow([_flatten(value) for value in row])


def build_parser():
    parser = argparse.ArgumentParser(
        description="在 events.db 的 ev_chat 里跨全部场次检索弹幕正文"
                    "（>= 3 字走 FTS 索引，1~2 字自动降级 LIKE），"
                    "支持场次/时间/房间过滤，输出命中明细 tsv 或 json。"
    )
    parser.add_argument(
        "-k", "--keyword",
        dest="keyword",
        required=True,
        metavar="TERM",
        help="要检索的词（子串匹配）。>= 3 字走 FTS 索引，1~2 字自动"
             "改走 LIKE（trigram 索引检索不到这么短的词）。"
             # argparse 对 help 做 % 格式化，字面量 % 必须写 %%
             '含 %% _ \\ 时按字面量处理，含 " 也不会破坏查询。'
    )
    parser.add_argument(
        "--like",
        action="store_true",
        help="强制走 LIKE 全表扫而不是 FTS 索引。用于调试 3 字门槛，"
             "或需要与 FTS 不完全一致的大小写/模糊行为时。"
    )
    parser.add_argument(
        "-S", "--session",
        dest="session",
        default=None,
        metavar="SPEC",
        help="场次：'2026-10-11:1'（当天第1场）、'2026-10-11:-1'（最后一场）、"
             "'2026-10-11'（仅1场时直选）、或精确 session id。"
             "缺省不限场次，检索全库；用 --list-sessions 查看清单。"
    )
    parser.add_argument(
        "--list-sessions",
        action="store_true",
        help="列出全部场次（含天内序号）后退出。",
    )
    parser.add_argument(
        "-r", "--room",
        dest="rooms",
        type=dbcli.parse_room_arg,
        default=None,
        metavar="ROOMS",
        help="房间号，逗号分隔，如 -r 74083423272,249069870647。"
             "缺省不限房间（检索全库）。也用于过滤场次池和 -S 的天内编号。"
    )
    parser.add_argument(
        "--start",
        type=dbcli.parse_time_arg,
        metavar="TIME",
        help="起始时间，如 '2025-08-20 20:00'、'20:00'；"
             "仅给时分时按一天中的时刻过滤（任意日期生效，支持跨午夜）。"
    )
    parser.add_argument(
        "--end",
        type=dbcli.parse_time_arg,
        metavar="TIME",
        help="结束时间；仅给日期 (如 '2025-08-20') 表示当天 23:59:59。"
    )
    parser.add_argument(
        "-o", "--output-format",
        dest="output_format",
        choices=["tsv", "json"],
        default="tsv",
        help="输出格式，可选 'tsv' 或 'json' (默认为 'tsv')。"
    )
    parser.add_argument(
        "--order",
        choices=["asc", "desc"],
        default="desc",
        help="按时间排序方向，'desc' 最新在前（默认），'asc' 最早在前。"
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=200,
        metavar="N",
        help="最多显示多少条命中，0 表示不限（默认为 200）。"
             "总命中数始终打到 stderr。"
    )
    parser.add_argument(
        "--db",
        dest="db",
        default=None,
        metavar="PATH",
        help=f"events.db 路径（缺省 {session_db.DEFAULT_DB_PATH}）。"
    )
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)

    conn = session_db.open_db(args.db)

    if args.list_sessions:
        session_db.print_session_list(
            session_db.list_sessions(conn, rooms=args.rooms)
        )
        return 0

    session_id = None
    if args.session:
        sessions = session_db.list_sessions(conn, rooms=args.rooms)
        session_id = session_db.resolve_session(args.session, sessions)[
            "session"
        ]

    try:
        total, rows = search(
            conn,
            args.keyword,
            rooms=args.rooms,
            session_id=session_id,
            start=args.start,
            end=args.end,
            use_like=args.like,
            order=args.order,
            limit=max(args.limit, 0),
        )
    except ValueError as exc:
        print(f"错误: {exc}", file=sys.stderr)
        return 1

    if not total:
        # 对齐 grep 约定：零命中退 1，stdout 不出任何东西。
        print(f"共 0 条命中：{args.keyword}", file=sys.stderr)
        return 1

    emit(rows, args.output_format)
    if len(rows) < total:
        print(
            f"共 {total} 条命中，已显示 {len(rows)} 条"
            f"（--limit {args.limit}，--limit 0 可看全部）",
            file=sys.stderr,
        )
    else:
        print(f"共 {total} 条命中", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
