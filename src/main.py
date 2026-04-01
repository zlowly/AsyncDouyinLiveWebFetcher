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
import time

# 定义 ntfy 相关的常量
NTFY_URL = "http://localhost:10380/mytopic/json"

# 白名单：主播名 -> 房间号
WHITELIST = {
    "温缇": "249069870647",
    "涂山柒柒": "868467964350",
    "戏清玥": "729171023487",
}

# 直播间事件字典，用于在 ntfy 监听器和各主任务之间通信
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
    """
    一个异步函数，持续监听 ntfy 消息。
    当检测到白名单中的主播直播时，设置对应房间的事件并传递参数。
    """
    async with aiohttp.ClientSession() as session:
        while True:
            try:
                async with session.get(NTFY_URL, timeout=60) as r:
                    r.raise_for_status()
                    async for line in r.content:
                        if line:
                            try:
                                message_data = json.loads(line.decode("utf-8"))
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
                                        streamer_name = match.group(1).strip()
                                        for name, room_id in WHITELIST.items():
                                            if name in streamer_name:
                                                logger.info(
                                                    f"Detected a live broadcast from: {streamer_name} (room: {room_id})."
                                                )
                                                room_log_suffixes[room_id] = (
                                                    datetime.now().strftime(
                                                        "%Y-%m-%d_%H-%M-%S"
                                                    )
                                                )
                                                room_events[room_id].set()
                                                break
                                    else:
                                        end_match = re.search(
                                            r"直播间状态更新：(.*?) 直播已结束",
                                            message_text,
                                        )
                                        if end_match:
                                            streamer_name = end_match.group(
                                                1
                                            ).strip()
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
                                logger.error(f"Could not decode JSON: {line}")
            except aiohttp.ClientError as e:
                logger.error(
                    f"An aiohttp error occurred in ntfy listener: {e}. Retrying in 5 seconds..."
                )
                await asyncio.sleep(5)
            except asyncio.TimeoutError:
                logger.debug(
                    "ntfy listener connection timed out. Reconnecting..."
                )


async def main_task_for_room(room_id: str):
    """
    为指定直播间运行的任务。
    在监视模式下等待触发事件后开始执行，收到结束消息后关闭。
    """
    from websocket import (
        register_room_stop_callback,
        register_room_reconnect_callback,
    )

    async def on_room_stop():
        logger.info(f"Room {room_id} stream ended.")
        room_stop_events[room_id].set()

    register_room_stop_callback(room_id, on_room_stop)

    while True:
        logger = logging.getLogger(__name__)
        logger.info(
            f"Task for room {room_id} is ready and waiting for trigger..."
        )
        await room_events[room_id].wait()
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

        reconnect_event = asyncio.Event()

        async def on_reconnect():
            if room_loggers.get(room_id):
                room_loggers[room_id][0].info(
                    f"Room {room_id} connection timeout, reconnecting..."
                )

        register_room_reconnect_callback(room_id, on_reconnect)

        while True:
            try:
                async with await DoyinLiveRoom.new(room_id) as room:
                    ws = await room.create_websocket()
                    try:
                        while not ws._ws_session.closed:
                            await asyncio.sleep(1)
                    except KeyboardInterrupt:
                        break
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


async def run_concurrent_tasks():
    """包装多个协程的执行"""
    tasks: list[asyncio.Task] = [asyncio.create_task(ntfy_listener())]
    for room_id in WHITELIST.values():
        tasks.append(asyncio.create_task(main_task_for_room(room_id)))
    await asyncio.gather(*tasks)


if __name__ == "__main__":
    # 1. 创建解析器对象
    parser = argparse.ArgumentParser(
        description="运行直播间监控应用，并可自定义房间ID和日志文件名。"
    )

    # 2. 定义命令行参数
    parser.add_argument(
        "-r",
        "--room",
        type=str,
        default="249069870647",
        help="指定直播间的房间ID (默认: 249069870647)",
    )

    parser.add_argument(
        "-s",
        "--suffix",
        type=str,
        default=datetime.now().strftime("%Y-%m-%d_%H-%M-%S"),
        help="指定日志文件的后缀 (默认: 当前日期时间)",
    )

    parser.add_argument(
        "-p",
        "--path",
        type=str,
        default="logs",
        help="指定日志文件的保存路径。",
    )

    # --- 新增参数 ---
    parser.add_argument(
        "-w",
        "--watch",
        action="store_true",
        help="启用 ntfy 监听模式，等待消息触发。",
    )

    # 3. 解析命令行参数
    args = parser.parse_args()

    # 4. 从解析结果中获取参数值
    room_id = args.room
    log_suffix = args.suffix
    log_path = args.path

    # --- 根据 -w 参数决定运行模式 ---
    if args.watch:
        # 监视模式：启用房间前缀，便于区分多房间日志
        logging_config.setup_app_logging(
            log_suffix, log_path, enable_room_prefix=True
        )
        logger = logging.getLogger(__name__)
        logger.info("Running in ntfy watch mode...")
        asyncio.run(run_concurrent_tasks())  # 运行包装后的协程
    else:
        # 常规模式：直接运行主任务
        logging_config.setup_logging(log_suffix, log_path, room_id)
        logger = logging.getLogger(__name__)
        logger.info("Application started in normal mode.")
        asyncio.run(main_task_for_room(room_id))
