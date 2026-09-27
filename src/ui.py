"""统一控制台输出：logging + RichHandler，取代散落的 print。

- logger 名为 "ui"，propagate=False：只出现在控制台，不写 app 日志文件
  （与原先 print 的行为一致）。
- RichHandler 关闭 time/level/path 显示，保持原先"消息自带时间前缀 +
  彩色 markup"的视觉效果；消息里的 rich markup（如 [white on #7386ea]）
  会照常渲染。
- 大写开头的方括号标签（如 [MAIN]、[ROOM-abc123]）不会被 rich 当作
  markup 解析，会原样输出。
"""

import logging
import sys

from rich.console import Console
from rich.logging import RichHandler

logger = logging.getLogger("ui")
logger.setLevel(logging.DEBUG)
logger.propagate = False

handler = RichHandler(
    console=Console(file=sys.stdout),
    show_time=False,
    show_level=False,
    show_path=False,
    markup=True,
    rich_tracebacks=False,
)
handler.setLevel(logging.DEBUG)
logger.addHandler(handler)


def info(message: str) -> None:
    """向控制台输出一条信息。"""
    logger.info(message)
