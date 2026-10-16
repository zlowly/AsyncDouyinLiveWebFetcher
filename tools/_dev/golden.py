#!/usr/bin/env python3
"""golden 基线快照器 —— tools/ 统一 CLI 重构的验收工具。

用法:
    python /tmp/opencode/golden.py capture <outdir>   # 跑全部 fixture 存快照
    python /tmp/opencode/golden.py diff <outdir>      # 再跑一次并与快照比对

比对口径:每个 fixture 的 stdout + stderr + exit code 必须逐字节相同。
输出里含时间戳/耗时的字段已在前置处理中去抖。

不属于仓库,是一次性验收脚手架。
"""
import os
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent          # tools/_dev
TOOLS = HERE.parent                            # tools
REPO = TOOLS.parent                             # 仓库根
BASE = HERE / "golden_base"

# (fixture 名, argv 列表)。--chart-format sixel 这类依赖终端的显式给 ascii。
FIXTURES = [
    # ---- chart (extract_and_visualize_log_data_sqlite.py) ----
    ("chart_default",        ["extract_and_visualize_log_data_sqlite.py"]),
    ("chart_list_sessions",  ["extract_and_visualize_log_data_sqlite.py", "--list-sessions"]),
    ("chart_ascii",          ["extract_and_visualize_log_data_sqlite.py", "--chart-format", "ascii"]),
    ("chart_audience",       ["extract_and_visualize_log_data_sqlite.py", "-m", "audienceCount", "--chart-format", "ascii"]),
    ("chart_session_exact",  ["extract_and_visualize_log_data_sqlite.py", "2026-10-14_21-57-54", "--chart-format", "ascii"]),
    ("chart_compare",        ["extract_and_visualize_log_data_sqlite.py", "2026-10-14_21-57-54",
                              "-c", "2026-10-14_08-04-40", "--chart-format", "ascii"]),
    ("chart_rooms",          ["extract_and_visualize_log_data_sqlite.py", "-r", "249069870647", "--chart-format", "ascii"]),
    ("chart_bar_ascii",      ["extract_and_visualize_log_data_sqlite.py", "--bar-style", "ascii", "--chart-format", "ascii"]),
    ("chart_bad_session",    ["extract_and_visualize_log_data_sqlite.py", "1999-01-01:1", "--chart-format", "ascii"]),
    ("chart_bad_chart_h",    ["extract_and_visualize_log_data_sqlite.py", "--chart-format", "ascii", "--chart-height", "100"]),
    ("chart_decay_bad",      ["extract_and_visualize_log_data_sqlite.py", "-a", "5.0", "--chart-format", "ascii"]),

    # ---- messages (process_log_sqlite.py) ----
    ("msg_default",          ["process_log_sqlite.py"]),
    ("msg_list_sessions",    ["process_log_sqlite.py", "--list-sessions"]),
    ("msg_session",          ["process_log_sqlite.py", "-S", "2026-10-11:1", "-o", "tsv"]),
    ("msg_session_last",     ["process_log_sqlite.py", "-S", "2026-10-14:-1", "-o", "tsv"]),
    ("msg_json",             ["process_log_sqlite.py", "-S", "2026-10-11:1", "-o", "json"]),
    ("msg_user",             ["process_log_sqlite.py", "-u", "咲夜*", "-o", "tsv"]),
    ("msg_nocolor",          ["process_log_sqlite.py", "-S", "2026-10-11:1", "--no-color", "-o", "tsv"]),
    ("msg_rooms_one",        ["process_log_sqlite.py", "-r", "249069870647", "-S", "2026-10-11:1", "-o", "tsv"]),
    ("msg_time_range",       ["process_log_sqlite.py", "--start", "2026-10-14", "--end", "2026-10-15", "-o", "tsv"]),
    ("msg_bad_session",      ["process_log_sqlite.py", "-S", "1999-01-01:1", "-o", "tsv"]),
    ("msg_empty_range",      ["process_log_sqlite.py", "--start", "1999-01-01", "--end", "1999-01-02", "-o", "tsv"]),

    # ---- search (search_chat.py) ----
    ("search_help",          ["search_chat.py", "--help"]),
    ("search_k3",            ["search_chat.py", "-k", "哈哈哈"]),
    ("search_k2",            ["search_chat.py", "-k", "主播"]),
    ("search_kpct",          ["search_chat.py", "-k", "%"]),
    ("search_like",          ["search_chat.py", "--like", "-k", "笑"]),
    ("search_order_asc",     ["search_chat.py", "-k", "礼物", "--order", "asc", "--limit", "5"]),
    ("search_json",          ["search_chat.py", "-k", "6666", "-o", "json"]),
    ("search_nohit",         ["search_chat.py", "-k", "zzzzzz不存在的词"]),
    ("search_session",       ["search_chat.py", "-k", "礼物", "-S", "2026-10-11:1"]),
    ("search_list_sessions", ["search_chat.py", "--list-sessions"]),
    ("search_nokw",          ["search_chat.py"]),
    ("search_db_bad",        ["search_chat.py", "-k", "x", "--db", "/tmp/opencode/nonexistent.db"]),

    # ---- dbcli / session_db 公共模块（库级，不单独运行，冒烟 import）----
    ("lib_search_chat_ok",   ["search_chat.py", "-k", "礼物", "--limit", "3"]),
]


def normalize(text: str) -> str:
    """去抖：Sixel/光标探测与绝对耗时在不同机器/时刻会变。"""
    out = []
    for line in text.splitlines():
        if "\x1b" in line and line.strip().startswith("\x1b"):
            continue  # 裸 ANSI 序列行（终端能力探测输出）
        out.append(line.rstrip())
    return "\n".join(out)


def run_fixture(name, argv):
    env = dict(os.environ)
    env["TERM"] = "dumb"          # 禁掉着色分支差异
    env["NO_COLOR"] = "1"
    env["COLUMNS"] = "100"
    env["PYTHONHASHSEED"] = "0"    # 消除 set 迭代顺序抖动
    try:
        proc = subprocess.run(
            [sys.executable, str(TOOLS / argv[0]), *argv[1:]],
            cwd=REPO, env=env, capture_output=True, text=True,
            timeout=300,
        )
        out, err, rc = proc.stdout, proc.stderr, proc.returncode
    except subprocess.TimeoutExpired:
        out, err, rc = "", "<<TIMEOUT>>", -99
    body = (
        f"$ {argv[0]} {' '.join(argv[1:])}\n"
        f"--- exit: {rc}\n"
        f"--- stdout:\n{normalize(out)}\n"
        f"--- stderr:\n{normalize(err)}\n"
    )
    return body


def run_import_smoke():
    """三个共享库必须能从 tools/_lib 直接导入。

    单独存在的理由：chart 对 sixel_plot 用的是 try/except ImportError →
    None，导入失败会被静默吞掉，而 ascii 分支的 fixture 照样全绿。
    这个冒烟把「静默降级」变成「显式失败」。
    """
    code = (
        "import sys; sys.path.insert(0, %r)\n"
        "import dbcli, session_db, sixel_plot\n"
        "assert callable(sixel_plot.render_chart), 'render_chart 缺失'\n"
        "assert callable(sixel_plot.verify_chart), 'verify_chart 缺失'\n"
        "assert sixel_plot.BAND_HEIGHT > 0, 'BAND_HEIGHT 异常'\n"
        "print('libs ok', session_db.DEFAULT_DB_PATH)\n"
    ) % str(TOOLS / "_lib")
    proc = subprocess.run(
        [sys.executable, "-c", code], cwd=REPO, capture_output=True,
        text=True, timeout=60,
    )
    return (
        "$ import smoke: tools/_lib/{dbcli,session_db,sixel_plot}\n"
        f"--- exit: {proc.returncode}\n"
        f"--- stdout:\n{normalize(proc.stdout)}\n"
        f"--- stderr:\n{normalize(proc.stderr)}\n"
    )


def capture(outdir):
    outdir = Path(outdir)
    outdir.mkdir(parents=True, exist_ok=True)
    smoke = run_import_smoke()
    (outdir / "_import_smoke.txt").write_text(smoke, encoding="utf-8")
    print("{:24s} {}".format(
        "_import_smoke",
        next(l for l in smoke.splitlines() if l.startswith("--- exit:")),
    ))
    for name, argv in FIXTURES:
        body = run_fixture(name, argv)
        (outdir / f"{name}.txt").write_text(body, encoding="utf-8")
        rc_line = next(l for l in body.splitlines() if l.startswith("--- exit:"))
        print(f"{name:24s} {rc_line}  ({len(body)} bytes)")


def diff(outdir):
    outdir = Path(outdir)
    bad = []
    smoke = run_import_smoke()
    snap = outdir / "_import_smoke.txt"
    if not snap.exists():
        bad.append(("_import_smoke", "缺少基线快照"))
    elif smoke != snap.read_text(encoding="utf-8"):
        bad.append(("_import_smoke", "共享库导入冒烟不一致"))
    for name, argv in FIXTURES:
        snap = outdir / f"{name}.txt"
        if not snap.exists():
            bad.append((name, "缺少基线快照"))
            continue
        now = run_fixture(name, argv)
        if now != snap.read_text(encoding="utf-8"):
            bad.append((name, "输出不一致"))
    if not bad:
        print(f"ALL {len(FIXTURES)} fixtures + 导入冒烟 与基线逐字节一致 ✅")
        return 0
    print(f"❌ {len(bad)}/{len(FIXTURES) + 1} 项不一致：")
    for name, why in bad:
        print(f"  - {name}: {why}")
    return 1


if __name__ == "__main__":
    cmd = sys.argv[1]
    target = sys.argv[2] if len(sys.argv) > 2 else BASE
    sys.exit(capture(target) if cmd == "capture" else diff(target))
