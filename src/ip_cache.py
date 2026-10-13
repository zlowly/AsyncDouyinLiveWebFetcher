"""ip_cache 数据的轻量 CLI：CSV 导入、待抓清单、单用户速查、状态。

取代已退役的 ~/scripts/track_with_duckdb.zsh 中 enrich/user/status 的
数据通路：ip_cache 现在是 events.db 的附属表（sec_uid -> IP 属地），
用户全集由 6 张 user-bearing 分表现算，不再依赖 ~/tracking.db。
fetch_ip_location.py 保持不变，它的 list.txt 由 pending 子命令生成。

用法（在项目根执行，或任意位置用绝对路径调用）：

    python src/ip_cache.py import-csv out.csv
    python src/ip_cache.py pending --fans > ~/scripts/list.txt
    python src/ip_cache.py user <id|昵称|抖音号>
    python src/ip_cache.py status

设计依据见 PLAN-sqlite-split.md Phase 5（未入库，路径
~/.opencode/plan/douyin-log-sqlite.md）。
"""

import argparse
import csv
import os
import sqlite3
import sys
import time

from store import DEFAULT_DB_PATH

# 带 user 字段的 6 张表（ev_rank / ev_stats / ev_control / ev_other 无 user）
_USER_TABLES = (
    "ev_chat",
    "ev_like",
    "ev_member",
    "ev_gift",
    "ev_social",
    "ev_notice",
)

# 与 track_with_duckdb.zsh 同款 upsert：新值非空才覆盖（COALESCE）。
_UPSERT_SQL = """
INSERT INTO ip_cache(sec_uid, ip_location, age, ip_status, ip_fetched_at)
VALUES (?, ?, ?, ?, ?)
ON CONFLICT(sec_uid) DO UPDATE SET
    ip_location = COALESCE(excluded.ip_location, ip_cache.ip_location),
    age         = COALESCE(excluded.age,         ip_cache.age),
    ip_status   = COALESCE(excluded.ip_status,   ip_cache.ip_status),
    ip_fetched_at = COALESCE(excluded.ip_fetched_at, ip_cache.ip_fetched_at)
"""

# 用户全集：user-bearing 表 UNION ALL，按 user_id 聚合
# （去重口径 = 按 user_id 取任意一行；fans_level 取曾达到的最高级）。
_PEOPLE_CTE = """
WITH people AS (
    SELECT user_id, user_name, sec_uid, fans_level, ts_epoch FROM ev_chat
    UNION ALL
    SELECT user_id, user_name, sec_uid, fans_level, ts_epoch FROM ev_like
    UNION ALL
    SELECT user_id, user_name, sec_uid, fans_level, ts_epoch FROM ev_member
    UNION ALL
    SELECT user_id, user_name, sec_uid, fans_level, ts_epoch FROM ev_gift
    UNION ALL
    SELECT user_id, user_name, sec_uid, fans_level, ts_epoch FROM ev_social
    UNION ALL
    SELECT user_id, user_name, sec_uid, fans_level, ts_epoch FROM ev_notice
), agg AS (
    SELECT user_id, max(user_name) AS user_name,
           max(sec_uid) AS sec_uid,
           max(fans_level) AS fans_level,
           max(ts_epoch) AS last_seen
    FROM people
    WHERE user_id IS NOT NULL AND sec_uid IS NOT NULL AND sec_uid <> ''
    GROUP BY user_id
)
"""

# 单用户速查的子查询：公共列的投影（专属列各表不同，不能 SELECT *）
_USER_PROJ = " UNION ALL ".join(
    f"""SELECT user_id, user_name, display_id, short_id, sec_uid,
        gender, pay_level, fans_level, ts_epoch FROM {t}"""
    for t in _USER_TABLES
)


def _open(path):
    if not os.path.exists(path):
        sys.exit(f"中止：数据库不存在 {path}")
    conn = sqlite3.connect(path)
    missing = [
        t
        for t in (*_USER_TABLES, "ip_cache")
        if not conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (t,)
        ).fetchone()
    ]
    if missing:
        # 迁移（migrate_split_events.py）早已完成并归档，不再是"跑一下就能
        # 修好"的问题。出现缺失只可能是库本身残缺或版本不匹配，需要人工介入。
        sys.exit(
            f"中止：{path} 缺少分表 {missing}\n"
            "分表迁移已于本项目早期完成，migrate_split_events.py 是归档脚本、"
            "不再是修复手段。多半是这份库本身残缺（导库不全 / 备份不完整 / "
            "用旧代码打开过），请确认库来源或联系维护者。"
        )
    return conn


def cmd_import_csv(conn, args):
    now = time.strftime("%Y-%m-%d %H:%M:%S")
    rows = []
    # fetch_ip_location.py 用 utf-8-sig 写（带 BOM），必须用 utf-8-sig
    # 读，否则首列键带 \ufeff 前缀，sec_uid 全部取不到。
    with open(args.csv, newline="", encoding="utf-8-sig") as fh:
        for record in csv.DictReader(fh):
            sec_uid = (record.get("sec_uid") or "").strip()
            if not sec_uid:
                continue
            age_text = (record.get("age") or "").strip()
            rows.append(
                (
                    sec_uid,
                    (record.get("ip_location") or "").strip() or None,
                    int(age_text) if age_text.isdigit() else None,
                    (record.get("status") or "").strip() or None,
                    now,
                )
            )
    conn.executemany(_UPSERT_SQL, rows)
    conn.commit()
    print(f"已 upsert {len(rows)} 行 -> ip_cache")


def _pending_where(args):
    conds = []
    if args.scope == "fans":
        conds.append("a.fans_level > 0")
    elif args.scope == "chatters":
        conds.append(
            "EXISTS (SELECT 1 FROM ev_chat c WHERE c.user_id = a.user_id)"
        )
    if args.active_days > 0:
        conds.append(
            f"a.last_seen > strftime('%s','now') - {args.active_days} * 86400"
        )
    # 「什么算待抓」：默认只补没抓过的；--retry-errors 让失败行也重试，
    # --refresh-days N 让 N 天前抓过的行过期重抓（与旧脚本口径一致）。
    pending = ["ip.sec_uid IS NULL"]
    if args.retry_errors:
        pending.append("ip.ip_status NOT IN ('ok', 'no_ip_location')")
    if args.refresh_days > 0:
        pending.append(
            f"ip.ip_fetched_at < datetime('now', '-{args.refresh_days} days')"
        )
    conds.append("(" + " OR ".join(pending) + ")")
    return "\n  AND ".join(conds)


def cmd_pending(conn, args):
    sql = (
        _PEOPLE_CTE
        + f"""
SELECT a.sec_uid FROM agg a
LEFT JOIN ip_cache ip ON ip.sec_uid = a.sec_uid
WHERE {_pending_where(args)}
ORDER BY a.last_seen DESC
"""
    )
    for (sec_uid,) in conn.execute(sql):
        print(sec_uid)


def cmd_user(conn, args):
    pattern = args.query
    rows = conn.execute(
        f"""
SELECT user_id, max(user_name) AS user_name,
       max(display_id) AS display_id, max(short_id) AS short_id,
       max(sec_uid) AS sec_uid, max(gender) AS gender,
       max(pay_level) AS pay_level, max(fans_level) AS fans_level,
       count(*) AS records,
       datetime(max(ts_epoch), 'unixepoch', 'localtime') AS last_seen
FROM ({_USER_PROJ})
WHERE user_id = ? OR user_name LIKE '%' || ? || '%' OR display_id = ?
GROUP BY user_id ORDER BY max(ts_epoch) DESC LIMIT 20
""",
        (pattern, pattern, pattern),
    ).fetchall()
    if not rows:
        print("未找到匹配的用户")
        return
    genders = {None: "?", 0: "保密", 1: "男", 2: "女"}
    for row in rows:
        (
            uid,
            name,
            display,
            short_id,
            sec_uid,
            gender,
            pay_level,
            fans_level,
            records,
            last_seen,
        ) = row
        print(f"用户ID  {uid}")
        print(f"昵称    {name or '<空>'}    抖音号  {display or '<空>'}")
        print(f"short_id {short_id or '-'}    sec_uid {sec_uid or '-'}")
        print(
            f"性别    {genders.get(gender, gender)}"
            f"    付费等级 {pay_level if pay_level is not None else '-'}"
            f"    粉丝等级 {fans_level if fans_level is not None else '-'}"
        )
        print(f"记录数  {records}    最后出现 {last_seen}")
        if sec_uid:
            ip = conn.execute(
                "SELECT ip_location, age, ip_status, ip_fetched_at"
                " FROM ip_cache WHERE sec_uid = ?",
                (sec_uid,),
            ).fetchone()
            if ip is None:
                print("属地    尚未抓取（pending 子命令可生成待抓清单）")
            elif ip[2] == "ok":
                age = f"（{ip[1]} 岁）" if ip[1] is not None else ""
                print(f"属地    {ip[0]}{age}    抓取于 {ip[3]}")
            elif ip[2] == "no_ip_location":
                print(f"属地    抖音主页未展示    抓取于 {ip[3]}")
            else:
                print(f"属地    抓取失败：{ip[2]}    抓取于 {ip[3]}")
        else:
            print("属地    该用户无 sec_uid，无法抓取")
        print()


def cmd_status(conn, args):
    total = conn.execute("SELECT count(*) FROM ip_cache").fetchone()[0]
    print(f"ip_cache 共 {total} 行；抓取状态分布：")
    for status, n in conn.execute(
        "SELECT ip_status, count(*) FROM ip_cache"
        " GROUP BY ip_status ORDER BY 2 DESC"
    ):
        print(f"  {status or '<NULL>'}: {n}")
    for scope in ("fans", "all", "chatters"):
        args.scope = scope
        args.retry_errors = False
        args.refresh_days = 0
        args.active_days = 0
        n = conn.execute(
            _PEOPLE_CTE
            + f"""
SELECT count(*) FROM agg a
LEFT JOIN ip_cache ip ON ip.sec_uid = a.sec_uid
WHERE {_pending_where(args)}
"""
        ).fetchone()[0]
        print(f"候选池（--{scope}，默认待抓口径）: {n}")


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--db", default=DEFAULT_DB_PATH)
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("import-csv", help="导入 fetch_ip_location.py 的 CSV")
    p.add_argument("csv")

    p = sub.add_parser("pending", help="打印尚未抓取的 sec_uid（喂 list.txt）")
    scope = p.add_mutually_exclusive_group()
    scope.add_argument(
        "--fans",
        dest="scope",
        action="store_const",
        const="fans",
        help="曾有粉丝团的用户（默认）",
    )
    scope.add_argument(
        "--all",
        dest="scope",
        action="store_const",
        const="all",
        help="全部用户",
    )
    scope.add_argument(
        "--chatters",
        dest="scope",
        action="store_const",
        const="chatters",
        help="发过言的用户",
    )
    p.set_defaults(scope="fans")
    p.add_argument("--retry-errors", action="store_true", help="失败行也重试")
    p.add_argument(
        "--refresh-days",
        type=int,
        default=0,
        metavar="N",
        help="N 天前抓过的行过期重抓",
    )
    p.add_argument(
        "--active-days",
        type=int,
        default=0,
        metavar="N",
        help="只算最后出现在 N 天内的用户",
    )

    p = sub.add_parser("user", help="单用户速查（id / 昵称 / 抖音号）")
    p.add_argument("query")

    sub.add_parser("status", help="抓取进度与候选池")

    args = ap.parse_args()
    conn = _open(args.db)
    try:
        {
            "import-csv": cmd_import_csv,
            "pending": cmd_pending,
            "user": cmd_user,
            "status": cmd_status,
        }[args.cmd](conn, args)
    finally:
        conn.close()


if __name__ == "__main__":
    main()
