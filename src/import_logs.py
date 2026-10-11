"""离线导入：把 logs/app-*.log(.gz) 的 DEBUG payload 灌进 SQLite。

三种日志格式都由 store.extract_payload() 统一还原，脚本本身不关心：

    message JSON   payload 在 record["message"] 里（旧格式 / gzip 归档）
    flattened      payload 摊平在顶层，message 已被 JsonFormatter 删除
    plain text     非 payload 行（asyncio 等），自动跳过

房间号是**文件级**属性：实测 563 个 app 日志里没有一个混了多个房间的
弹幕，推出来后套给该文件所有行。三级来源依次尝试：

    1. 同后缀的 logs/<room_id>/stats-<suffix>.log —— 已验证 62/62 全部匹配
    2. payload 里 user.fansClub.data.anchorId（主播 uid）查映射表 ——
       每个文件内 anchorId 恒定；用第 1 级定出房间的文件反查出映射表，
       含弹幕文件的命中率因此从 42/498 提升到 497/498
    3. 消息文本里的 [room_id] 前缀或 room_<id> logger 名，作兜底

payload 里的 common.roomId 每次会话都变，不可用（见 plan F6）。

--room 是白名单而不是覆盖：不在名单里的文件跳过且不写 imported_sessions，
下次去掉 --room 重跑仍能补上。

watch 模式（python src/main.py 不带 -r）已关闭实时入库，房间号只能离线算，
所以要在 fetcher 退出后再跑本脚本；单房间模式（-r <room_id>）的实时入库
room_id 正确，可边播边写。

--report 落一份逐文件报告（--dry-run 也生成），列出每个日志读了多少行、
还原出多少 payload、实际写入多少行、房间号是哪一级推出来的，用来人工抽查。
不带路径时写 logs/import-report.txt。用法：

    python src/import_logs.py --dry-run
    python src/import_logs.py
    python src/import_logs.py --room 249069870647,74083423272
    python src/import_logs.py --dry-run --report /tmp/import-report.txt
"""

import argparse
import gzip
import json
import re
import sys
import time
from collections import Counter, namedtuple
from contextlib import nullcontext
from datetime import datetime
from pathlib import Path

import store

DEFAULT_LOGS_DIR = Path(__file__).resolve().parents[1] / "logs"
DEFAULT_DB = Path(store.DEFAULT_DB_PATH)
DEFAULT_REPORT = DEFAULT_LOGS_DIR / "import-report.txt"

APP_NAME_RE = re.compile(r"^app-(.+)\.log(?:\.gz)?$")
STATS_NAME_RE = re.compile(r"^stats-(.+)\.log(?:\.gz)?$")

LogEntry = namedtuple("LogEntry", "path suffix mtime size")

# read/payload/written 用 None 表示「没算」而不是「算了是 0」，报告里
# 渲染成 -。跳过的文件根本没读，白名单外的文件读了但没必要建 payload
# （建 row 会 zlib 压缩一遍再丢掉），报 0 会误导人工抽查。
FileResult = namedtuple(
    "FileResult", "action read payload written room source detail note"
)

# 报告用 ASCII 表头，保证等宽对齐；中文含义写在下面的图例里。
_ROW_HEAD = (
    f"{'ACTION':<8}{'SIZE_KB':>8}{'READ':>9}{'PAYLOAD':>9}{'WRITTEN':>9}"
    f"  {'ROOM_ID':<15}{'SOURCE':<8}{'DETAIL':<21}FILE"
)
_ROW_FMT = (
    "{action:<8}{size:>8}{read:>9}{payload:>9}{written:>9}"
    "  {room:<15}{source:<8}{detail:<21}{name}"
)

REPORT_LEGEND = """\
列说明
  ACTION    import=已入库  dry-run=试运行未入库  skip=账本已记录
            empty=无可解析记录  active=疑似正在写入  filtered=不在白名单
            unknown=推断不出房间号
  SIZE_KB   日志文件大小
  READ      能解析成 JSON 对象的行数
  PAYLOAD   其中能还原成 protobuf payload 的行数，即本来会入库的行数
  WRITTEN   实际 INSERT 进库的行数；--dry-run 恒为 -
  ROOM_ID   文件级房间号，整份文件所有行共用
  SOURCE    ROOM_ID 的来源：stats=同后缀 stats 目录  anchor=粉丝团主播 uid
            查映射表  text=正文/logger 兜底  -=没定出来
  DETAIL    stats 写日志后缀，anchor 写主播 uid，其余 -
  FILE      app 日志文件名

数字列的 - 表示「没算」而不是「算了是 0」：skip/active 的文件根本没打开，
filtered/unknown 的文件读了但故意不建 payload。

"""


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


def find_app_log(logs_dir, suffix):
    """返回 app-<suffix>.log 或其 gzip 归档，都没有则返回 None。"""
    for name in (f"app-{suffix}.log", f"app-{suffix}.log.gz"):
        path = logs_dir / name
        if path.is_file():
            return path
    return None


def anchor_id_from_payload(payload):
    """粉丝团数据里带着主播 uid，是唯一跨会话稳定的房间标记。"""
    anchor = store._int(
        store._dig(payload, "user", "fansClub", "data", "anchorId")
    )
    return str(anchor) if anchor else ""


def iter_anchor_ids(records):
    """惰性产出记录里的主播 uid；一个文件内恒定，取首个即可。"""
    for record in records:
        anchor = anchor_id_from_payload(store.extract_payload(record))
        if anchor:
            yield anchor


def stats_room_index(logs_dir):
    """一次扫目录，返回 {stats 日志后缀: room_id}。

    比逐文件遍历 logs_dir 便宜得多；实测没有后缀命中多个房间目录，
    所以撞车时保留排序最靠前的那个。
    """
    index = {}
    for child in sorted(logs_dir.iterdir()):
        if not child.is_dir():
            continue
        for path in sorted(child.glob("stats-*.log*")):
            match = STATS_NAME_RE.match(path.name)
            if match:
                index.setdefault(match.group(1), child.name)
    return index


def build_anchor_map(logs_dir, stats_rooms):
    """拿房间号已知的文件反推 {主播 uid: room_id}。

    只扫 stats 目录能匹配的文件，而且读到首个主播 uid 就退出，
    I/O 可忽略；实测 62 个文件种出 8 条映射且零冲突。
    同一主播对应到多个房间说明推断本身不可信，宁可丢弃。
    """
    mapping = {}
    conflicts = set()
    for suffix, room in sorted(stats_rooms.items()):
        path = find_app_log(logs_dir, suffix)
        if path is None:
            continue
        anchor = next(iter_anchor_ids(iter_records(path)), "")
        if not anchor:
            continue
        if mapping.setdefault(anchor, room) != room:
            conflicts.add(anchor)
    for anchor in conflicts:
        del mapping[anchor]
    return mapping


def find_room_id(records, suffix, *, stats_rooms, anchor_rooms, tail=200):
    """推断房间号，按可靠性依次尝试。

    返回 (room_id, source, detail)，source 是给报告看的一级来源标签，
    detail 是可复核的凭据（stats 的后缀 / anchor 的主播 uid）。
    payload 里的 common.roomId 每次会话都变，不可用（plan F6）。
    watch 模式下 app 日志用进程启动时间做 suffix、stats 用开播时间，
    两者不同，所以目录匹配会失效，必须退回主播 uid / 正文解析。
    """
    # 1) 同后缀的 stats 目录（single 模式下 62/62 匹配）
    room = stats_rooms.get(suffix, "")
    if room:
        return room, "stats", suffix

    # 2) 粉丝团里的主播 uid
    anchor = next(iter_anchor_ids(records), "")
    if anchor and anchor in anchor_rooms:
        return anchor_rooms[anchor], "anchor", anchor

    # 3) 正文里的房间号或 logger 名
    for record in records[:tail]:
        room = store.room_id_from_record(record, "")
        if room:
            return room, "text", ""
    return "", "", ""


def discover(logs_dir):
    """返回按修改时间升序的 LogEntry 列表。"""
    found = []
    for path in sorted(logs_dir.glob("app-*.log*")):
        match = APP_NAME_RE.match(path.name)
        if match is None:
            continue
        stat = path.stat()
        found.append(
            LogEntry(path, match.group(1), stat.st_mtime, stat.st_size)
        )
    return sorted(found, key=lambda item: item.mtime)


def import_file(
    entry,
    store_obj,
    *,
    stats_rooms,
    anchor_rooms,
    whitelist,
    force,
    dry_run,
    verbose,
    min_age_min,
):
    path, suffix = entry.path, entry.suffix

    if not force and store_obj.is_imported(suffix):
        return FileResult("skip", None, None, 0, "", "", "", "账本已记录")

    # 正在被写入的日志不能标记为已导入 —— 否则后续行永远进不了库。
    age_min = (time.time() - entry.mtime) / 60
    if age_min < min_age_min:
        return FileResult(
            "active",
            None,
            None,
            0,
            "",
            "",
            "",
            f"mtime 仅 {age_min:.1f} 分钟前，疑似正在写入",
        )

    records = list(iter_records(path))
    if not records:
        if not dry_run:
            store_obj.mark_imported(suffix, "", str(path), 0)
        return FileResult("empty", 0, 0, 0, "", "", "", "无可解析记录")

    room, source, detail = find_room_id(
        records, suffix, stats_rooms=stats_rooms, anchor_rooms=anchor_rooms
    )

    # 房间号是文件级的，一个文件的所有行同房；不在白名单就整个跳过，
    # 且不写 imported_sessions —— 换个白名单重跑时它还在。
    # read 有值但 payload 是 None：已经读过了，但建 row 要 zlib 压缩，
    # 压完直接丢掉不划算。
    if whitelist and room not in whitelist:
        if not room:
            return FileResult(
                "unknown",
                len(records),
                None,
                0,
                "",
                "",
                "",
                "推断不出 room_id",
            )
        return FileResult(
            "filtered",
            len(records),
            None,
            0,
            room,
            source,
            detail,
            "不在 --room 白名单",
        )

    rows = store_obj.build_rows(records, room_id=room, session=suffix)

    if verbose:
        print(
            f"  {path.name}: {len(records)} 行 -> {len(rows)} payload 行"
            f" (room={room or '?'} via {source or '-'})"
        )

    if dry_run:
        return FileResult(
            "dry-run", len(records), len(rows), None, room, source, detail, ""
        )

    inserted = store_obj.insert_rows(rows)
    store_obj.mark_imported(suffix, room, str(path), inserted)
    return FileResult(
        "import", len(records), len(rows), inserted, room, source, detail, ""
    )


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
        "--room",
        default="",
        help="只导入这些 room_id，逗号分隔（留空表示不限）",
    )
    parser.add_argument("--force", action="store_true", help="忽略已导入记录")
    parser.add_argument("--dry-run", action="store_true", help="只统计不写库")
    parser.add_argument(
        "--report",
        type=Path,
        nargs="?",
        const=DEFAULT_REPORT,
        default=None,
        metavar="PATH",
        help=(
            "生成本次运行的逐文件报告，--dry-run 也生成；"
            f"不带路径则写 {DEFAULT_REPORT}"
        ),
    )
    parser.add_argument(
        "--min-age-min",
        type=float,
        default=5.0,
        help="mtime 在该分钟数内变化的文件视为正在写入，跳过（默认 5）",
    )
    parser.add_argument("-v", "--verbose", action="store_true")
    return parser.parse_args(argv)


def parse_rooms(text):
    """解析逗号分隔的 room_id 白名单。"""
    return {part.strip() for part in text.split(",") if part.strip()}


def print_header(args, stats_rooms, anchor_rooms, whitelist, total):
    print(f"数据库：{args.db}")
    print(f"日志目录：{args.logs_dir}")
    print(
        f"房间号来源：stats 目录 {len(stats_rooms)} 个"
        f"  主播映射 {len(anchor_rooms)} 条"
    )
    if whitelist:
        print(f"房间白名单：{'、'.join(sorted(whitelist))}")
    print(f"发现 {total} 个 app 日志文件\n")


def print_skip_report(notes):
    if notes["active"]:
        print("\n正在写入、已跳过（稍后重跑即可补上）：")
        for line in notes["active"]:
            print(line)
    if notes["filtered"]:
        print("\n不在 --room 白名单、已跳过（去掉 --room 即可导入）：")
        for room, count in sorted(
            notes["filtered"].items(), key=lambda kv: -kv[1]
        ):
            print(f"  room={room}  {count} 个文件")
    if notes["unknown"]:
        print("\n推断不出 room_id、已跳过（不带 --room 即可导入）：")
        for line in notes["unknown"]:
            print(line)


def _cell(value):
    """None 渲染成 -，区分「没算」和「算了是 0」。"""
    return "-" if value is None else str(value)


def open_report(path):
    """打开报告文件；path 为 None 时返回 nullcontext，调用方照常 with。"""
    if path is None:
        return nullcontext(None)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        return open(path, "w", encoding="utf-8")
    except OSError as exc:
        raise SystemExit(f"无法写入报告 {path}：{exc}") from exc


def report_header(handle, args, stats_rooms, anchor_rooms, whitelist, total):
    """写出运行参数和列含义。"""
    if handle is None:
        return
    write = handle.write
    write("import_logs 运行报告\n")
    write("=" * 78 + "\n")
    write(f"生成时间    {datetime.now():%Y-%m-%d %H:%M:%S}\n")
    write(f"数据库      {args.db}\n")
    write(f"日志目录    {args.logs_dir}\n")
    mode = "试运行 --dry-run（不写库）" if args.dry_run else "正式导入"
    write(f"运行模式    {mode}\n")
    if whitelist:
        write(f"房间白名单  {'、'.join(sorted(whitelist))}\n")
    else:
        write("房间白名单  （无，全部导入）\n")
    write(
        f"房间号来源  stats 目录 {len(stats_rooms)} 个后缀"
        f"  主播映射 {len(anchor_rooms)} 条\n"
    )
    write(f"文件总数    {total}\n\n")
    write(REPORT_LEGEND)
    write(_ROW_HEAD + "\n")


def report_row(handle, entry, result):
    """每处理完一个文件就落一行，中途 Ctrl-C 也能留下已完成的部分。"""
    if handle is None:
        return
    handle.write(
        _ROW_FMT.format(
            action=result.action,
            size=f"{entry.size / 1024:.0f}",
            read=_cell(result.read),
            payload=_cell(result.payload),
            written=_cell(result.written),
            room=result.room or "-",
            source=result.source or "-",
            detail=result.detail or "-",
            name=entry.path.name,
        )
        + "\n"
    )
    handle.flush()


def _sum_by_room(pairs, dry_run):
    """按房间号聚合。

    只累加「统计过」的文件（payload 有值的），并同时记下参与统计的文件数。
    白名单外的文件读了日志但没建 payload —— 把它们当 0 累加会低估，
    整列标成 - 又会丢掉已经统计出来的部分，所以两者都给。
    """
    agg = {}
    for _, result in pairs:
        cur = agg.setdefault(result.room or "(未定出)", [0, 0, 0, 0, 0])
        cur[0] += 1
        if result.payload is None:
            continue
        cur[1] += 1
        cur[2] += result.read or 0
        cur[3] += result.payload
        if not dry_run:
            cur[4] += result.written or 0
    return agg


def _sum_by_source(pairs):
    """按房间号来源聚合，看得出多少是靠最弱的 text 兜底猜的。"""
    agg = Counter()
    for _, result in pairs:
        agg[result.source or "-"] += 1
    return agg


def _unknown_files(pairs):
    """没定出房间号的文件，带 payload 行数；未统计的排在最后。"""
    items = [
        (
            # 排序键：未统计的当 -1，沉到最后；渲染仍用原始值。
            result.payload if result.payload is not None else -1,
            result.payload,
            entry.path.name,
        )
        for entry, result in pairs
        if not result.room
    ]
    return sorted(items, reverse=True)


def report_summary(
    handle, pairs, totals, read, written, final_count, elapsed, dry_run
):
    """写出汇总：按状态、按房间、按来源，外加没定出房间的文件清单。"""
    if handle is None:
        return
    write = handle.write

    write("\n\n汇总\n")
    write("-" * 78 + "\n")
    label = "试运行" if dry_run else "完成"
    write(f"{label}  耗时 {elapsed:.1f}s  库内总行数 {final_count:,}\n\n")

    write("按状态\n")
    for action, count in sorted(totals.items()):
        write(f"  {action:<10} {count:4d} 个文件\n")
    write(f"  {'读取':<10} {read:>10,} 行\n")
    write(f"  {'写入':<10} {written:>10,} 行\n")

    write("\n按房间号（FILES 是 参与统计/文件总数，只累加统计过的文件）\n")
    write(
        f"  {'ROOM_ID':<16}{'FILES':>9}{'READ':>11}{'PAYLOAD':>11}"
        f"{'WRITTEN':>11}\n"
    )
    rows = _sum_by_room(pairs, dry_run)
    for room, c in sorted(rows.items(), key=lambda kv: -kv[1][0]):
        written = "-" if dry_run else f"{c[4]:,}"
        write(
            f"  {room:<16}{f'{c[1]}/{c[0]}':>9}{c[2]:>11,}{c[3]:>11,}"
            f"{written:>11}\n"
        )

    write("\n房间号来源命中分布\n")
    for source, count in sorted(
        _sum_by_source(pairs).items(), key=lambda kv: -kv[1]
    ):
        write(f"  {source:<10} {count:4d} 个文件\n")

    unknown = _unknown_files(pairs)
    if unknown:
        write(f"\n未定出房间号的文件（{len(unknown)} 个）\n")
        for _, payload, name in unknown:
            write(f"  payload {_cell(payload):>6} 行  {name}\n")
    else:
        write("\n所有文件都定出了房间号\n")


def print_summary(totals, read, written, final_count, elapsed, dry_run):
    label = "试运行" if dry_run else "完成"
    print(f"\n{label}：耗时 {elapsed:.1f}s")
    for action, count in sorted(totals.items()):
        print(f"  {action:10} {count:3d} 个文件")
    print(f"  读取       {read:,} 行")
    print(f"  写入       {written:,} 行")
    print(f"  库内总行数 {final_count:,}")


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

    stats_rooms = stats_room_index(logs_dir)
    anchor_rooms = build_anchor_map(logs_dir, stats_rooms)
    whitelist = parse_rooms(args.room)
    print_header(args, stats_rooms, anchor_rooms, whitelist, len(entries))

    db = args.db
    store_obj = store.SqliteEventStore(str(db))

    started = time.time()
    totals = {}
    # active/unknown 要点名到文件，filtered 只按房间汇总（可能上百个）。
    notes = {"active": [], "filtered": Counter(), "unknown": []}
    pairs = []
    seen = read = written = 0

    with open_report(args.report) as report:
        report_header(
            report,
            args,
            stats_rooms,
            anchor_rooms,
            whitelist,
            len(entries),
        )

        for entry in entries:
            result = import_file(
                entry,
                store_obj,
                stats_rooms=stats_rooms,
                anchor_rooms=anchor_rooms,
                whitelist=whitelist,
                force=args.force,
                dry_run=args.dry_run,
                verbose=args.verbose,
                min_age_min=args.min_age_min,
            )
            pairs.append((entry, result))
            report_row(report, entry, result)
            totals[result.action] = totals.get(result.action, 0) + 1
            if result.action in notes:
                if result.action == "filtered":
                    notes[result.action][result.room] += 1
                else:
                    notes[result.action].append(
                        f"  {result.note}  {entry.path.name}"
                    )
            seen += 1
            read += result.read or 0
            written += result.written or 0
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

        report_summary(
            report,
            pairs,
            totals,
            read,
            written,
            final_count,
            time.time() - started,
            args.dry_run,
        )

    print_skip_report(notes)
    print_summary(
        totals,
        read,
        written,
        final_count,
        time.time() - started,
        args.dry_run,
    )
    if args.report:
        print(f"\n报告已写入：{args.report}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
