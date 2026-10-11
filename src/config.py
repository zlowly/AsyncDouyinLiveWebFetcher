"""统一配置加载：读取项目根目录的 config.json。

config.json 取代了原先只有白名单的 config/whitelist.json，除白名单外
还描述日志的存放形式：

    logging.sinks   日志去处，可选 "file"（日志文件）/ "sqlite"（事件库）。
                    缺省或留空为两种都存。
    logging.log_dir 日志文件目录，相对路径按项目根目录解析。
    logging.db_path sqlite 事件库路径，相对路径按项目根目录解析。

watch 与单房间模式的日志去处完全一致，都是 sinks 说了算。弹幕行的
room_id 由 websocket._dump_debug 的 extra 字段带入（见 store.py 的
room_id_from_record），所以 watch 模式下多个房间的弹幕能各自归属。
"""

import json
import logging
import os
from dataclasses import dataclass

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG_PATH = os.path.join(PROJECT_ROOT, "config.json")

DEFAULT_LOG_DIR = os.path.join(PROJECT_ROOT, "logs")
DEFAULT_DB_PATH = os.path.join(PROJECT_ROOT, "data", "events.db")
# 缺省两种都存。
DEFAULT_SINKS = ("file", "sqlite")
VALID_SINKS = frozenset({"file", "sqlite"})

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class LoggingConfig:
    """日志存放形式。"""

    sinks: tuple[str, ...] = DEFAULT_SINKS
    log_dir: str = DEFAULT_LOG_DIR
    db_path: str = DEFAULT_DB_PATH

    @property
    def file_enabled(self) -> bool:
        return "file" in self.sinks

    @property
    def sqlite_enabled(self) -> bool:
        return "sqlite" in self.sinks


@dataclass(frozen=True)
class AppConfig:
    """config.json 的整体视图。"""

    whitelist: dict[str, str]
    logging: LoggingConfig


def _resolve_path(raw: object, default: str) -> str:
    """配置里的相对路径按项目根目录解析，空值退回默认路径。"""
    if not isinstance(raw, str) or not raw.strip():
        return default
    path = raw.strip()
    if os.path.isabs(path):
        return os.path.normpath(path)
    return os.path.normpath(os.path.join(PROJECT_ROOT, path))


def _parse_sinks(raw: object) -> tuple[str, ...]:
    """解析 sinks，缺省/全部非法时退回"两种都存"。"""
    if raw is None:
        return DEFAULT_SINKS
    if isinstance(raw, str):
        raw = [raw]
    if not isinstance(raw, list):
        logger.warning("logging.sinks 应为名字数组，已按缺省处理：%r", raw)
        return DEFAULT_SINKS

    sinks: list[str] = []
    for item in raw:
        name = str(item).strip().lower()
        if not name:
            continue
        if name not in VALID_SINKS:
            logger.warning("logging.sinks 含未知去处 %r，已忽略", item)
            continue
        if name not in sinks:
            sinks.append(name)
    # 允许显式关掉某一处，但两个都写错/都没写时仍按缺省来。
    if not sinks:
        logger.warning(
            "logging.sinks 未给出有效去处，已按缺省 %s 处理",
            "、".join(DEFAULT_SINKS),
        )
        return DEFAULT_SINKS
    return tuple(sinks)


def parse_logging(raw: object) -> LoggingConfig:
    """解析 config.json 的 logging 段，字段缺失时逐项退回缺省。"""
    if not isinstance(raw, dict):
        if raw is not None:
            logger.warning("logging 应为对象，已按缺省处理：%r", raw)
        return LoggingConfig()
    return LoggingConfig(
        sinks=_parse_sinks(raw.get("sinks")),
        log_dir=_resolve_path(raw.get("log_dir"), DEFAULT_LOG_DIR),
        db_path=_resolve_path(raw.get("db_path"), DEFAULT_DB_PATH),
    )


def parse_whitelist(raw: object) -> dict[str, str]:
    """解析白名单段，容忍 name -> web_rid 之外的脏数据。"""
    if not isinstance(raw, dict):
        if raw is not None:
            logger.warning("whitelist 应为对象，已按空表处理：%r", raw)
        return {}
    whitelist: dict[str, str] = {}
    for name, web_rid in raw.items():
        if isinstance(web_rid, str) and web_rid.strip():
            whitelist[str(name)] = web_rid.strip()
        else:
            logger.warning("白名单 %r 的房间号非法，已忽略：%r", name, web_rid)
    return whitelist


def load_config(path: str = CONFIG_PATH) -> AppConfig:
    """读取 config.json；文件缺失或解析失败时返回全缺省配置。"""
    data: object = {}
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning("读取配置 %s 失败，使用缺省配置：%s", path, exc)
            data = {}
    else:
        logger.warning("未找到配置文件 %s，使用缺省配置", path)

    if not isinstance(data, dict):
        logger.warning("配置根节点应为对象，使用缺省配置：%r", data)
        return AppConfig(whitelist={}, logging=LoggingConfig())

    return AppConfig(
        whitelist=parse_whitelist(data.get("whitelist")),
        logging=parse_logging(data.get("logging")),
    )
