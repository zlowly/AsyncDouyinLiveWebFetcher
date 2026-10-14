#!/usr/bin/env python3
"""串行抓取抖音用户页的 IP 属地。

用法:
    ./fetch_ip_location.py                    # 读同目录 list.txt
    ./fetch_ip_location.py --list other.txt
    ./fetch_ip_location.py --interval 2 --csv out.csv
    ./fetch_ip_location.py --no-save

list.txt 每行一个 secUID 或完整 /user/ URL，空行与 # 注释忽略。
请求串行发出，每次请求后固定等待 interval 秒，避免给抖音造成压力。

环境要求：脚本 import 了 scrapling（`from scrapling.fetchers import Fetcher`），
必须在 scrapling conda 环境里运行（track.zsh 用该环境的 python3 调用本脚本）。
list.txt / ip_locations.csv 是运行时生成的临时工作文件，不随仓库跟踪；
track.zsh 管线总是显式传 --list/--csv，这里的缺省值仅供手工运行。
"""
from __future__ import annotations

import argparse
import csv
import re
import sys
import time
from pathlib import Path

from scrapling.fetchers import Fetcher

# 从浏览器 devtools 导出的签名 cookie，过期后整批会返回 challenge 挑战页
COOKIES = {
    "__ac_nonce": "06ab7599d00feae36ffa1",
    "__ac_signature": "_02B4Z6wo00f01FNfOdQAAIDA3RJP4tcTFtRTfz1AAH5o93",
}

BASE = "https://www.douyin.com/user/"
HERE = Path(__file__).resolve().parent
DEFAULT_LIST = HERE / "list.txt"
DEFAULT_CSV = HERE / "ip_locations.csv"

MIN_OK_BYTES = 20_000  # 小于这个长度基本是挑战/verify 空页
MIN_INTERVAL = 1.0  # 请求间隔下限，防止压到抖音

# IP 属地：优先取 SSR 渲染出的 DOM，其次取 JSON blob 里的 ipLocation 字段
RE_IP_SPAN = re.compile(r"<span[^>]*>\s*IP属地：([^<]+?)\s*</span>")
RE_IP_BLOB = re.compile(r'\\"ipLocation\\":\\"([^"\\]*)\\"')
RE_IP_BARE = re.compile(r"IP属地：([一-龥]{2,12})")

RE_H1 = re.compile(r'<[^>]*data-e2e="user-info"[^>]*>.*?</h1>', re.DOTALL)  # 渲染出的昵称
RE_TAG = re.compile(r"<[^>]+>")
RE_NICK = re.compile(r'\\"nickname\\":\\"([^"\\]*)\\"')
RE_DYID = re.compile(r"抖音号：([^<]+)</span>")
RE_FOLLOW = re.compile(r'\\"followerCount\\":(\d+)')
RE_AWEME = re.compile(r'\\"awemeCount\\":(\d+)')

# 岁数：优先取渲染 DOM 里紧跟抖音号/IP属地的 span，回退 blob 的 age 字段
RE_AGE_SPAN = re.compile(r"<span[^>]*>\s*(\d{1,3})岁\s*</span>")
RE_AGE_BLOB = re.compile(r'\\"age\\":(\d+)')

SCOPE_WINDOW = 12_000  # 以 secUid 为锚点的 blob 搜索窗口，避免抓到别人的字段

# 注意：顺序与 zsh 端 read_csv 的 columns 声明必须完全一致
COLUMNS = [
    "sec_uid",
    "ip_location",
    "nickname",
    "douyin_id",
    "follower_count",
    "aweme_count",
    "age",
    "status",
    "url",
]

EXIT_OK = 0
EXIT_CHALLENGE = 10  # 签名 cookie 失效
EXIT_RATE_LIMITED = 11  # 连续失败，疑似被限流

GOOD_STATUS = ("ok", "no_ip_location")

# 限流退避（宽松档）：连续 FAIL_TRIGGER 条失败触发下一档，每档 (新间隔秒, 冷却秒)
# 触发两次仍失败 → 第三次直接停止 (EXIT_RATE_LIMITED)
FAIL_TRIGGER = 3
BACKOFF_STEPS = (
    (5.0, 60.0),
    (20.0, 180.0),
)


def normalize_uid(line: str) -> str | None:
    """接受裸 secUID 或完整 /user/ URL，统一取出 /user/ 后面那段。"""
    line = line.strip().strip('"').strip()
    if not line or line.startswith("#"):
        return None
    if "/user/" in line:
        line = line.split("/user/", 1)[1]
    return line.split("?")[0].split("/")[0].strip() or None


def load_uids(path: Path) -> list[str]:
    if not path.is_file():
        sys.exit(f"列表文件不存在: {path}")
    seen: dict[str, None] = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        # 允许无表头 CSV（取第一列），其余列（如 url）忽略
        line = raw.split(",")[0] if "," in raw else raw
        uid = normalize_uid(line)
        if uid:
            seen.setdefault(uid)
    return list(seen)


def clean(value: str | None) -> str | None:
    value = (value or "").strip()
    return None if value in ("", "$undefined", "undefined", "null") else value


def extract(uid: str, html: str) -> dict[str, str | None]:
    ip = None
    if m := RE_IP_SPAN.search(html):
        ip = clean(m.group(1))
    elif m := RE_IP_BLOB.search(html):
        ip = clean(m.group(1).split("IP属地：", 1)[-1])
    elif m := RE_IP_BARE.search(html):
        ip = clean(m.group(1))

    # 抖音号来自 SSR 渲染 DOM，整页唯一
    m = RE_DYID.search(html)
    douyin_id = clean(m.group(1)) if m else None

    # 昵称优先取 <h1 data-e2e="user-info"> 的渲染文本
    nickname = None
    if m := RE_H1.search(html):
        nickname = clean(RE_TAG.sub("", m.group(0)))

    # 统计字段只在该 secUid 附近的 blob 里找，找不到再退回全局
    anchor = html.find(f'secUid\\":\\"{uid}')
    scope = html[anchor : anchor + SCOPE_WINDOW] if anchor != -1 else html

    def num(pattern: re.Pattern[str]) -> str | None:
        m = pattern.search(scope) or pattern.search(html)
        return m.group(1) if m else None

    if nickname is None and (m := RE_NICK.search(scope)):
        nickname = clean(m.group(1))

    age = None
    if (m := RE_AGE_SPAN.search(html)) or (m := RE_AGE_BLOB.search(scope)) or (m := RE_AGE_BLOB.search(html)):
        age = m.group(1)

    return {
        "ip_location": ip,
        "nickname": nickname,
        "douyin_id": douyin_id,
        "follower_count": num(RE_FOLLOW),
        "aweme_count": num(RE_AWEME),
        "age": age,
    }


def fetch_one(uid: str, timeout: int, retries: int = 1) -> dict[str, str | None]:
    url = BASE + uid
    row: dict[str, str | None] = {
        "sec_uid": uid,
        "ip_location": None,
        "nickname": None,
        "douyin_id": None,
        "follower_count": None,
        "aweme_count": None,
        "age": None,
        "status": "error",
        "url": url,
    }

    html = None
    last_err: Exception | None = None
    for attempt in range(retries + 1):
        try:
            resp = Fetcher.get(url, cookies=COOKIES, headers={"referer": url}, timeout=timeout)
            html = str(resp.html_content)
            if "jsvmprt" in html:  # test.html 混淆挑战页 = 签名 cookie 失效
                row["status"] = "challenge"
                return row
            if resp.status != 200:
                row["status"] = f"http_{resp.status}"
                return row
            if len(html) < MIN_OK_BYTES:
                row["status"] = f"empty_{len(html)}B"
                return row
            break
        except Exception as exc:  # noqa: BLE001 - 网络类异常统一重试
            last_err = exc
            if attempt < retries:
                time.sleep(2)
    else:
        row["status"] = f"error: {type(last_err).__name__}: {last_err}"
        return row

    assert html is not None
    row.update(extract(uid, html))
    row["status"] = "ok" if row["ip_location"] else "no_ip_location"
    return row


def print_table(rows: list[dict[str, str | None]]) -> None:
    head = ("#", "sec_uid", "ip_location", "age", "nickname", "douyin_id", "fans", "works", "status")
    table = []
    for i, r in enumerate(rows, 1):
        uid = r.get("sec_uid") or ""
        table.append(
            (
                str(i),
                uid[:16] + ("…" if len(uid) > 16 else ""),
                r["ip_location"] or "-",
                r.get("age") or "-",
                r["nickname"] or "-",
                r["douyin_id"] or "-",
                r["follower_count"] or "-",
                r["aweme_count"] or "-",
                r["status"] or "-",
            )
        )
    widths = [max(len(str(x)) for x in col) for col in zip(head, *table, strict=False)]
    for line in (head, *table):
        print("  ".join(str(cell).ljust(width) for cell, width in zip(line, widths)))


def write_csv(path: Path, rows: list[dict[str, str | None]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=COLUMNS, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def main() -> int:
    parser = argparse.ArgumentParser(description="串行抓取抖音用户页 IP 属地")
    parser.add_argument("--list", type=Path, default=DEFAULT_LIST, help="secUID/URL 列表文件（可为无表头 CSV）")
    parser.add_argument("--interval", type=float, default=1.0, help="每次请求后的间隔秒数，下限 1")
    parser.add_argument("--timeout", type=int, default=30, help="单次请求超时秒数")
    parser.add_argument("--csv", type=Path, default=DEFAULT_CSV, help="CSV 输出路径")
    parser.add_argument("--no-save", action="store_true", help="不写 CSV")
    args = parser.parse_args()

    interval = args.interval
    if interval < MIN_INTERVAL:
        print(f"⚠ --interval {interval:g}s 低于下限 {MIN_INTERVAL:g}s，已钳制为 {MIN_INTERVAL:g}s")
        interval = MIN_INTERVAL

    uids = load_uids(args.list)
    if not uids:
        print("列表为空，未发起任何请求")
        if not args.no_save:
            write_csv(args.csv, [])
        return EXIT_OK

    print(f"共 {len(uids)} 个 secUID | 串行 | 间隔 {interval:g}s | 列表 {args.list}\n")

    rows: list[dict[str, str | None]] = []
    exit_code = EXIT_OK
    stop_msg = None
    consecutive_fail = 0
    backoff_level = 0  # 已触发的降速档数

    for i, uid in enumerate(uids, 1):
        started = time.monotonic()
        row = fetch_one(uid, args.timeout)
        if row["status"] == "challenge":
            # 签名 cookie 失效：立刻停，且该行不落盘，保持待查以便续跑
            stop_msg = "签名 cookie 已失效（challenge 页），已中止"
            exit_code = EXIT_CHALLENGE
            break
        rows.append(row)
        if row["status"] in GOOD_STATUS:
            consecutive_fail = 0
        else:
            consecutive_fail += 1
        print(
            f"[{i}/{len(uids)}] {row['status']:<16} ip={row['ip_location'] or '-':<6} "
            f"age={row['age'] or '-':<4} {time.monotonic() - started:.1f}s  {uid[:20]}…"
        )

        sleep_for = interval if i < len(uids) else 0.0
        if consecutive_fail >= FAIL_TRIGGER:
            if backoff_level < len(BACKOFF_STEPS):
                interval, cooldown = BACKOFF_STEPS[backoff_level]
                backoff_level += 1
                consecutive_fail = 0
                print(
                    f"\n⚠ 连续 {FAIL_TRIGGER} 条失败 → 冷却 {cooldown:g}s，"
                    f"之后按 {interval:g}s 间隔继续（第 {backoff_level}/{len(BACKOFF_STEPS)} 档）"
                )
                time.sleep(cooldown)
                sleep_for = 0.0
            else:
                stop_msg = (
                    f"连续 {consecutive_fail} 条失败，且已降速至 {interval:g}s 仍失败，"
                    "疑似被限流，已中止"
                )
                exit_code = EXIT_RATE_LIMITED
                break
        if sleep_for > 0:
            time.sleep(sleep_for)

    print()
    if rows:
        print_table(rows)
    if stop_msg:
        print(f"\n⚠ {stop_msg}")
        if exit_code == EXIT_CHALLENGE:
            print("  请从浏览器 devtools 重新导出 __ac_nonce / __ac_signature 更新 COOKIES，")
            print("  之后重跑同一条命令即可从中断处继续（已完成的行不会重复请求）。")
        else:
            print("  已完成的行已落盘，未完成的保持待查状态，稍后重跑即可续跑。")

    if not args.no_save:
        write_csv(args.csv, rows)
        print(f"\n已保存 {len(rows)} 行 -> {args.csv}")

    return exit_code


if __name__ == "__main__":
    sys.exit(main())
