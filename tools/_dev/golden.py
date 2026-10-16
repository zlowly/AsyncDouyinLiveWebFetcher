#!/usr/bin/env python3
"""golden 基线快照器 —— tools/ 统一 CLI 重构的**常驻**验收安全网。

用法:
    python tools/_dev/golden.py capture   # 跑全部 fixture 存基线（默认写 tools/_dev/golden_base）
    python tools/_dev/golden.py diff      # 重跑并与基线比对（验收用）

比对口径:每个 fixture 的 stdout + stderr + exit code 必须逐字节相同。
输出里含时间戳/耗时的字段已在前置处理中去抖。

**本文件已入库,不是一次性脚本。** 仓库没有测试框架、没有 CI,
它就是 tools/ 重构的唯一安全网,不要删。
只有**有意**改变某个命令的输出时才 capture;否则 diff 红了就是行为回归,
应修代码而不是刷新基线。

注意 P3 起 argparse 的 prog 会显式化,届时 search 的 usage/help 文案会
有意变化 —— 那时才 capture,并在 commit message 里写明。
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
# argv[0] 是相对 tools/ 的脚本路径（P4 起命令模块在 commands/ 下，故带前缀）。
FIXTURES = [
    # ---- chart (commands/chart.py) ----
    ("chart_default",        ["commands/chart.py"]),
    ("chart_list_sessions",  ["commands/chart.py", "--list-sessions"]),
    ("chart_ascii",          ["commands/chart.py", "--chart-format", "ascii"]),
    ("chart_audience",       ["commands/chart.py", "-m", "audienceCount", "--chart-format", "ascii"]),
    ("chart_session_exact",  ["commands/chart.py", "2026-10-14_21-57-54", "--chart-format", "ascii"]),
    ("chart_compare",        ["commands/chart.py", "2026-10-14_21-57-54",
                              "-c", "2026-10-14_08-04-40", "--chart-format", "ascii"]),
    ("chart_rooms",          ["commands/chart.py", "-r", "249069870647", "--chart-format", "ascii"]),
    ("chart_bar_ascii",      ["commands/chart.py", "--bar-style", "ascii", "--chart-format", "ascii"]),
    ("chart_bad_session",    ["commands/chart.py", "1999-01-01:1", "--chart-format", "ascii"]),
    ("chart_bad_chart_h",    ["commands/chart.py", "--chart-format", "ascii", "--chart-height", "100"]),
    ("chart_decay_bad",      ["commands/chart.py", "-a", "5.0", "--chart-format", "ascii"]),
    # P2 曾把 --debug 与 --color always 弄失效（模块级开关在 main() 里被局部遮蔽，
    # helper 仍读初值），而这两条 fixture 缺失导致 golden 全绿放行。补上以锁死。
    ("chart_debug",          ["commands/chart.py", "-d", "--chart-format", "ascii"]),
    ("chart_debug_session",  ["commands/chart.py", "-d", "--chart-format", "ascii",
                              "2026-10-14_21-57-54"]),
    ("chart_color_always",   ["commands/chart.py", "2026-10-14_21-57-54",
                              "-c", "2026-10-14_08-04-40", "--color", "always", "--chart-format", "ascii"]),
    # 唯一可达的 sixel 分支：--chart-height 非 BAND_HEIGHT 倍数的校验
    # （只在该分支内生效，ascii 模式不触发）。sixel_plot 本身可用时走真渲染。
    ("chart_sixel_bad_h",    ["commands/chart.py",
                              "--chart-format", "sixel", "--chart-height", "100"]),

    # ---- messages (commands/messages.py) ----
    ("msg_default",          ["commands/messages.py"]),
    ("msg_list_sessions",    ["commands/messages.py", "--list-sessions"]),
    ("msg_session",          ["commands/messages.py", "-S", "2026-10-11:1", "-o", "tsv"]),
    ("msg_session_last",     ["commands/messages.py", "-S", "2026-10-14:-1", "-o", "tsv"]),
    ("msg_json",             ["commands/messages.py", "-S", "2026-10-11:1", "-o", "json"]),
    ("msg_user",             ["commands/messages.py", "-u", "咲夜*", "-o", "tsv"]),
    ("msg_nocolor",          ["commands/messages.py", "-S", "2026-10-11:1", "--no-color", "-o", "tsv"]),
    ("msg_rooms_one",        ["commands/messages.py", "-r", "249069870647", "-S", "2026-10-11:1", "-o", "tsv"]),
    ("msg_time_range",       ["commands/messages.py", "--start", "2026-10-14", "--end", "2026-10-15", "-o", "tsv"]),
    ("msg_bad_session",      ["commands/messages.py", "-S", "1999-01-01:1", "-o", "tsv"]),
    ("msg_empty_range",      ["commands/messages.py", "--start", "1999-01-01", "--end", "1999-01-02", "-o", "tsv"]),

    # ---- search (commands/search.py) ----
    ("search_help",          ["commands/search.py", "--help"]),
    ("search_k3",            ["commands/search.py", "-k", "哈哈哈"]),
    ("search_k2",            ["commands/search.py", "-k", "主播"]),
    ("search_kpct",          ["commands/search.py", "-k", "%"]),
    ("search_like",          ["commands/search.py", "--like", "-k", "笑"]),
    ("search_order_asc",     ["commands/search.py", "-k", "礼物", "--order", "asc", "--limit", "5"]),
    ("search_json",          ["commands/search.py", "-k", "6666", "-o", "json"]),
    ("search_nohit",         ["commands/search.py", "-k", "zzzzzz不存在的词"]),
    ("search_session",       ["commands/search.py", "-k", "礼物", "-S", "2026-10-11:1"]),
    ("search_list_sessions", ["commands/search.py", "--list-sessions"]),
    ("search_nokw",          ["commands/search.py"]),
    ("search_db_bad",        ["commands/search.py", "-k", "x", "--db", "/tmp/opencode/nonexistent.db"]),

    # ---- dbcli / session_db 公共模块（库级，不单独运行，冒烟 import）----
    ("lib_search_chat_ok",   ["commands/search.py", "-k", "礼物", "--limit", "3"]),
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
    script = TOOLS / argv[0]
    if not script.is_file():
        # 搬迁/改名后 fixture 若指错路径（如仍写旧脚本名），subprocess 只会报
        # 「找不到文件」并被当成一条普通差异，容易误判为「只是脚本名变了」。
        # 这里显式炸掉：fixture 目标不存在即验收机制本身失效。
        raise FileNotFoundError(
            f"fixture {name!r} 的命令脚本不存在: {script}"
        )
    try:
        proc = subprocess.run(
            [sys.executable, str(script), *argv[1:]],
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
