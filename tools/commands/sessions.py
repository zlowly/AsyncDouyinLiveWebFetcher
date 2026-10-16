#!/usr/bin/env python3
"""列出 events.db 的全部场次清单（统一入口 tool 的 sessions 子命令）。

等价于旧脚本里三处重复的 `--list-sessions` 早退行为，单独抽成一个子命令。
新增于 tools 统一 CLI 重构（P3），不与旧脚本共享参数定义，只薄壳转发。
"""
import argparse
import sys
from pathlib import Path

# 本文件在 tools/commands/ 下，比 tools/ 根下的命令模块深一层，
# 因此指向 tools/_lib 要多一次 .parent。
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "_lib"))
import dbcli  # noqa: E402
import session_db  # noqa: E402


def build_parser(prog=None):
    parser = argparse.ArgumentParser(
        prog=prog,
        description="列出 events.db 的全部场次（含天内序号），"
                    "可直接抄进 -S/--session。",
    )
    parser.add_argument(
        "-r", "--room",
        dest="rooms",
        type=dbcli.parse_room_arg,
        # 与 messages 的默认**不同**：这里缺省 None（列出全部房间的场次），
        # 不要与 process_log_sqlite.py 的非空 DEFAULT_ROOMS 统一。
        default=None,
        metavar="ROOMS",
        help="房间号，逗号分隔，如 -r 74083423272,249069870647。"
             "缺省列出全部房间的场次；给定后过滤场次池并重排天内序号。",
    )
    parser.add_argument(
        "--db",
        dest="db",
        default=None,
        metavar="PATH",
        help=f"events.db 路径（缺省 {session_db.DEFAULT_DB_PATH}）。",
    )
    return parser


def main(argv=None, prog=None):
    args = build_parser(prog).parse_args(argv)
    conn = session_db.open_db(args.db)
    session_db.print_session_list(
        session_db.list_sessions(conn, rooms=args.rooms)
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
