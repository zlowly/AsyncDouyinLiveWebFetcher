"""离线导入：把 logs/app-*.log(.gz) 的 DEBUG payload 灌进 SQLite。

三种日志格式都由 store.extract_payload() 统一还原，脚本本身不关心：

    message JSON   payload 在 record["message"] 里（旧格式 / gzip 归档）
    flattened      payload 摊平在顶层，message 已被 JsonFormatter 删除
    plain text     非 payload 行（asyncio 等），自动跳过

房间号的两个来源（payload 里的 common.roomId 每次会话都变，不可用，
见 plan F6）：
    1. 同后缀的 logs/<room_id>/stats-<suffix>.log —— 已验证 62/62 全部匹配
    2. 消息文本里的 [room_id] 前缀，作兜底

用法：
    python src/import_logs.py --dry-run
    python src/import_logs.py
    python src/import_logs.py --room 249069870647   # 强制指定房间
"""

import argparse
import gzip
import json
import re
import sys
import time
from pathlib import Path

import store

DEFAULT_LOGS_DIR = Path(__file__).resolve().parents[1] / "logs"
DEFAULT_DB = Path(store.DEFAULT_DB_PATH)

APP_NAME_RE = re.compile(r"^app-(.+)\.log(?:\.gz)?$")


def open_log(path):
    if path.suffix == ".gz":
        return gzip.open(path, "rt", encoding="utf-8", errors="replace")
    return open(path, "r", encoding="utf-8", errors="replace")


def iter_records(path):
    """逐行产出可解析的 dict，坏行直接跳过。"""
    with open_log(path) as handle:
        for raw in handle:
            raw = raw.strip()
            if not raw:
                continue
            try:
                record = json.loads(raw)
            except (json.JSONDecodeError, UnicodeDecodeError):
                continue
            if isinstance(record, dict):
                yield record


def iter_records_sample(logs_dir, suffix, limit):
    for name in (f"app-{suffix}.log", f"app-{suffix}.log.gz"):
        path = logs_dir / name
        if not path.is_file():
            continue
        for i, record in enumerate(iter_records(path)):
            if i >= limit:
                break
            yield record


def find_room_id(logs_dir, suffix, *, override="", records_sample=200):
    """从日志里推断房间号，按可靠性依次尝试。

    payload 里的 common.roomId 每次会话都变，不可用（plan F6）。
    watch 模式下 app 日志用进程启动时间做 suffix、stats 用开播时间，
    两者不同，所以目录匹配会失效，必须退回到正文/logger 名解析。
    """
    if override:
        return override

    # 1) 同后缀的 stats 目录（single 模式下 62/62 匹配）
    for child in sorted(logs_dir.iterdir()):
        if child.is_dir() and (child / f"stats-{suffix}.log").is_file():
            return child.name

    # 2) 正文里的房间号或 logger 名
    for record in iter_records_sample(logs_dir, suffix, records_sample):
        room = store.room_id_from_record(record, "")
        if room:
            return room
    return ""


def discover(logs_dir):
    """返回 [(path, suffix, mtime)]，按修改时间升序。"""
    found = []
    for path in sorted(logs_dir.glob("app-*.log*")):
        match = APP_NAME_RE.match(path.name)
        if match is None:
            continue
        found.append((path, match.group(1), path.stat().st_mtime))
    return sorted(found, key=lambda item: item[2])


def import_file(
    entry,
    store_obj,
    *,
    logs_dir,
    room_override,
    force,
    dry_run,
    verbose,
    min_age_min,
):
    path, suffix, mtime = entry

    if not force and store_obj.is_imported(suffix):
        return "skip", 0, 0, ""

    # 正在被写入的日志不能标记为已导入 —— 否则后续行永远进不了库。
    age_min = (time.time() - mtime) / 60
    if age_min < min_age_min:
        return (
            "active",
            0,
            0,
            f"mtime 仅 {age_min:.1f} 分钟前，疑似正在写入",
        )

    records = list(iter_records(path))
    if not records:
        if not dry_run:
            store_obj.mark_imported(suffix, "", str(path), 0)
        return "empty", 0, 0, ""

    room_id = find_room_id(logs_dir, suffix, override=room_override)
    rows = store_obj.build_rows(
        records,
        room_id=room_id,
        session=suffix,
        room_resolver=store.room_id_from_record,
    )

    if verbose:
        print(
            f"  {path.name}: {len(records)} 行 -> {len(rows)} payload 行"
            f" (room={room_id or '?'})"
        )

    if dry_run:
        return "dry-run", len(records), len(rows), room_id

    inserted = store_obj.insert_rows(rows)
    store_obj.mark_imported(suffix, room_id, str(path), inserted)
    return "import", len(records), inserted, room_id


def parse_args(argv):
    parser = argparse.ArgumentParser(
        description="把 logs/app-*.log(.gz) 导入 SQLite 事件库"
    )
    parser.add_argument(
        "--logs-dir", type=Path, default=DEFAULT_LOGS_DIR, help="日志目录"
    )
    parser.add_argument(
        "--db", type=Path, default=DEFAULT_DB, help="目标数据库"
    )
    parser.add_argument(
        "--room", default="", help="强制指定 room_id（覆盖自动推断）"
    )
    parser.add_argument("--force", action="store_true", help="忽略已导入记录")
    parser.add_argument("--dry-run", action="store_true", help="只统计不写库")
    parser.add_argument(
        "--min-age-min",
        type=float,
        default=5.0,
        help="mtime 在该分钟数内变化的文件视为正在写入，跳过（默认 5）",
    )
    parser.add_argument("-v", "--verbose", action="store_true")
    return parser.parse_args(argv)


def main(argv):
    args = parse_args(argv)
    logs_dir = args.logs_dir
    if not logs_dir.is_dir():
        print(f"日志目录不存在：{logs_dir}", file=sys.stderr)
        return 2

    entries = discover(logs_dir)
    if not entries:
        print(f"未找到 app-*.log*：{logs_dir}", file=sys.stderr)
        return 2

    db = args.db
    store_obj = store.SqliteEventStore(str(db))

    print(f"数据库：{db}")
    print(f"日志目录：{logs_dir}")
    print(f"发现 {len(entries)} 个 app 日志文件\n")

    started = time.time()
    totals = {}
    reasons = []
    seen = read = written = 0

    for entry in entries:
        action, rec, ins, note = import_file(
            entry,
            store_obj,
            logs_dir=logs_dir,
            room_override=args.room,
            force=args.force,
            dry_run=args.dry_run,
            verbose=args.verbose,
            min_age_min=args.min_age_min,
        )
        totals[action] = totals.get(action, 0) + 1
        if action == "active":
            reasons.append(f"  {note}  {entry[0].name}")
        seen += 1
        read += rec
        written += ins
        if seen % 10 == 0:
            elapsed = time.time() - started
            print(
                f"  进度 {seen}/{len(entries)}  已读 {read:,} 行"
                f"  写入 {written:,} 行  {elapsed:.1f}s",
                flush=True,
            )

    # 先统计再关库。
    final_count = store_obj.count()
    store_obj.close()

    if reasons:
        print("\n正在写入、已跳过（稍后重跑即可补上）：")
        for line in reasons:
            print(line)

    label = "试运行" if args.dry_run else "完成"
    print(f"\n{label}：耗时 {time.time() - started:.1f}s")
    for action, count in sorted(totals.items()):
        print(f"  {action:10} {count:3d} 个文件")
    print(f"  读取       {read:,} 行")
    print(f"  写入       {written:,} 行")
    print(f"  库内总行数 {final_count:,}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
