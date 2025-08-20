import argparse
import asyncio
import logging
import logging_config
from liveroom import DoyinLiveRoom
from datetime import datetime
import sys


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

    # 3. 解析命令行参数
    args = parser.parse_args()

    # 4. 从解析结果中获取参数值
    room_id = args.room
    log_suffix = args.suffix
    
    logging_config.setup_logging(log_suffix)
    logger = logging.getLogger(__name__)

    async def main():
        logger.info("Application started.")
        async with await DoyinLiveRoom.new(room_id) as room:
            ws = await room.create_websocket()
            try:
                while True:
                    await asyncio.sleep(60)
            except KeyboardInterrupt:
                await ws.close()
        logger.info("Application finished.")

    asyncio.run(main())
