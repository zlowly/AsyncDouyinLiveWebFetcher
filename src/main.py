import argparse
import asyncio
import logging
import signal
import sys
import time
from dataclasses import replace
from datetime import datetime

import logging_config
import ui
from config import load_config
from ntfy import ntfy_listener
from session import (
    RoomContext,
    single_room_task,
    watch_room_task,
)

shutdown_event = asyncio.Event()
start_time = time.time()
shutdown_count = 0
logger = logging.getLogger(__name__)

VALID_SINKS = ("file", "sqlite")


def parse_sinks_arg(raw: str) -> tuple[str, ...]:
    """解析 --sinks 的逗号分隔值，非法项/空元组时报错退出。"""
    sinks = []
    for item in raw.split(","):
        name = item.strip().lower()
        if not name:
            continue
        if name not in VALID_SINKS:
            raise argparse.ArgumentTypeError(
                f"未知存储方式 {name!r}（可选：{', '.join(VALID_SINKS)}）"
            )
        if name not in sinks:
            sinks.append(name)
    if not sinks:
        raise argparse.ArgumentTypeError("--sinks 至少要指定一种存储方式")
    return tuple(sinks)


def handle_signal(signum, frame):
    global shutdown_count
    shutdown_count += 1
    elapsed = time.time() - start_time

    if shutdown_count == 1:
        ui.info(
            f"\n[MAIN] 收到信号 {signum} (已运行 {elapsed:.0f}s)，"
            "开始优雅关闭..."
        )
        logger.info(
            f"Received signal {signum}, initiating graceful shutdown..."
        )
        shutdown_event.set()
    elif shutdown_count == 2:
        ui.info("\n[MAIN] 强制关闭请求...")
        logger.warning("Force shutdown requested")
    else:
        ui.info("\n[MAIN] 程序即将退出")


def handle_live_start(ctx: RoomContext, streamer_name: str) -> None:
    """
    处理 ntfy 的开播通知，设置触发事件和本次会话的日志 suffix。

    会话进行中收到重复的开播通知时直接忽略：否则 suffix 会被覆盖，
    等当前会话结束再次触发时会为同一场直播多建一个 stats 文件。
    """
    logger.info(
        f"Detected a live broadcast from: {streamer_name} "
        f"(room: {ctx.web_rid})."
    )
    if ctx.active:
        logger.info(
            f"Room {ctx.web_rid} session already active, ignoring "
            "duplicate live notification."
        )
        return
    ctx.log_suffix = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    ctx.start_event.set()


def handle_live_end(ctx: RoomContext, streamer_name: str) -> None:
    """处理 ntfy 的下播通知，触发当前会话停止。"""
    logger.info(
        f"Detected stream ended for: {streamer_name} (room: {ctx.web_rid})."
    )
    ctx.stop_event.set()


async def run_watch_mode(whitelist: dict, contexts: dict) -> None:
    """watch 模式：ntfy 监听 + 每个白名单房间一个任务。"""
    ui.info("[MAIN] 启动 TaskGroup，运行所有任务...")
    logger.info("Starting TaskGroup with all tasks...")

    def on_start(web_rid: str, streamer_name: str) -> None:
        handle_live_start(contexts[web_rid], streamer_name)

    def on_end(web_rid: str, streamer_name: str) -> None:
        handle_live_end(contexts[web_rid], streamer_name)

    try:
        async with asyncio.TaskGroup() as tg:
            tg.create_task(
                ntfy_listener(shutdown_event, whitelist, on_start, on_end)
            )
            for ctx in contexts.values():
                tg.create_task(watch_room_task(ctx, shutdown_event))
    except BaseExceptionGroup as EG:
        for exc in EG.exceptions:
            if isinstance(exc, asyncio.CancelledError):
                ui.info("[MAIN] TaskGroup 收到取消信号")
                logger.info("TaskGroup cancelled")
            else:
                logger.error(f"TaskGroup exception: {exc}")
    except asyncio.CancelledError:
        ui.info("[MAIN] TaskGroup 收到取消信号")
        logger.info("TaskGroup cancelled")
    ui.info("[MAIN] TaskGroup 退出，所有任务已关闭")
    logger.info("All tasks shut down")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="运行直播间监控应用，并可自定义房间ID和日志文件名。"
    )
    parser.add_argument(
        "-r",
        "--room",
        type=str,
        required=False,
        help="指定直播间的房间ID（不指定则启用监控模式）",
    )
    parser.add_argument(
        "--sinks",
        type=parse_sinks_arg,
        default=None,
        help="覆盖 config.json 的 logging.sinks，逗号分隔的日志存储方式"
        f"（可选：{', '.join(VALID_SINKS)}；不指定则按 config.json）",
    )
    args = parser.parse_args()

    signal.signal(signal.SIGINT, handle_signal)
    signal.signal(signal.SIGTERM, handle_signal)

    app_config = load_config()
    log_cfg = app_config.logging
    if args.sinks is not None:
        log_cfg = replace(log_cfg, sinks=args.sinks)

    # 登录态只影响能否收到礼物推送，开播前就报一次，免得连上了才发现
    # 走的是游客通道。
    if app_config.auth.cookie:
        ui.info(
            f"[MAIN] 登录模式（config.local.json auth.cookie，"
            f"{len(app_config.auth.cookie.split(';'))} 项）"
        )
    else:
        ui.info(
            "[MAIN] 游客模式：未配置 config.local.json 的 auth.cookie，"
            "收不到礼物推送"
        )

    log_suffix = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    # sinks 缺省两种都存，watch 与单房间模式一致：日志文件和 sqlite 都写。
    # watch 模式下弹幕行的 room_id 由 websocket._dump_debug 的 extra 带入
    # （store.room_id_from_record 先读 extra），多个房间各自归属，不必等
    # import_logs.py 事后补。
    db_path = log_cfg.db_path if log_cfg.sqlite_enabled else ""
    try:
        if args.room:
            logging_config.setup_app_logging(
                log_suffix,
                log_file_path=log_cfg.log_dir,
                room_id=args.room,
                db_path=db_path,
                write_file=log_cfg.file_enabled,
            )
            logger.info("Application started in direct mode.")
            asyncio.run(
                single_room_task(
                    args.room,
                    shutdown_event,
                    auth_cookie=app_config.auth.cookie,
                )
            )
        else:
            logging_config.setup_app_logging(
                log_suffix,
                log_file_path=log_cfg.log_dir,
                enable_room_prefix=True,
                db_path=db_path,
                write_file=log_cfg.file_enabled,
            )
            logger.info("Running in watch mode...")
            whitelist = app_config.whitelist
            contexts = {
                web_rid: RoomContext(
                    web_rid=web_rid,
                    log_dir=log_cfg.log_dir,
                    write_log_file=log_cfg.file_enabled,
                    auth_cookie=app_config.auth.cookie,
                )
                for web_rid in whitelist.values()
            }
            asyncio.run(run_watch_mode(whitelist, contexts))
    except KeyboardInterrupt:
        pass

    ui.info("[MAIN] 程序退出")
    sys.exit(0)


if __name__ == "__main__":
    main()
