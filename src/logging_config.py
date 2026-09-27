import json
import logging
import os
import sys

DEFAULT_LOG_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "..", "logs"
)


class JsonFormatter(logging.Formatter):
    def format(self, record):
        log_record = {
            "timestamp": self.formatTime(record, self.datefmt),
            "level": record.levelname,
            "name": record.name,
            "message": record.getMessage(),
        }
        try:
            msg_dict = json.loads(record.getMessage())
            log_record.update(msg_dict)
        except (json.JSONDecodeError, TypeError):
            pass

        return json.dumps(log_record, ensure_ascii=False)


def setup_app_logging(
    log_file_suffix: str,
    log_file_path: str = DEFAULT_LOG_PATH,
    enable_room_prefix: bool = False,
    room_id: str | None = None,
):
    """
    配置统一的应用日志系统，只设置控制台和主应用日志文件。
    这个函数只应该在应用启动时调用一次。

    room_id 提前传入时会先建好该房间的日志目录（单房间模式），
    供后续 setup_room_logger 写入统计日志。
    """
    if room_id:
        os.makedirs(os.path.join(log_file_path, room_id), exist_ok=True)
    root_logger = logging.getLogger()
    root_logger.setLevel(logging.DEBUG)

    full_log_path = os.path.join(log_file_path, f"app-{log_file_suffix}.log")
    os.makedirs(log_file_path, exist_ok=True)

    file_handler = logging.FileHandler(full_log_path)
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(JsonFormatter())

    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setLevel(logging.INFO)

    if enable_room_prefix:
        formatter = logging.Formatter(
            "%(asctime)s - [%(name)s] - %(levelname)s - %(message)s"
        )
    else:
        formatter = logging.Formatter(
            "%(asctime)s - %(name)s - %(levelname)s - %(message)s"
        )
    console_handler.setFormatter(formatter)

    root_logger.addHandler(file_handler)
    root_logger.addHandler(console_handler)

    logging.info("App logging system configured successfully.")


def setup_room_logger(
    room_id: str, suffix: str, log_file_path: str = DEFAULT_LOG_PATH
) -> tuple:
    """
    为指定房间设置独立的统计日志记录器。
    返回 (app_logger, stat_logger) 元组。
    """
    os.makedirs(os.path.join(log_file_path, room_id), exist_ok=True)

    app_logger = logging.getLogger(f"room_{room_id}")
    app_logger.setLevel(logging.DEBUG)

    stats_log_path = os.path.join(
        log_file_path, room_id, f"stats-{suffix}.log"
    )
    stat_file_handler = logging.FileHandler(stats_log_path)
    stat_file_handler.setLevel(logging.INFO)
    stat_file_handler.setFormatter(JsonFormatter())

    stat_logger = logging.getLogger(f"stat_{room_id}")
    stat_logger.setLevel(logging.INFO)
    stat_logger.propagate = False
    for handler in list(stat_logger.handlers):
        stat_logger.removeHandler(handler)
        handler.close()
    stat_logger.addHandler(stat_file_handler)

    return app_logger, stat_logger
