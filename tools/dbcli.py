#!/usr/bin/env python3
"""命令行参数解析与时间过滤的公共模块，供 tools/ 下的 sqlite 脚本共用。

被 process_log_sqlite.py 与 search_chat.py import，保证两边的
-S / --start / --end / -r 语义完全一致，不会各写一份慢慢漂移。

约定：
  parse_time_arg  返回 (datetime, 粒度)，粒度为
                  "datetime"（含日期）/ "date"（纯日期）/ "time"（纯时刻）
  build_time_filter 收的是上面那个元组或 None，不是裸 datetime；
                  纯时刻按"一天中的时刻"比较（跨日期均生效，支持跨午夜），
                  结束日期为纯日期时含义是当天 23:59:59.999999。
  build_time_filter 返回 Python 谓词；能表达成 epoch 区间的调用方
                  最好自己下推到 SQL 的 ts_epoch 列，走索引更快。
"""
import argparse
from datetime import datetime, timedelta

DATETIME_FORMATS = [
    ("%Y-%m-%d %H:%M:%S,%f", "datetime"),
    ("%Y-%m-%d %H:%M:%S.%f", "datetime"),
    ("%Y-%m-%d %H:%M:%S", "datetime"),
    ("%Y-%m-%d %H:%M", "datetime"),
    ("%Y-%m-%d", "date"),
]
TIME_FORMATS = ["%H:%M:%S,%f", "%H:%M:%S.%f", "%H:%M:%S", "%H:%M"]


def parse_time_arg(value):
    """
    解析 --start/--end 参数，返回 (datetime, 粒度)。
    粒度为 "datetime"/"date"/"time"；仅时分时用 1900-01-01 占位日期。
    """
    value = value.strip()
    for fmt, granularity in DATETIME_FORMATS:
        try:
            return datetime.strptime(value, fmt), granularity
        except ValueError:
            continue
    for fmt in TIME_FORMATS:
        try:
            dt = datetime.strptime(value, fmt)
            return dt.replace(year=1900, month=1, day=1), "time"
        except ValueError:
            continue
    raise argparse.ArgumentTypeError(
        f"无法解析时间 '{value}'，"
        "支持: YYYY-MM-DD[ HH:MM[:SS]] 或 HH:MM[:SS]"
    )


def parse_room_arg(value):
    """
    解析 -r 参数：逗号分隔的房间号（允许空格），返回去重后的列表。
    """
    rooms = []
    for part in value.split(","):
        room = part.strip()
        if room and room not in rooms:
            rooms.append(room)
    if not rooms:
        raise argparse.ArgumentTypeError(
            f"无法解析房间号 '{value}'，"
            "应为逗号分隔的房间号，如 74083423272,249069870647"
        )
    return rooms


def parse_log_timestamp(value):
    """解析时间戳字符串，失败返回 None。"""
    if not value:
        return None
    for fmt, _ in DATETIME_FORMATS:
        try:
            return datetime.strptime(value, fmt)
        except ValueError:
            continue
    return None


def _seconds_of_day(dt):
    return dt.hour * 3600 + dt.minute * 60 + dt.second + dt.microsecond / 1e6


def build_time_filter(start, end):
    """
    根据起止时间构造时间谓词，返回 None 表示不过滤。
    仅给出时分时按"一天中的时刻"比较（跨日期均生效，支持跨午夜区间）。
    结束日期为纯日期时，含义是当天 23:59:59.999999。
    """
    if start is None and end is None:
        return None

    start_dt, start_gran = start if start else (None, None)
    end_dt, end_gran = end if end else (None, None)

    if end_gran == "date" and end_dt is not None:
        end_dt = end_dt + timedelta(days=1) - timedelta(microseconds=1)

    if "time" in (start_gran, end_gran):
        start_sec = _seconds_of_day(start_dt) if start_dt else None
        end_sec = _seconds_of_day(end_dt) if end_dt else None

        def filter_by_clock(ts):
            sec = _seconds_of_day(ts)
            if (
                start_sec is not None
                and end_sec is not None
                and start_sec > end_sec
            ):
                return sec >= start_sec or sec <= end_sec
            if start_sec is not None and sec < start_sec:
                return False
            if end_sec is not None and sec > end_sec:
                return False
            return True

        return filter_by_clock

    def filter_by_datetime(ts):
        if start_dt is not None and ts < start_dt:
            return False
        if end_dt is not None and ts > end_dt:
            return False
        return True

    return filter_by_datetime
