import atexit
import json
import logging
import os
import sys

import store as event_store

DEFAULT_LOG_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "..", "logs"
)

# JsonFormatter 自己写入的基础字段，payload 顶层若出现同名 key 会覆盖它们。
BASE_FIELDS = frozenset({"timestamp", "level", "name", "message"})


class JsonFormatter(logging.Formatter):
    def format(self, record):
        raw_message = record.getMessage()
        log_record = {
            "timestamp": self.formatTime(record, self.datefmt),
            "level": record.levelname,
            "name": record.name,
            "message": raw_message,
        }
        try:
            payload = json.loads(raw_message)
        except (json.JSONDecodeError, TypeError):
            payload = None

        if isinstance(payload, dict):
            log_record.update(payload)
            # payload 的顶层 key 已经摊平进 log_record，message 只是同一份
            # 内容的字符串副本，可以省掉（日志体积约减半）。
            # 若 payload 会覆盖基础字段，则保留 message 以免丢信息。
            if not (BASE_FIELDS & payload.keys()):
                del log_record["message"]

        return json.dumps(log_record, ensure_ascii=False)


def _attach_event_store(
    log_file_suffix: str,
    room_id: str | None,
    db_path: str,
) -> None:
    """把实时事件库 handler 挂到 root logger 上。

    任何失败都只降级 —— 数据库绝不能把采集主流程带崩。

    db_path 为空串表示关闭实时入库（config.json 的 sinks 不含 sqlite）。
    """
    if not db_path:
        logging.getLogger(__name__).info(
            "Event store disabled: 实时入库已关闭"
            + ("，请关掉进程后用 import_logs.py 入库" if db_path == "" else "")
        )
        return

    try:
        store_obj = event_store.SqliteEventStore(db_path)
        handler = event_store.SqliteLogHandler(
            store_obj,
            room_id=room_id or "",
            session=log_file_suffix,
        )
    except Exception as exc:  # noqa: BLE001 - 降级不影响日志本身
        logging.getLogger(__name__).warning(
            "Event store disabled, file logging unaffected: %s", exc
        )
        return

    logging.getLogger().addHandler(handler)
    atexit.register(handler.close)
    logging.getLogger(__name__).info(
        "Event store enabled: %s (session=%s)",
        os.path.abspath(db_path),
        log_file_suffix,
    )


def setup_app_logging(
    log_file_suffix: str,
    log_file_path: str = DEFAULT_LOG_PATH,
    enable_room_prefix: bool = False,
    room_id: str | None = None,
    db_path: str = event_store.DEFAULT_DB_PATH,
    write_file: bool = True,
):
    """
    配置统一的应用日志系统，只设置控制台和主应用日志文件。
    这个函数只应该在应用启动时调用一次。

    room_id 提前传入时会先建好该房间的日志目录（单房间模式），
    供后续 setup_room_logger 写入统计日志；write_file=False 时不建目录。

    log_file_path / db_path 来自 config.json 的 logging 段。
    db_path 传空串可关闭实时入库；write_file=False 时不落日志文件，
    只保留控制台输出和事件库（sinks 不含 file 时用）。
    """
    if write_file and room_id:
        os.makedirs(os.path.join(log_file_path, room_id), exist_ok=True)
    root_logger = logging.getLogger()
    root_logger.setLevel(logging.DEBUG)

    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setLevel(logging.INFO)

    name_field = "[%(name)s]" if enable_room_prefix else "%(name)s"
    console_handler.setFormatter(
        logging.Formatter(
            f"%(asctime)s - {name_field} - %(levelname)s - %(message)s"
        )
    )
    root_logger.addHandler(console_handler)

    if write_file:
        full_log_path = os.path.join(
            log_file_path, f"app-{log_file_suffix}.log"
        )
        os.makedirs(log_file_path, exist_ok=True)

        file_handler = logging.FileHandler(full_log_path)
        file_handler.setLevel(logging.DEBUG)
        file_handler.setFormatter(JsonFormatter())
        root_logger.addHandler(file_handler)
    else:
        logging.getLogger(__name__).info(
            "File logging disabled: 仅输出到控制台"
        )

    _attach_event_store(log_file_suffix, room_id, db_path)

    logging.info("App logging system configured successfully.")


def setup_room_logger(
    room_id: str,
    suffix: str,
    log_file_path: str = DEFAULT_LOG_PATH,
    write_file: bool = True,
) -> tuple:
    """
    为指定房间设置独立的统计日志记录器。
    返回 (app_logger, stat_logger) 元组。

    write_file=False（sinks 不含 file）时统计不落盘，挂 NullHandler 兜底，
    调用方无需分支。
    """
    app_logger = logging.getLogger(f"room_{room_id}")
    app_logger.setLevel(logging.DEBUG)

    if not write_file:
        stat_file_handler = logging.NullHandler()
    else:
        os.makedirs(os.path.join(log_file_path, room_id), exist_ok=True)
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
