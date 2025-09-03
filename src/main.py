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
TARGET_STREAMER = "SL.艾珀Aper♰"

# 创建一个异步事件对象，用于在 ntfy 监听器和主任务之间通信
event_to_trigger_main_task = asyncio.Event()

# 定义全局变量，用于传递触发时的日志后缀和房间 ID
triggered_log_suffix = None
triggered_room_id = None


async def ntfy_listener():
    """
    一个异步函数，持续监听 ntfy 消息。
    当检测到特定消息时，设置一个异步事件并传递参数。
    """
    global triggered_log_suffix, triggered_room_id
    
    async with aiohttp.ClientSession() as session:
        while True:
            try:
                async with session.get(NTFY_URL, timeout=60) as r:
                    r.raise_for_status()
                    async for line in r.content:
                        if line:
                            try:
                                message_data = json.loads(line.decode('utf-8'))
                                message_text = message_data.get("message")
                                if message_text:
                                    logger.info(f"Received ntfy message: {message_text}")
                                    match = re.search(r"直播间状态更新：(.*?) 正在直播中", message_text)
                                    if match:
                                        streamer_name = match.group(1).strip()
                                        if streamer_name.startswith(TARGET_STREAMER):
                                            logger.info(f"Detected a live broadcast from: {streamer_name}.")
                                            # 更新全局变量，传递给主任务
                                            triggered_log_suffix = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
                                            triggered_room_id = args.room # 使用解析器中的默认 room_id
                                            # 设置事件，通知 main_task 可以开始运行了
                                            event_to_trigger_main_task.set()
                            except json.JSONDecodeError:
                                logger.error(f"Could not decode JSON: {line}")
            except aiohttp.ClientError as e:
                logger.error(f"An aiohttp error occurred in ntfy listener: {e}. Retrying in 5 seconds...")
                await asyncio.sleep(5)
            except asyncio.TimeoutError:
                logger.warning("ntfy listener connection timed out. Reconnecting...")

async def main_task(is_watch_mode=False):
    """
    一个异步函数，它会根据模式执行任务。
    在监视模式下，它会等待触发事件；否则，它会立即执行。
    """
    if is_watch_mode:
        # 在监视模式下，等待 ntfy_listener 的触发事件
        logger.info("Main task is ready and waiting for ntfy trigger...")
        await event_to_trigger_main_task.wait()
        logger.info("Main task received trigger. Starting application...")
        event_to_trigger_main_task.clear()
    else:
        # 在常规模式下，直接开始执行
        logger.info("Application started in normal mode.")
    # 执行你的核心业务逻辑
    async with await DoyinLiveRoom.new(args.room) as room:
        ws = await room.create_websocket()
        try:
            while True:
                await asyncio.sleep(60)
        except KeyboardInterrupt:
            await ws.close()
    logger.info("Main task finished its execution.")

async def run_concurrent_tasks():
    """包装多个协程的执行"""
    await asyncio.gather(ntfy_listener(), main_task(is_watch_mode=True))

if __name__ == "__main__":
    # 1. 创建解析器对象
    parser = argparse.ArgumentParser(
        description="运行直播间监控应用，并可自定义房间ID和日志文件名。"
    )

    # 2. 定义命令行参数
    parser.add_argument(
        "-r", "--room",
        type=str,
        default="74083423272",
        help="指定直播间的房间ID (默认: 74083423272)"
    )

    parser.add_argument(
        "-s", "--suffix",
        type=str,
        default=datetime.now().strftime("%Y-%m-%d_%H-%M-%S"),
        help="指定日志文件的后缀 (默认: 当前日期时间)"
    )

    parser.add_argument(
        "-p", "--path",
        type=str,
        default="logs",
        help="指定日志文件的保存路径。"
    )

    # --- 新增参数 ---
    parser.add_argument(
        "-w", "--watch",
        action="store_true",
        help="启用 ntfy 监听模式，等待消息触发。"
    )

    # 3. 解析命令行参数
    args = parser.parse_args()

    # 4. 从解析结果中获取参数值
    room_id = args.room
    log_suffix = args.suffix
    log_path = args.path

    # --- 根据 -w 参数决定运行模式 ---
    if args.watch:
        # 监视模式：同时运行 ntfy 监听器和主任务（等待触发）
        logging_config.setup_logging(log_suffix, log_path, room_id)
        logger = logging.getLogger(__name__)
        logger.info("Running in ntfy watch mode...")
        asyncio.run(run_concurrent_tasks())  # 运行包装后的协程
    else:
        # 常规模式：直接运行主任务
        logging_config.setup_logging(log_suffix, log_path, room_id)
        logger = logging.getLogger(__name__)
        asyncio.run(main_task(is_watch_mode=False))
