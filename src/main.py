import argparse
import asyncio
import logging
import logging_config
from liveroom import DoyinLiveRoom
from datetime import datetime
import sys
import os
import aiohttp
import json
import re
import signal
import time

shutdown_event = asyncio.Event()
start_time = time.time()
shutdown_count = 0
logger = logging.getLogger(__name__)


def handle_signal(signum, frame):
    global shutdown_count
    shutdown_count += 1
    elapsed = time.time() - start_time

    if shutdown_count == 1:
        print(
            f"\n[MAIN] 收到信号 {signum} (已运行 {elapsed:.0f}s)，开始优雅关闭..."
        )
        logger.info(
            f"Received signal {signum}, initiating graceful shutdown..."
        )
        shutdown_event.set()
    elif shutdown_count == 2:
        print(f"\n[MAIN] 强制关闭请求...")
        logger.warning("Force shutdown requested")
    else:
        print(f"\n[MAIN] 程序即将退出")


async def safe_close_session(session):
    if session is None:
        return
    try:
        print("[NTFY] 正在关闭 HTTP Session...")
        await session.close()
        print("[NTFY] HTTP Session 关闭完成")
    except Exception as e:
        print(f"[NTFY] HTTP Session 关闭异常: {e}")
        logger.debug(f"Session close error: {e}")


signal.signal(signal.SIGINT, handle_signal)
signal.signal(signal.SIGTERM, handle_signal)

DEFAULT_LOG_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "..", "logs"
)


def load_whitelist() -> dict:
    script_dir = os.path.dirname(os.path.abspath(__file__))
    config_path = os.path.join(script_dir, "..", "config", "whitelist.json")
    if os.path.exists(config_path):
        with open(config_path, "r", encoding="utf-8") as f:
            config = json.load(f)
            return config.get("whitelist", {})
    return {}


WHITELIST = load_whitelist()

room_events = {}
room_stop_events = {}
room_log_suffixes = {}
room_loggers = {}
for room_id in WHITELIST.values():
    room_events[room_id] = asyncio.Event()
    room_stop_events[room_id] = asyncio.Event()
    room_log_suffixes[room_id] = None
    room_loggers[room_id] = None


async def ntfy_listener():
    print("[NTFY] ntfy_listener 启动")
    logger.info("ntfy_listener started")
    session = None
    try:
        session = aiohttp.ClientSession()
        while not shutdown_event.is_set():
            try:
                async with asyncio.timeout(2):
                    async with session.get(NTFY_URL, timeout=60) as r:
                        r.raise_for_status()
                        async for line in r.content:
                            if shutdown_event.is_set():
                                break
                            if line:
                                try:
                                    message_data = json.loads(
                                        line.decode("utf-8")
                                    )
                                    message_text = message_data.get("message")
                                    if message_text:
                                        logger.info(
                                            f"Received ntfy message: {message_text}"
                                        )
                                        match = re.search(
                                            r"直播间状态更新：(.*?) 正在直播中",
                                            message_text,
                                        )
                                        if match:
                                            streamer_name = match.group(
                                                1
                                            ).strip()
                                            for (
                                                name,
                                                room_id,
                                            ) in WHITELIST.items():
                                                if name in streamer_name:
                                                    logger.info(
                                                        f"Detected a live broadcast from: {streamer_name} (room: {room_id})."
                                                    )
                                                    room_log_suffixes[
                                                        room_id
                                                    ] = datetime.now().strftime(
                                                        "%Y-%m-%d_%H-%M-%S"
                                                    )
                                                    room_events[room_id].set()
                                                    break
                                        else:
                                            end_match = re.search(
                                                r"直播间状态更新：(.*?) 直播已结束",
                                                message_text,
                                            )
                                            if end_match:
                                                streamer_name = (
                                                    end_match.group(1).strip()
                                                )
                                                for (
                                                    name,
                                                    room_id,
                                                ) in WHITELIST.items():
                                                    if name in streamer_name:
                                                        logger.info(
                                                            f"Detected stream ended for: {streamer_name} (room: {room_id})."
                                                        )
                                                        room_stop_events[
                                                            room_id
                                                        ].set()
                                                        break
                                except json.JSONDecodeError:
                                    logger.error(
                                        f"Could not decode JSON: {line}"
                                    )
            except asyncio.TimeoutError:
                if shutdown_event.is_set():
                    break
                logger.debug(
                    "ntfy listener connection timed out. Reconnecting..."
                )
            except aiohttp.ClientError as e:
                if shutdown_event.is_set():
                    break
                logger.error(
                    f"An aiohttp error occurred in ntfy listener: {e}. Retrying in 5 seconds..."
                )
                await asyncio.sleep(5)
    except asyncio.CancelledError:
        print("[NTFY] ntfy_listener 收到取消信号")
        logger.info("ntfy_listener cancelled")
        raise
    finally:
        await safe_close_session(session)
        print("[NTFY] ntfy_listener 关闭完成")
        logger.info("ntfy_listener shutdown complete")


NTFY_URL = "http://localhost:10380/mytopic/json"


async def main_task_for_room(room_id: str):
    print(f"[ROOM-{room_id[:8]}] main_task_for_room 启动，等待触发")
    logger.info(f"Task for room {room_id} started")

    from websocket import (
        register_room_stop_callback,
        register_room_reconnect_callback,
    )

    async def on_room_stop():
        logger.info(f"Room {room_id} stream ended.")
        room_stop_events[room_id].set()

    register_room_stop_callback(room_id, on_room_stop)

    waiting_printed = False
    while not shutdown_event.is_set():
        if not waiting_printed:
            logger.info(
                f"Task for room {room_id} is ready and waiting for trigger..."
            )
            waiting_printed = True
        try:
            await asyncio.wait_for(room_events[room_id].wait(), timeout=1)
        except asyncio.TimeoutError:
            continue
        if shutdown_event.is_set():
            break
        waiting_printed = False
        print(f"[ROOM-{room_id[:8]}] 收到直播触发，开始连接...")
        logger.info(
            f"Task for room {room_id} received trigger. Starting application..."
        )
        room_events[room_id].clear()

        log_suffix = room_log_suffixes.get(room_id)
        if log_suffix:
            app_logger, stat_logger = logging_config.setup_room_logger(
                room_id, log_suffix, log_path
            )
            room_loggers[room_id] = (app_logger, stat_logger)
            app_logger.info(
                f"Started logging to new file with suffix: {log_suffix}"
            )

        async def on_reconnect():
            if room_loggers.get(room_id):
                room_loggers[room_id][0].info(
                    f"Room {room_id} connection timeout, reconnecting..."
                )

        register_room_reconnect_callback(room_id, on_reconnect)

        while not shutdown_event.is_set():
            try:
                async with await DoyinLiveRoom.new(room_id) as room:
                    ws = await room.create_websocket()
                    try:
                        while (
                            not ws._ws_session.closed
                            and not shutdown_event.is_set()
                        ):
                            await asyncio.sleep(1)
                    finally:
                        await ws.close(timeout=5)
            except (aiohttp.ClientError, asyncio.TimeoutError) as e:
                if room_loggers.get(room_id):
                    room_loggers[room_id][0].warning(
                        f"Room {room_id} connection failed: {e}. Retrying in 5s..."
                    )
                await asyncio.sleep(5)
                continue
            if room_stop_events[room_id].is_set():
                break
            if room_loggers.get(room_id):
                room_loggers[room_id][0].info(
                    f"Room {room_id} connection closed, reconnecting..."
                )
        if room_stop_events[room_id].is_set():
            if room_loggers.get(room_id):
                room_loggers[room_id][0].info(
                    f"Task for room {room_id} finished this session. Waiting for next trigger..."
                )
            room_stop_events[room_id].clear()
        else:
            if room_loggers.get(room_id):
                room_loggers[room_id][0].info(
                    f"Task for room {room_id} finished this session. Waiting for next trigger..."
                )

    print(f"[ROOM-{room_id[:8]}] main_task_for_room 关闭完成")
    logger.info(f"Task for room {room_id} shutdown complete")


async def main_task_for_room_single(room_id: str):
    print(f"[SINGLE-{room_id[:8]}] main_task_for_room_single 启动")
    logger.info(f"Single mode started for room {room_id}")

    from websocket import (
        register_room_stop_callback,
        register_room_reconnect_callback,
    )

    log_suffix = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    app_logger, stat_logger = logging_config.setup_room_logger(
        room_id, log_suffix, log_path
    )
    app_logger.info(f"Started logging to new file with suffix: {log_suffix}")

    async def on_room_stop():
        app_logger.info(f"Room {room_id} stream ended.")

    register_room_stop_callback(room_id, on_room_stop)

    async def on_reconnect():
        app_logger.info(f"Room {room_id} connection timeout, reconnecting...")

    register_room_reconnect_callback(room_id, on_reconnect)

    while not shutdown_event.is_set():
        try:
            async with await DoyinLiveRoom.new(room_id) as room:
                ws = await room.create_websocket()
                try:
                    while (
                        not ws._ws_session.closed
                        and not shutdown_event.is_set()
                    ):
                        await asyncio.sleep(1)
                finally:
                    await ws.close(timeout=5)
        except (aiohttp.ClientError, asyncio.TimeoutError) as e:
            app_logger.warning(
                f"Room {room_id} connection failed: {e}. Retrying in 5s..."
            )
            await asyncio.sleep(5)
            continue
        app_logger.info(f"Room {room_id} connection closed, reconnecting...")

    print(f"[SINGLE-{room_id[:8]}] main_task_for_room_single 关闭完成")
    logger.info("main_task_for_room_single shutdown complete")


async def run_concurrent_tasks():
    print("[MAIN] 启动 TaskGroup，运行所有任务...")
    logger.info("Starting TaskGroup with all tasks...")
    try:
        async with asyncio.TaskGroup() as tg:
            tg.create_task(ntfy_listener())
            for room_id in WHITELIST.values():
                tg.create_task(main_task_for_room(room_id))
    except BaseExceptionGroup as EG:
        for exc in EG.exceptions:
            if isinstance(exc, asyncio.CancelledError):
                print("[MAIN] TaskGroup 收到取消信号")
                logger.info("TaskGroup cancelled")
            else:
                logger.error(f"TaskGroup exception: {exc}")
    except asyncio.CancelledError:
        print("[MAIN] TaskGroup 收到取消信号")
        logger.info("TaskGroup cancelled")
    print("[MAIN] TaskGroup 退出，所有任务已关闭")
    logger.info("All tasks shut down")


if __name__ == "__main__":
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

    args = parser.parse_args()

    room_id = args.room
    log_path = DEFAULT_LOG_PATH

    if room_id:
        log_suffix = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
        logging_config.setup_logging(log_suffix, log_path, room_id)
        logger.info("Application started in direct mode.")
        try:
            asyncio.run(main_task_for_room_single(room_id))
        except KeyboardInterrupt:
            pass
    else:
        log_suffix = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
        logging_config.setup_app_logging(
            log_suffix, log_path, enable_room_prefix=True
        )
        logger.info("Running in watch mode...")
        try:
            asyncio.run(run_concurrent_tasks())
        except KeyboardInterrupt:
            pass

    print("[MAIN] 程序退出")
    sys.exit(0)
