"""独立命令行工具：压缩/瘦身 logs/ 目录下的历史 app-*.log。

两种处理方式：

rewrite  近期日志。JsonFormatter 会把 protobuf 的 to_json() 同时写进
         message 字段和摊平后的顶层字段，message 只是同一份内容的字符串
         副本。删掉它体积约减半，内容 100% 可从剩余字段还原。
gzip     较旧日志。流式 gzip 归档，实测压缩比 20x 以上。

只处理 logs/app-*.log。logs/<room_id>/stats-*.log 不动 —— 那里的 message
是 dict 的 repr，不是 JSON，是唯一的数据载体。

用法：
    python src/scripts/compact_logs.py --dry-run
    python src/scripts/compact_logs.py
    python src/scripts/compact_logs.py --days 0      # 全部重写，不压缩
    python src/scripts/compact_logs.py --days 999    # 全部压缩
"""

import argparse
import dataclasses
import gzip
import json
import os
import re
import shutil
import sys
import time
from pathlib import Path

DEFAULT_LOGS_DIR = Path(__file__).resolve().parents[2] / "logs"

# JsonFormatter 自己写入的基础字段，见 logging_config.BASE_FIELDS。
BASE_FIELDS = frozenset({"timestamp", "level", "name", "message"})

LOG_NAME_RE = re.compile(r"^app-(\d{4}-\d{2}-\d{2})_(\d{2}-\d{2}-\d{2})\.log$")

MB = 1024 * 1024


@dataclasses.dataclass
class Result:
    """单个文件的处理结果。"""

    path: Path
    action: str  # rewrite / gzip / skip / fail
    before: int = 0
    after: int = 0
    lines: int = 0
    dropped: int = 0
    kept: int = 0
    note: str = ""


def droppable_payload(record) -> dict | None:
    """该行能否安全删除 message。

    能删时返回 payload（用于校验删除后信息完整），不能删返回 None。
    条件与 JsonFormatter 完全一致：行内除基础字段外还有别的 key、
    message 能解析成 dict、且 payload 顶层 key 不与基础字段同名。
    """
    if not isinstance(record, dict):
        return None
    if not set(record) - BASE_FIELDS:
        return None  # 非 payload 行
    message = record.get("message")
    if not isinstance(message, str):
        return None
    try:
        payload = json.loads(message)
    except (json.JSONDecodeError, TypeError):
        return None
    if not isinstance(payload, dict):
        return None
    if BASE_FIELDS & payload.keys():
        return None  # 会覆盖基础字段，保留 message
    return payload


def transform_line(raw: bytes) -> tuple[bytes, bool]:
    """返回 (输出字节, 是否删除了 message)。不需要改动时原样返回。"""
    try:
        record = json.loads(raw)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return raw, False

    payload = droppable_payload(record)
    if payload is None:
        return raw, False

    del record["message"]
    # 删掉 message 后剩余字段必须能完整还原 payload，否则中止。
    remaining = {k: v for k, v in record.items() if k not in BASE_FIELDS}
    if remaining != payload:
        raise ValueError("删除 message 后信息不可还原，已中止")

    tail = "\n" if raw.endswith(b"\n") else ""
    return (json.dumps(record, ensure_ascii=False) + tail).encode(
        "utf-8"
    ), True


def verify_file(path: Path, expected_lines: int) -> None:
    """回读校验：行数不变，且没有任何 payload 行仍带着 message。"""
    count = 0
    with open(path, "rb") as handle:
        for raw in handle:
            count += 1
            try:
                record = json.loads(raw)
            except (json.JSONDecodeError, UnicodeDecodeError):
                continue
            if droppable_payload(record) is not None:
                raise ValueError("仍存在可删除却没删的 message")
    if count != expected_lines:
        raise ValueError(f"行数不符：期望 {expected_lines}，实际 {count}")


def rewrite_file(path: Path, dry_run: bool, keep_backup: bool) -> Result:
    """原地重写：删除 payload 行里冗余的 message 字段。"""
    result = Result(path=path, action="rewrite")
    stat_before = path.stat()
    result.before = stat_before.st_size
    tmp_path = path.with_name(path.name + ".tmp")

    try:
        with open(path, "rb") as src, open(tmp_path, "wb") as dst:
            for raw in src:
                result.lines += 1
                out, dropped = transform_line(raw)
                if dropped:
                    result.dropped += 1
                else:
                    result.kept += 1
                dst.write(out)
        result.after = tmp_path.stat().st_size

        if dry_run:
            tmp_path.unlink()
            return result

        verify_file(tmp_path, result.lines)

        stat_after = path.stat()
        if (
            stat_before.st_size != stat_after.st_size
            or stat_before.st_mtime_ns != stat_after.st_mtime_ns
        ):
            raise RuntimeError("文件在处理过程中被修改（可能有进程在写）")

        if keep_backup:
            shutil.copy2(path, path.with_name(path.name + ".bak"))
        shutil.copymode(path, tmp_path)
        os.utime(tmp_path, (stat_before.st_atime, stat_before.st_mtime))
        os.replace(tmp_path, path)
    except Exception as exc:  # noqa: BLE001
        tmp_path.unlink(missing_ok=True)
        result.action = "fail"
        result.note = f"{type(exc).__name__}: {exc}"
    return result


def gzip_file(
    path: Path, level: int, dry_run: bool, keep_backup: bool
) -> Result:
    """流式 gzip 归档，原文件改名为 .gz（--backup 时保留 .bak）。"""
    result = Result(path=path, action="gzip")
    stat_before = path.stat()
    result.before = stat_before.st_size
    tmp_path = path.with_name(path.name + ".gz.tmp")
    target = path.with_name(path.name + ".gz")

    try:
        with open(path, "rb") as src, open(tmp_path, "wb") as raw_out:
            # mtime=0 让输出可复现，不嵌入处理时刻。
            with gzip.GzipFile(
                fileobj=raw_out, mode="wb", compresslevel=level, mtime=0
            ) as gz:
                shutil.copyfileobj(src, gz, length=MB)
        result.after = tmp_path.stat().st_size

        if dry_run:
            tmp_path.unlink()
            return result

        stat_after = path.stat()
        if stat_before.st_size != stat_after.st_size:
            raise RuntimeError("文件在处理过程中被修改（可能有进程在写）")

        os.utime(tmp_path, (stat_before.st_atime, stat_before.st_mtime))
        if keep_backup:
            os.replace(path, path.with_name(path.name + ".bak"))
        else:
            path.unlink()
        os.replace(tmp_path, target)
    except Exception as exc:  # noqa: BLE001
        tmp_path.unlink(missing_ok=True)
        result.action = "fail"
        result.note = f"{type(exc).__name__}: {exc}"
    return result


def pick_action(path: Path, cutoff: float | None) -> str:
    """按文件名里的起始时间决定处理方式。"""
    match = LOG_NAME_RE.match(path.name)
    if match is None or cutoff is None:
        return "rewrite"
    started = time.mktime(
        time.strptime(
            f"{match.group(1)} {match.group(2)}", "%Y-%m-%d %H-%M-%S"
        )
    )
    return "rewrite" if started >= cutoff else "gzip"


def human(size: int) -> str:
    if size >= MB:
        return f"{size / MB:.1f}MB"
    if size >= 1024:
        return f"{size / 1024:.0f}KB"
    return f"{size}B"


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="压缩/瘦身 logs/ 下的历史 app-*.log"
    )
    parser.add_argument(
        "--logs-dir", type=Path, default=DEFAULT_LOGS_DIR, help="日志目录"
    )
    parser.add_argument(
        "--days",
        type=int,
        default=30,
        help="近 N 天的日志重写去重，更早的 gzip 归档；"
        "0=全部重写，999=全部压缩",
    )
    parser.add_argument("--gzip-level", type=int, default=6)
    parser.add_argument("--dry-run", action="store_true", help="只统计不落盘")
    parser.add_argument(
        "--backup",
        action="store_true",
        help="原始文件改名为 .bak 保留而不是删除",
    )
    parser.add_argument(
        "--min-age-min",
        type=float,
        default=5.0,
        help="mtime 在该分钟数内变化的文件视为正在写入，跳过",
    )
    return parser.parse_args(argv)


def main(argv: list[str]) -> int:
    args = parse_args(argv)
    logs_dir = args.logs_dir
    if not logs_dir.is_dir():
        print(f"日志目录不存在：{logs_dir}", file=sys.stderr)
        return 2

    cutoff = time.time() - args.days * 86400 if args.days >= 0 else None
    now = time.time()
    results: list[Result] = []

    for path in sorted(logs_dir.glob("app-*.log")):
        age_min = (now - path.stat().st_mtime) / 60
        if age_min < args.min_age_min:
            results.append(
                Result(
                    path=path,
                    action="skip",
                    note=f"mtime 仅 {age_min:.1f} 分钟前，疑似正在写入",
                )
            )
            continue

        action = pick_action(path, cutoff)
        if action == "rewrite":
            results.append(rewrite_file(path, args.dry_run, args.backup))
        else:
            results.append(
                gzip_file(path, args.gzip_level, args.dry_run, args.backup)
            )

    report(results, args.dry_run)
    return 1 if any(r.action == "fail" for r in results) else 0


def report(results: list[Result], dry_run: bool) -> None:
    prefix = "[试运行] " if dry_run else ""
    print(
        f"\n{prefix}{'文件':44s} {'处理':7s} {'原大小':>9s} "
        f"{'新大小':>9s} {'省':>6s}  说明"
    )
    print("-" * 108)

    totals: dict[str, list[int]] = {}
    for r in results:
        before, after = r.before, r.after
        saved = 100 - (100 * after // before) if before else 0
        note = r.note
        if r.action == "rewrite" and r.lines:
            note = f"{r.lines}行, 去重{r.dropped}/保留{r.kept}"
        print(
            f"{prefix}{r.path.name:44s} {r.action:7s} "
            f"{human(before):>9s} {human(after):>9s} "
            f"{str(saved) + '%':>6s}  {note}"
        )
        acc = totals.setdefault(r.action, [0, 0, 0])
        acc[0] += 1
        acc[1] += before
        acc[2] += after

    print("-" * 108)
    for action, (count, before, after) in sorted(totals.items()):
        saved = 100 - (100 * after // before) if before else 0
        print(
            f"{action:8s} {count:3d} 个  {human(before):>9s} -> "
            f"{human(after):>9s}  省 {saved}%"
        )
    total_before = sum(a[1] for a in totals.values())
    total_after = sum(a[2] for a in totals.values())
    if total_before:
        print(
            f"{'合计':8s} {len(results):3d} 个  {human(total_before):>9s}"
            f" -> {human(total_after):>9s}  省 "
            f"{100 - (100 * total_after // total_before)}%"
        )


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
