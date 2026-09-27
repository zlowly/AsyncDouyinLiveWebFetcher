import asyncio
import json
import logging
import re

import aiohttp

import ui
from whitelist import match_whitelist

logger = logging.getLogger(__name__)

NTFY_URL = "http://localhost:10380/mytopic/sse"

_START_RE = re.compile(r"直播间状态更新：(.*?) 正在直播中")
_END_RE = re.compile(r"直播间状态更新：(.*?) 直播已结束")


def parse_ntfy_message(text: str) -> tuple[str, str] | None:
    """从 ntfy 通知文本解析 (主播名, 动作)，动作为 start/end。

    无法识别的通知返回 None。
    """
    match = _START_RE.search(text)
    if match:
        return match.group(1).strip(), "start"
    match = _END_RE.search(text)
    if match:
        return match.group(1).strip(), "end"
    return None


async def ntfy_listener(shutdown_event, whitelist, on_start, on_end):
    """监听 ntfy SSE，解析开播/下播通知并回调 on_start/on_end。

    回调签名为 callback(web_rid, streamer_name)，仅在白名单命中时触发。
    """
    ui.info("[NTFY] ntfy_listener 启动")
    logger.info("ntfy_listener started")
    session = None
    retry_count = 0
    base_delay = 5
    max_delay = 60
    try:
        session = aiohttp.ClientSession()
        while not shutdown_event.is_set():
            try:
                async with session.get(
                    NTFY_URL,
                    timeout=aiohttp.ClientTimeout(total=300, sock_read=60),
                ) as r:
                    r.raise_for_status()
                    retry_count = 0
                    async for line in r.content:
                        if shutdown_event.is_set():
                            break
                        _handle_line(
                            line.decode("utf-8").strip(),
                            whitelist,
                            on_start,
                            on_end,
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
                retry_count += 1
                delay = min(base_delay * (2 ** (retry_count - 1)), max_delay)
                if getattr(e, "status", None) == 429:
                    logger.warning(
                        f"ntfy server rate limited (429). "
                        f"Retry {retry_count}, waiting {delay}s..."
                    )
                else:
                    logger.warning(
                        f"An aiohttp error occurred in ntfy listener: {e}. "
                        f"Retry {retry_count}, waiting {delay}s..."
                    )
                await asyncio.sleep(delay)
    except asyncio.CancelledError:
        ui.info("[NTFY] ntfy_listener 收到取消信号")
        logger.info("ntfy_listener cancelled")
        raise
    finally:
        await _close_session(session)
        ui.info("[NTFY] ntfy_listener 关闭完成")
        logger.info("ntfy_listener shutdown complete")


def _handle_line(line_str: str, whitelist, on_start, on_end) -> None:
    """处理一条 SSE 行（同步：仅解析并触发回调）。"""
    logger.debug(f"Received SSE line: {line_str}")
    if not line_str.startswith("data: "):
        return
    try:
        message_data = json.loads(line_str[6:])
    except json.JSONDecodeError:
        logger.error(f"Could not decode JSON: {line_str}")
        return
    if message_data.get("event") == "keepalive":
        return
    message_text = message_data.get("message")
    if not message_text:
        return
    logger.info(f"Received ntfy message: {message_text}")
    parsed = parse_ntfy_message(message_text)
    if parsed is None:
        return
    streamer_name, action = parsed
    web_rid = match_whitelist(streamer_name, whitelist)
    if web_rid is None:
        return
    if action == "start":
        on_start(web_rid, streamer_name)
    else:
        on_end(web_rid, streamer_name)


async def _close_session(session) -> None:
    if session is None:
        return
    try:
        ui.info("[NTFY] 正在关闭 HTTP Session...")
        await session.close()
        ui.info("[NTFY] HTTP Session 关闭完成")
    except Exception as e:
        ui.info(f"[NTFY] HTTP Session 关闭异常: {e}")
        logger.debug(f"Session close error: {e}")
