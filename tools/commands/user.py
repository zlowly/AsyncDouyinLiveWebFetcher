#!/usr/bin/env python3
"""单用户速查：资料 / 变化时间线 / 最近发言 / IP 属地。

从 events.db 的 6 张 user-bearing 分表（ev_chat / ev_like / ev_member /
ev_gift / ev_social / ev_notice）取用户快照，移植自旧 zsh 单用户速查脚本
（已删除）。ev_other / ev_rank 的 user_id 全为 NULL，不参与。

与旧脚本的差别（都是修 bug，不是改版）：

1. 候选表里的昵称/抖音号取**最近一行**的值，而不是 max(user_name)。
   max() 按字节序取最大，会把旧昵称（如「咲夜（已急哭版）」）当成最新。
2. 时间戳一律本地化：旧脚本用 sqlite 的 datetime(...,'unixepoch')，
   输出的是 UTC，比 ev_chat.ts（本地时间）差 8 小时。
3. 候选搜索只扫一遍（一条 6-way UNION ALL 查询），旧脚本扫两遍。
4. 新增【变化时间线】：按 ts_epoch 升序遍历该用户的全部快照，字段值
   变化时记一条（NULL/空串视为未知，不作为变化）。
5. 「出没」不再数原始行数（6 张表里 ev_like 点赞占大头，行数不代表活跃），
   改看 session（= app 启动时刻 YYYY-MM-DD_HH-MM-SS）：
   **直播日** = count(DISTINCT session 日期前缀)、**场次** =
   count(DISTINCT session)（跨午夜场归启动日），并附【近 6 个月】逐月
   天数/场次（缺月补 `0 天 / 0 场`）。
6. ip_cache 没记录时默认当场抓取：自动定位能
   `from scrapling.fetchers import Fetcher` 的解释器（SCRAPLING_PY 可覆盖），
   跑 tools/fetch_ip_location.py 生成 CSV，再把 CSV 直接 upsert 进 ip_cache；
   `--no-fetch` 可关闭。

用法示例：
  python tools/tool user 咲夜
  python tools/tool user '主播*' -n 20
  python tools/tool user 4132408461370336 -y --no-fetch
  SCRAPLING_PY=/path/to/env/bin/python python tools/tool user 咲夜
"""
import argparse
import glob
import os
import sqlite3
import subprocess
import sys
import tempfile
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

# 与 sessions.py 一致：命令模块在 tools/commands/ 下，多一次 .parent 才到
# tools/_lib。
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "_lib"))
import session_db  # noqa: E402

TOOLS_DIR = Path(__file__).resolve().parent.parent
PROJ = TOOLS_DIR.parent

# 候选表最多列这么多，再多只提示；交互选择的范围也以它为准。
CAND_LIMIT = 50

# 6 张带用户画像的分表。专属列各表不同，只能显式取公共列并集。
USER_TABLES = (
    "ev_chat",
    "ev_like",
    "ev_member",
    "ev_gift",
    "ev_social",
    "ev_notice",
)

_SNAPSHOT_COLS = (
    "user_id",
    "user_name",
    "display_id",
    "sec_uid",
    "gender",
    "pay_level",
    "fans_level",
    "ts_epoch",
)

# UNION 投影比「快照列」多一个 session：活跃度统计（直播日/场次）要用它。
# fetch_snapshots / 候选查询各自显式列名，所以这里加列不影响它们的顺序。
_UNION_SQL = " UNION ALL ".join(
    "SELECT " + ", ".join(_SNAPSHOT_COLS + ("session",)) + f" FROM {table}"
    for table in USER_TABLES
)

# 参与「最新值 / 变化时间线」的字段（按时间线里的展示顺序）。
_TRACKED_FIELDS = (
    ("user_name", "昵称"),
    ("display_id", "抖音号"),
    ("sec_uid", "secUid"),
    ("gender", "性别"),
    ("pay_level", "付费等级"),
    ("fans_level", "粉丝等级"),
)

# 【近 6 个月】窗口长度（含当月）。
_MONTH_WINDOW = 6


def glob_to_like(pattern):
    """glob -> LIKE 模式：* -> %，? -> _，字面 % _ \\ 用反斜杠转义。"""
    out = []
    for ch in pattern:
        if ch == "*":
            out.append("%")
        elif ch == "?":
            out.append("_")
        elif ch in ("%", "_", "\\"):
            # ESCAPE '\' 下反斜杠本身也要翻倍，否则它会把后一个字符吃掉。
            out.append("\\" + ch)
        else:
            out.append(ch)
    return "".join(out)


def _local(ts_epoch):
    """epoch 秒 -> 本地时间文本（与 ev_chat.ts 同一时区口径）。"""
    return datetime.fromtimestamp(ts_epoch).strftime("%Y-%m-%d %H:%M:%S")


def _recent_months(count=_MONTH_WINDOW):
    """含当月在内的最近 count 个 YYYY-MM 标签（升序，本地时间）。"""
    year, month = datetime.now().year, datetime.now().month
    labels = []
    for _ in range(count):
        labels.append(f"{year:04d}-{month:02d}")
        month -= 1
        if month == 0:
            year, month = year - 1, 12
    return list(reversed(labels))


def _known(value):
    """NULL / 空串算未知；0 是合法值（gender 0=保密、level 0=无）。"""
    if value is None:
        return False
    return not (isinstance(value, str) and value.strip() == "")


def _gender_label(value):
    if value == 1:
        return "男"
    if value == 2:
        return "女"
    return "未知"


def _field_value(field, value):
    """时间线里字段值的展示形式。"""
    if field == "gender":
        return _gender_label(value)
    return str(value)


def search_candidates(conn, like):
    """一次扫描 6 张表，按最近出现时间倒序列出候选用户。

    用 SQLite 的 bare-column 规则：查询里**只有一个 max() 聚合**，所以
    user_name / display_id 取自 max(ts_epoch) 那一行 —— 即最新昵称。
    出没口径是 session 维度（直播日/场次），不是原始行数。
    """
    return conn.execute(
        f"""
        WITH p AS ({_UNION_SQL})
        SELECT user_id, user_name, display_id,
               count(DISTINCT substr(session, 1, 10)) AS days,
               count(DISTINCT session) AS sessions,
               max(ts_epoch) AS last
        FROM p
        WHERE user_name LIKE ? ESCAPE '\\'
           OR display_id LIKE ? ESCAPE '\\'
           OR user_id LIKE ? ESCAPE '\\'
        GROUP BY user_id
        ORDER BY last DESC
        """,
        (like, like, like),
    ).fetchall()


def fetch_snapshots(conn, user_id):
    """按 ts_epoch 升序取该用户的全部快照（6 张表并集）。"""
    return conn.execute(
        f"""
        WITH p AS ({_UNION_SQL})
        SELECT user_name, display_id, sec_uid, gender,
               pay_level, fans_level, ts_epoch
        FROM p WHERE user_id = ? ORDER BY ts_epoch ASC
        """,
        (user_id,),
    ).fetchall()


def activity_stats(conn, user_id):
    """活跃度：直播日 / 场次 / 首末时间 + 近 6 个月逐月分布。

    直播日 = count(DISTINCT substr(session,1,10))；场次 =
    count(DISTINCT session)。session 形如 `YYYY-MM-DD_HH-MM-SS`（app 启动
    时刻），所以跨午夜场归启动日。months 恒为 6 项、按时间升序，缺月补
    (ym, 0, 0)。
    """
    days, sessions, first_ts, last_ts = conn.execute(
        f"""
        WITH p AS ({_UNION_SQL})
        SELECT count(DISTINCT substr(session, 1, 10)) AS days,
               count(DISTINCT session) AS sessions,
               min(ts_epoch) AS first, max(ts_epoch) AS last
        FROM p WHERE user_id = ?
        """,
        (user_id,),
    ).fetchone()

    labels = _recent_months()
    by_month = {}
    for ym, d, s in conn.execute(
        f"""
        WITH p AS ({_UNION_SQL})
        SELECT substr(session, 1, 7) AS ym,
               count(DISTINCT substr(session, 1, 10)) AS d,
               count(DISTINCT session) AS s
        FROM p WHERE user_id = ? AND substr(session, 1, 7) >= ?
        GROUP BY ym
        """,
        (user_id, labels[0]),
    ):
        by_month[ym] = (d, s)

    return {
        "days": days,
        "sessions": sessions,
        "first_ts": first_ts,
        "last_ts": last_ts,
        "months": [(ym, *by_month.get(ym, (0, 0))) for ym in labels],
    }


def analyze_snapshots(snapshots):
    """一次遍历算出最新值与变化时间线。

    返回 (latest, changes)，latest 为 field -> 最新非空值，changes 为
    [(ts_epoch, field, old, new), ...]（时间升序）。首个非空值只当作
    基线，不算变化。
    """
    latest = {}
    known = {}
    changes = []
    for row in snapshots:
        ts_epoch = row[-1]
        for idx, (field, _label) in enumerate(_TRACKED_FIELDS):
            value = row[idx]
            if not _known(value):
                continue
            if field in known and value != known[field]:
                changes.append((ts_epoch, field, known[field], value))
            known[field] = value
            latest[field] = value
    return latest, changes


def _print_candidates(candidates):
    """打印候选 markdown 表（最多 CAND_LIMIT 行）。"""
    shown = candidates[:CAND_LIMIT]
    print()
    print(f"命中 {len(candidates)} 个用户，最近出现的前 {len(shown)} 个：")
    print()
    print("| 序号 | 用户ID | 昵称 | 抖音号 | 天/场 | 最近出现 |")
    print("|---|---|---|---|---|---|")
    for idx, row in enumerate(shown, start=1):
        user_id, user_name, display_id, days, sessions, last = row
        print(
            f"| {idx} | {user_id} | {user_name or ''} | "
            f"{display_id or ''} | {days}/{sessions} | {_local(last)} |"
        )
    if len(candidates) > len(shown):
        print()
        print(
            f"（候选较多，只列最近出现的 {len(shown)} 个，"
            f"共 {len(candidates)} 个命中）"
        )
    return shown


def choose_candidate(candidates, auto):
    """单选直出；多选列表并询问（-y 或非 TTY 自动取 #1）。"""
    if len(candidates) == 1:
        return candidates[0]

    shown = _print_candidates(candidates)
    if auto or not sys.stdin.isatty():
        print("自动选择最近出现的 #1")
        return shown[0]

    while True:
        try:
            answer = input(f"选择 [1-{len(shown)}]: ")
        except EOFError:
            print()
            print("自动选择最近出现的 #1")
            return shown[0]
        answer = answer.strip()
        if answer.isdigit() and 1 <= int(answer) <= len(shown):
            return shown[int(answer) - 1]
        print(f"  输入无效，请输入 1-{len(shown)} 之间的数字")


def query_ip(conn, sec_uid):
    return conn.execute(
        """
        SELECT ip_location, age, ip_status, ip_fetched_at
        FROM ip_cache WHERE sec_uid = ?
        """,
        (sec_uid,),
    ).fetchone()


def print_ip_row(row):
    """展示 ip_cache 的一行（措辞与旧脚本一致）。"""
    ip_location, age, status, fetched_at = row
    if status == "ok":
        suffix = f"（{age} 岁）" if age not in (None, "") else ""
        print(f"  属地    {ip_location or ''}{suffix}")
        print(f"  抓取于  {fetched_at}（status=ok）")
    elif status == "no_ip_location":
        print("  属地    抖音主页未展示 IP 属地")
        print(f"  抓取于  {fetched_at}")
    else:
        print(f"  属地    抓取失败：{status}")
        print(
            f"  抓取于  {fetched_at}（status={status}）"
        )
        print("          重试：python tools/fetch_ip_location.py --help")


def _can_import_scrapling(python_path):
    """子进程里试导入抓取脚本真正需要的 Fetcher；超时/起不来都算不行。

    只测 `import scrapling` 不够：scrapling 可能已装但缺依赖（实测 0.5
    缺 curl_cffi 时 `import scrapling` 成功、`scrapling.fetchers` 失败）。
    """
    try:
        proc = subprocess.run(
            [
                str(python_path),
                "-c",
                "from scrapling.fetchers import Fetcher",
            ],
            capture_output=True,
            timeout=15,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return proc.returncode == 0


_NO_SCRAPLING_MSG = (
    "未找到可用的 scrapling 解释器（需要能 "
    "`from scrapling.fetchers import Fetcher`）。已检查当前解释器、"
    "$SCRAPLING_PY、常见 conda env。若已装 scrapling 但缺依赖，补装 "
    '`pip install "scrapling[fetchers]"`；或设置 '
    "`SCRAPLING_PY=/path/to/scrapling/env/bin/python`；也可用 `--no-fetch` "
    "跳过。"
)


def _probe_candidate_pythons():
    """在常见 conda env 里逐个试 import scrapling，返回首个可用路径。"""
    patterns = (
        "~/.conda/envs/*/bin/python",
        "~/miniconda3/envs/*/bin/python",
        "~/anaconda3/envs/*/bin/python",
        "/opt/miniconda3/envs/*/bin/python",
        str(Path(sys.prefix).parent / "*" / "bin" / "python"),
    )
    current = Path(sys.executable).resolve()
    seen = set()
    for pattern in patterns:
        for raw in sorted(glob.glob(os.path.expanduser(pattern))):
            path = Path(raw)
            try:
                real = path.resolve()
            except OSError:
                continue
            if real in seen or real == current:
                continue
            seen.add(real)
            if not path.is_file() or not os.access(path, os.X_OK):
                continue
            if _can_import_scrapling(path):
                return str(path)
    return None


def find_fetch_python():
    """找一个能 import scrapling 的解释器，返回 (路径, 错误说明)。

    顺序：$SCRAPLING_PY（显式指定，失败就报错，不静默回退）-> 当前解释器
    -> 常见 conda env。都找不到时返回 (None, 可读提示)，不抛异常。
    """
    env_py = os.environ.get("SCRAPLING_PY")
    if env_py:
        path = Path(env_py).expanduser()
        if not path.is_file() or not os.access(path, os.X_OK):
            return None, (
                f"SCRAPLING_PY 指向的路径不存在或不可执行：{env_py}"
            )
        if not _can_import_scrapling(path):
            return None, (
                f"SCRAPLING_PY 指定的解释器无法 import scrapling：{env_py}"
            )
        return str(path), None

    if _can_import_scrapling(sys.executable):
        return sys.executable, None

    found = _probe_candidate_pythons()
    if found is not None:
        return found, None
    return None, _NO_SCRAPLING_MSG


def _import_ip_csv(csv_file, db_path):
    """把抓取脚本产出的 CSV 直接 upsert 进目标库的 ip_cache。

    惰性 import src/ip_cache.py（它顶层 `from store import DEFAULT_DB_PATH`，
    所以先把 src/ 放进 sys.path）。写入失败只返回错误说明，不往外抛。
    """
    src_dir = str(PROJ / "src")
    if src_dir not in sys.path:
        sys.path.insert(0, src_dir)
    default_db = session_db.DEFAULT_DB_PATH
    path = Path(db_path).expanduser() if db_path else default_db
    conn = None
    try:
        import ip_cache  # noqa: E402  惰性导入，避免 tools 启动时依赖 src/

        conn = sqlite3.connect(str(path))
        ip_cache.cmd_import_csv(conn, SimpleNamespace(csv=str(csv_file)))
        conn.commit()
    except sqlite3.Error as exc:
        return f"CSV 已抓到，但写入 ip_cache 失败：{exc}"
    except OSError as exc:
        return f"CSV 已抓到，但读取/写入失败：{exc}"
    except ImportError as exc:
        return f"无法导入 src/ip_cache.py：{exc}"
    finally:
        if conn is not None:
            conn.close()
    return None


def fetch_and_store_ip(sec_uid, db_path):
    """抓取单个 sec_uid 并直接写 ip_cache。

    返回 (rc, error, tail, interpreter)：rc 是抓取脚本退出码（解释器缺失
    或启动失败时为 None），error 是给人看的说明，tail 是输出末尾 15 行。
    CSV 只要非空就导入，即使 rc != 0（部分成功也先落库）。
    """
    python_path, error = find_fetch_python()
    if python_path is None:
        return None, error, [], None

    fetcher = TOOLS_DIR / "fetch_ip_location.py"
    with tempfile.TemporaryDirectory(prefix="tool_user_ip.") as workdir:
        list_file = Path(workdir) / "list.txt"
        csv_file = Path(workdir) / "ip_locations.csv"
        list_file.write_text(f"{sec_uid}\n", encoding="utf-8")
        try:
            proc = subprocess.run(
                [
                    python_path,
                    str(fetcher),
                    "--list",
                    str(list_file),
                    "--csv",
                    str(csv_file),
                    "--interval",
                    "1",
                    "--timeout",
                    "30",
                ],
                capture_output=True,
                text=True,
                timeout=300,
            )
        except FileNotFoundError as exc:
            return None, f"无法启动抓取进程：{exc}", [], python_path
        except subprocess.TimeoutExpired:
            return None, "抓取超时（超过 300 秒）", [], python_path
        except OSError as exc:
            return None, f"无法启动抓取进程：{exc}", [], python_path

        output = (proc.stdout or "") + "\n" + (proc.stderr or "")
        tail = [
            line.rstrip() for line in output.splitlines() if line.strip()
        ][-15:]

        import_error = None
        if csv_file.is_file() and csv_file.stat().st_size > 0:
            import_error = _import_ip_csv(csv_file, db_path)
        return proc.returncode, import_error, tail, python_path


def print_ip(conn, latest, user_id, db_path, no_fetch):
    """打印 IP 属地块；无缓存且允许时当场抓取（期间关闭只读连接）。

    返回（可能重开过的）连接：抓取前必须 close，否则写库会被只读连接
    持有的读事务卡住。
    """
    sec_uid = latest.get("sec_uid")
    if not sec_uid:
        print("  属地    该用户无 secUid，无法抓取")
        return conn

    row = query_ip(conn, sec_uid)
    if row is not None:
        print_ip_row(row)
        return conn

    if no_fetch:
        print(
            "  属地    尚未抓取（去掉 --no-fetch 会自动抓；"
            "可用 SCRAPLING_PY 指定 scrapling 解释器）"
        )
        return conn

    print("  属地    尚未抓取，正在抓取...")
    conn.close()
    rc, error, tail, python_path = fetch_and_store_ip(sec_uid, db_path)
    conn = session_db.open_db(db_path)

    row = query_ip(conn, sec_uid)
    if row is not None:
        print_ip_row(row)
        return conn

    print("  属地    抓取未完成")
    if python_path:
        print(f"    解释器  {python_path}")
    if error:
        print(f"    {error}")
    if rc == 10:
        print(
            "    签名 cookie 已失效（challenge 页）：更新 "
            "tools/fetch_ip_location.py 顶部的 COOKIES 后重试。"
        )
    elif rc == 11:
        print("    疑似被限流：稍后重试（已完成的行已落库）。")
    for line in tail:
        print(f"    {line}")
    print(f"（可重试：python tools/tool user {user_id}）")
    return conn


def print_chats(conn, user_id, count):
    """最近 count 条发言（本地 ts 文本列，制表/换行替换成空格）。"""
    print()
    print(f"【最近 {count} 条发言】")
    rows = conn.execute(
        """
        SELECT ts, content FROM ev_chat
        WHERE user_id = ? ORDER BY ts_epoch DESC LIMIT ?
        """,
        (user_id, count),
    ).fetchall()
    if not rows:
        print("无发言")
        return
    for idx, (ts, content) in enumerate(rows, start=1):
        text = (content or "").replace("\t", " ").replace("\n", " ")
        text = text.replace("\r", " ")
        print(f"{idx:2d}. {ts}  {text}")


def build_parser(prog=None):
    parser = argparse.ArgumentParser(
        prog=prog,
        description="单用户速查：按 昵称/抖音号/用户ID 匹配候选，展示最新"
                    "资料、变化时间线、最近发言与 IP 属地。",
    )
    parser.add_argument(
        "pattern",
        metavar="PATTERN",
        help="匹配 昵称/抖音号/用户ID，支持 * ? 通配符；不含通配符时按"
             "子串匹配（自动补成 *模式*）。含通配符或空格时请用引号包住。",
    )
    parser.add_argument(
        "-n", "--count",
        dest="count",
        type=int,
        default=10,
        metavar="N",
        help="显示的发言条数（默认 10，小于 1 按 1 处理）。",
    )
    parser.add_argument(
        "-y", "--yes",
        action="store_true",
        help="命中多个时自动选最近出现的那个，不询问。",
    )
    parser.add_argument(
        "--db",
        dest="db",
        default=None,
        metavar="PATH",
        help=f"events.db 路径（缺省 {session_db.DEFAULT_DB_PATH}）。",
    )
    parser.add_argument(
        "--no-fetch",
        dest="no_fetch",
        action="store_true",
        help="IP 属地尚未抓取时不自动抓取（默认会用 scrapling 解释器"
             "跑 fetch_ip_location.py 抓一次），只提示。",
    )
    return parser


def main(argv=None, prog=None):
    args = build_parser(prog).parse_args(argv)
    count = max(args.count, 1)

    pattern = args.pattern
    if "*" not in pattern and "?" not in pattern:
        pattern = f"*{pattern}*"
    like = glob_to_like(pattern)

    conn = session_db.open_db(args.db)
    print(f"[搜索] 模式「{pattern}」...")
    candidates = search_candidates(conn, like)
    if not candidates:
        print()
        print(f"未找到匹配「{pattern}」的用户")
        return 1

    user_id = choose_candidate(candidates, args.yes)[0]

    snapshots = fetch_snapshots(conn, user_id)
    latest, changes = analyze_snapshots(snapshots)
    activity = activity_stats(conn, user_id)

    print()
    print("选定用户")
    print(f"  用户ID  {user_id}")
    print(f"  昵称    {latest.get('user_name') or '<空>'}")
    print(f"  抖音号  {latest.get('display_id') or '<空>'}")
    print(
        f"  出没    {activity['days']} 天 / {activity['sessions']} 场 | "
        f"{_local(activity['first_ts'])} ~ {_local(activity['last_ts'])}"
    )

    print()
    print("【近 6 个月】")
    for ym, days, sessions in activity["months"]:
        print(f"  {ym}   {days} 天 / {sessions} 场")

    print()
    print("【用户资料】")
    gender = _gender_label(latest.get("gender"))
    pay = latest.get("pay_level")
    fans = latest.get("fans_level")
    print(f"  昵称    {latest.get('user_name') or '<空>'}    性别  {gender}")
    print(
        f"  付费等级 {pay if pay is not None else '-'}    "
        f"粉丝等级 {fans if fans is not None else '-'}"
    )

    print()
    print("【IP 属地】")
    conn = print_ip(conn, latest, user_id, args.db, args.no_fetch)

    print()
    print("【变化时间线】")
    if not changes:
        print("  无可见变化")
    else:
        for ts_epoch, field, old, new in changes:
            label = dict(_TRACKED_FIELDS)[field]
            print(
                f"  {_local(ts_epoch)}  {label}  "
                f"{_field_value(field, old)} -> {_field_value(field, new)}"
            )

    print_chats(conn, user_id, count)
    conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
