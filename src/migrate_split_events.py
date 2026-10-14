"""一次性迁移：旧单表 events -> 10 张 ev_* 分表（DuckDB 退役路线）。

用法（先在副本上演练，确认无写者后再跑正式库）：

    python src/migrate_split_events.py --db /tmp/events-test.db
    python src/migrate_split_events.py

安全模型：
  - 全程单个大事务（BEGIN IMMEDIATE ... COMMIT），中途崩溃自动回滚，
    原库无损；
  - 事务内先用 backup API 落一份快照，作为第二道保险；
  - 迁移前逐表逐 method 对账，不一致直接 ROLLBACK；
  - short_id / display_id / sec_uid / gift 投影靠注册 UDF 现场解压
    payload 抽取（INSERT ... SELECT 解不了 zlib）。

DDL 与路由常量全部 import 自 store，保证与运行时写入口径零漂移。
注意：本脚本逐句 execute TABLES_DDL，**不能**改用 executescript ——
isolation_level=None 下它会隐式 COMMIT 破坏大事务。

设计依据见 PLAN-sqlite-split.md Phase 2。
"""

import argparse
import json
import os
import sqlite3
import sys
import time
import zlib

from store import (  # noqa: E402 - sys.path[0] 即 src/
    _COMMON_INSERT_COLS,
    _METHOD_TABLES,
    _TABLE_EXTRA_COLS,
    _TABLES,
    DEFAULT_DB_PATH,
    FTS_DDL,
    TABLES_DDL,
)

BATCH = 50_000


def _decompress(blob):
    return json.loads(zlib.decompress(blob).decode("utf-8"))


def _user_ident(blob):
    """payload BLOB -> JSON 数组 [shortId, displayId, secUid] 或 NULL。"""
    try:
        data = _decompress(blob)
    except Exception:  # noqa: BLE001 - 坏行不阻断整批迁移
        return None
    user = data.get("user")
    if not isinstance(user, dict):
        return None
    return json.dumps(
        [user.get("shortId"), user.get("displayId"), user.get("secUid")],
        ensure_ascii=False,
    )


def _gift_json(blob):
    """payload BLOB -> gift 子对象的 JSON 文本或 NULL（旧表无投影列）。"""
    try:
        data = _decompress(blob)
    except Exception:  # noqa: BLE001
        return None
    gift = data.get("gift")
    return (
        json.dumps(gift, ensure_ascii=False)
        if isinstance(gift, dict)
        else None
    )


def _combo_json(blob):
    """payload BLOB -> 顶层 comboCount 的整数或 NULL（与 store 投影一致）。"""
    try:
        data = _decompress(blob)
    except Exception:  # noqa: BLE001
        return None
    value = data.get("comboCount")
    if isinstance(value, bool):
        return None
    if isinstance(value, str):
        try:
            value = int(value)
        except ValueError:
            return None
    if isinstance(value, float) and value.is_integer():
        return int(value)
    return value if isinstance(value, int) else None


# ident / gift_json 两个 UDF 的 SQL 侧引用（表达式 + 目标列别名）
_IDENT_EXPRS = (
    ("json_extract(ident(payload), '$[0]')", "short_id"),
    ("json_extract(ident(payload), '$[1]')", "display_id"),
    ("json_extract(ident(payload), '$[2]')", "sec_uid"),
)

# 旧 events 表已有、可直接拷贝的专属列；ev_gift 是例外：旧表没有投影，
# 两列用 gift_json UDF 现场抽取。元素是 (SELECT 表达式, 目标列名)。
_LEGACY_EXTRA = {
    "ev_chat": [("content", "content")],
    "ev_like": [("like_count", "like_count"), ("total", "total")],
    "ev_member": [("member_count", "member_count"), ("action", "action")],
    "ev_gift": [
        ("json_extract(gift_json(payload), '$.name')", "gift_name"),
        # comboCount 在 payload 顶层（不在 gift 子对象里），与
        # store._project_gift 的取键路径保持一致。
        ("combo_json(payload)", "gift_count"),
    ],
    "ev_social": [("action", "action"), ("follow_count", "follow_count")],
    "ev_rank": [
        ("total", "total"),
        ("total_user", "total_user"),
        ("ranks", "ranks"),
    ],
    "ev_stats": [("total", "total")],
    "ev_notice": [("content", "content"), ("action", "action")],
    "ev_control": [("status", "status")],
    "ev_other": [],
}

# method -> table 的反转：每张目标表的正向 method 集合
_METHOD_GROUPS = {}
for _method, _table in _METHOD_TABLES.items():
    _METHOD_GROUPS.setdefault(_table, []).append(_method)

_ALL_METHODS = tuple(_METHOD_TABLES)
_METHOD_FILTER = {
    t: f"method IN ({','.join('?' * len(ms))})"
    for t, ms in _METHOD_GROUPS.items()
}
# 兜底表：排除全部已知 method（StreamAdaptation 与未知 method 一并落入）
_METHOD_FILTER["ev_other"] = (
    "method NOT IN (" + ",".join("?" * len(_ALL_METHODS)) + ")"
)


def _filter_params(table):
    if table == "ev_other":
        return _ALL_METHODS
    return tuple(_METHOD_GROUPS[table])


def _dest_cols(table):
    return ", ".join(
        ("id",)
        + _COMMON_INSERT_COLS
        + tuple(alias for _, alias in _LEGACY_EXTRA[table])
    )


def _select_parts(table):
    udf = {alias: expr for expr, alias in _IDENT_EXPRS}
    parts = ["id"]
    parts.extend(udf.get(col, col) for col in _COMMON_INSERT_COLS)
    parts.extend(src for src, _ in _LEGACY_EXTRA[table])
    return parts


def _copy_table(conn, table):
    """按 method 过滤 + id 区间分批 INSERT ... SELECT，返回拷贝行数。"""
    sql = (
        f"INSERT INTO {table} ({_dest_cols(table)}) "
        f"SELECT {', '.join(_select_parts(table))} FROM events"
        f" WHERE {_METHOD_FILTER[table]} AND id BETWEEN ? AND ?"
    )
    params = _filter_params(table)
    lo, hi = conn.execute(
        f"SELECT min(id), max(id) FROM events WHERE {_METHOD_FILTER[table]}",
        params,
    ).fetchone()
    if lo is None:
        return 0
    n = 0
    for start in range(lo, hi + 1, BATCH):
        end = min(start + BATCH - 1, hi)
        cur = conn.execute(sql, params + (start, end))
        n += cur.rowcount
    return n


def _expected_counts(per_method):
    """旧库 per-method 计数 -> 每张目标表的预期行数。"""
    expected = {}
    for table in _TABLES:
        if table == "ev_other":
            continue
        expected[table] = sum(
            per_method.get(m, 0) for m in _METHOD_GROUPS.get(table, ())
        )
    expected["ev_other"] = sum(per_method.values()) - sum(expected.values())
    return expected


def _reconcile(conn, per_method, expected):
    """逐表对账；不一致时打印差异并 SystemExit（触发 ROLLBACK）。"""
    problems = []
    for table in _TABLES:
        actual = conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
        if actual != expected[table]:
            problems.append((table, expected[table], actual))
    if not problems:
        return
    print("对账失败：", file=sys.stderr)
    for table, exp, act in problems:
        print(
            f"  {table:<10} 预期 {exp:>10,}  实际 {act:>10,}", file=sys.stderr
        )
    print("旧库 per-method 基准：", file=sys.stderr)
    for method, count in sorted(per_method.items(), key=lambda kv: -kv[1]):
        print(f"  {method:<45} {count:>10,}", file=sys.stderr)
    raise SystemExit(1)


def _snapshot_path(db_path, snapshot_dir):
    directory = snapshot_dir or os.path.dirname(os.path.abspath(db_path))
    stamp = time.strftime("%Y%m%d-%H%M%S")
    return os.path.join(directory, f"events.db.bak-{stamp}")


def _check_preconditions(conn):
    for table in _TABLES:
        row = conn.execute(
            "SELECT count(*) FROM sqlite_master WHERE type='table' AND name=?",
            (table,),
        ).fetchone()
        if row[0]:
            sys.exit(f"中止：{table} 已存在（上一次迁移未完成？先恢复快照）")
    if not conn.execute(
        "SELECT count(*) FROM sqlite_master WHERE type='table'"
        " AND name='events'"
    ).fetchone()[0]:
        sys.exit("中止：旧表 events 不存在，无可迁移数据")


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--db", default=DEFAULT_DB_PATH)
    ap.add_argument(
        "--vacuum",
        action="store_true",
        help="完成后 VACUUM 回收空间（需双倍磁盘，默认关）",
    )
    ap.add_argument(
        "--snapshot-dir",
        default=None,
        help="快照目录，缺省 = 库所在目录",
    )
    args = ap.parse_args()

    # 目标列别名必须与 store 的插入列集合一致（防两边漂移）。只比前缀：
    # store 后来给 ev_gift 补过列（trace_id 等），那是本脚本之后的事。
    for table in _TABLES:
        aliases = tuple(alias for _, alias in _LEGACY_EXTRA[table])
        actual = _TABLE_EXTRA_COLS[table][: len(aliases)]
        assert aliases == actual, (table, aliases)

    conn = sqlite3.connect(args.db, isolation_level=None)
    conn.create_function("ident", 1, _user_ident, deterministic=True)
    conn.create_function("gift_json", 1, _gift_json, deterministic=True)
    conn.create_function("combo_json", 1, _combo_json, deterministic=True)

    _check_preconditions(conn)

    started = time.monotonic()
    # BEGIN IMMEDIATE 拿写锁，确保没有并发写者
    conn.execute("BEGIN IMMEDIATE")
    try:
        # 快照：不能在同一连接上 backup——该连接持有 BEGIN IMMEDIATE
        # 写事务时 sqlite3_backup_step 会永久 BUSY 死锁。改用第二个连接
        # 读同一库；写锁已在手，不会有并发写者，数据即当前快照。
        bak = _snapshot_path(args.db, args.snapshot_dir)
        snap_src = sqlite3.connect(args.db)
        dst = sqlite3.connect(bak)
        try:
            snap_src.backup(dst)
        finally:
            dst.close()
            snap_src.close()
        print(f"快照: {bak}")

        # 拆除旧 FTS 与触发器
        for trig in ("events_ai", "events_ad", "events_au"):
            conn.execute(f"DROP TRIGGER IF EXISTS {trig}")
        conn.execute("DROP TABLE IF EXISTS chat_fts")

        # 建 10 张新表 + 索引 + 账本/ip_cache（逐句，勿改 executescript）
        for stmt in TABLES_DDL:
            conn.execute(stmt)

        per_method = dict(
            conn.execute(
                "SELECT method, count(*) FROM events GROUP BY method"
            ).fetchall()
        )
        expected = _expected_counts(per_method)

        total_old = conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]
        migrated = 0
        for table in _TABLES:
            n = _copy_table(conn, table)
            print(f"  {table:<10} {n:>10,} 行")
            migrated += n
        if migrated != total_old:
            raise RuntimeError(
                f"总行数不符：旧 {total_old:,} vs 新 {migrated:,}"
            )

        _reconcile(conn, per_method, expected)

        conn.execute("DROP TABLE events")
        for stmt in FTS_DDL:
            conn.execute(stmt)
        conn.execute("INSERT INTO chat_fts(chat_fts) VALUES('rebuild')")
        conn.execute("ANALYZE")

        conn.execute("COMMIT")
    except BaseException:
        # SQLITE_FULL 时 SQLite 可能已自动回滚，再 ROLLBACK 会报
        # "no transaction is active"，掩盖原始错误。
        try:
            conn.execute("ROLLBACK")
        except sqlite3.Error as exc:
            print(f"ROLLBACK 跳过：{exc}", file=sys.stderr)
        raise

    if args.vacuum:
        print("VACUUM 中（需要耐心与双倍磁盘）…")
        conn.execute("VACUUM")
    conn.close()
    elapsed = time.monotonic() - started
    print(f"迁移完成：{total_old:,} 行，耗时 {elapsed:.1f} 秒。")


if __name__ == "__main__":
    main()
