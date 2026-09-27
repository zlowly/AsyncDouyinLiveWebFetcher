import asyncio
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime

import aiohttp

import logging_config
import ui
from liveroom import DoyinLiveRoom, InvalidWebridError
from utils import wait_first

logger = logging.getLogger(__name__)


@dataclass
class RoomContext:
    """单个房间在 watch/single 模式下的全部运行时状态。

    取代过去散落在 main.py 的 5 个 room_* 全局 dict。
    """

    web_rid: str
    start_event: asyncio.Event = field(default_factory=asyncio.Event)
    stop_event: asyncio.Event = field(default_factory=asyncio.Event)
    active: bool = False
    log_suffix: str | None = None
    stats_suffix: str | None = None
    stat_logger: logging.Logger | None = None

    @property
    def app_logger(self) -> logging.Logger:
        return logging.getLogger(f"room_{self.web_rid}")

    def setup_stats(self) -> None:
        """为本场直播创建统计日志（同一 suffix 只建一次）。"""
        if not self.log_suffix or self.log_suffix == self.stats_suffix:
            return
        app_logger, stat_logger = logging_config.setup_room_logger(
            self.web_rid, self.log_suffix
        )
        self.stats_suffix = self.log_suffix
        self.stat_logger = stat_logger
        app_logger.info(
            f"Started logging to new file with suffix: {self.log_suffix}"
        )


async def wait_for_trigger(ctx: RoomContext, shutdown_event) -> bool:
    """等待 ntfy 开播触发；因程序关闭退出时返回 False。"""
    ctx.app_logger.info(
        f"Task for room {ctx.web_rid} is ready and waiting for trigger..."
    )
    while not shutdown_event.is_set() and not ctx.start_event.is_set():
        await wait_first(ctx.start_event.wait(), shutdown_event.wait())
    return not shutdown_event.is_set()


async def run_room_session(
    ctx: RoomContext,
    shutdown_event,
    invalid_giveup: float | None = 600.0,
) -> None:
    """维持一场直播的连接：反复重连直到下播/探测超时/程序退出。

    invalid_giveup 为 InvalidWebridError 连续探测的最长秒数，
    None 表示永不放弃（single 模式语义）。
    """
    ctx.setup_stats()
    app_logger = ctx.app_logger
    probe_started = time.monotonic()
    while not shutdown_event.is_set() and not ctx.stop_event.is_set():
        try:
            probe_started = await _connect_once(ctx, shutdown_event)
        except asyncio.CancelledError:
            app_logger.info(f"Room {ctx.web_rid} task cancelled")
            raise
        except (aiohttp.ClientError, asyncio.TimeoutError) as e:
            app_logger.warning(
                f"Room {ctx.web_rid} connection failed: {e}. Retrying in 5s..."
            )
            await _pause(5, shutdown_event, ctx.stop_event)
            continue
        except InvalidWebridError as e:
            give_up = await _handle_invalid_webrid(
                ctx, shutdown_event, e, probe_started, invalid_giveup
            )
            if give_up:
                break
            continue
        if shutdown_event.is_set() or ctx.stop_event.is_set():
            break
        app_logger.info(
            f"Room {ctx.web_rid} connection closed, reconnecting..."
        )
    app_logger.info(f"Room {ctx.web_rid} session ended.")


async def watch_room_task(ctx: RoomContext, shutdown_event) -> None:
    """watch 模式下的单房间任务：等待触发 -> 会话 -> 回到等待。"""
    ui.info(f"[ROOM-{ctx.web_rid[:8]}] watch task 启动，等待触发")
    logger.info(f"Task for room {ctx.web_rid} started")
    while not shutdown_event.is_set():
        if not await wait_for_trigger(ctx, shutdown_event):
            break
        ui.info(f"[ROOM-{ctx.web_rid[:8]}] 收到直播触发，开始连接...")
        logger.info(
            f"Task for room {ctx.web_rid} received trigger. "
            "Starting application..."
        )
        ctx.start_event.clear()
        ctx.stop_event.clear()
        ctx.active = True
        try:
            await run_room_session(ctx, shutdown_event, invalid_giveup=600.0)
        finally:
            ctx.active = False
        ctx.app_logger.info(
            f"Task for room {ctx.web_rid} finished this session. "
            "Waiting for next trigger..."
        )
    ui.info(f"[ROOM-{ctx.web_rid[:8]}] watch task 关闭完成")
    logger.info(f"Task for room {ctx.web_rid} shutdown complete")


async def single_room_task(web_rid: str, shutdown_event) -> None:
    """single 模式：连接房间直到下播或程序关闭。"""
    ui.info(f"[SINGLE-{web_rid[:8]}] single_room_task 启动")
    logger.info(f"Single mode started for room {web_rid}")
    ctx = RoomContext(
        web_rid=web_rid,
        log_suffix=datetime.now().strftime("%Y-%m-%d_%H-%M-%S"),
    )
    await run_room_session(ctx, shutdown_event, invalid_giveup=None)
    ui.info(f"[SINGLE-{web_rid[:8]}] single_room_task 关闭完成")
    logger.info("single_room_task shutdown complete")


async def _connect_once(ctx: RoomContext, shutdown_event):
    """建立一次连接并保持到关闭，返回探测计时起点。

    连接成功即返回新的 probe_started（重置 InvalidWebrid 探测窗口）。
    """
    async with await DoyinLiveRoom.new(
        ctx.web_rid,
        stat_logger=ctx.stat_logger,
        stop_event=ctx.stop_event,
    ) as room:
        ws = await room.create_websocket()
        probe_started = time.monotonic()
        try:
            await wait_first(ws.wait_closed(), shutdown_event.wait())
        finally:
            if not ws.closed:
                await ws.close(timeout=5)
    return probe_started


async def _handle_invalid_webrid(
    ctx: RoomContext,
    shutdown_event,
    error: Exception,
    probe_started: float,
    invalid_giveup: float | None,
) -> bool:
    """处理"房间不存在/已下线"错误。

    返回 True 表示放弃本次会话，False 表示按退避间隔后重试。
    """
    if shutdown_event.is_set() or ctx.stop_event.is_set():
        return True
    elapsed = time.monotonic() - probe_started
    if invalid_giveup is not None and elapsed >= invalid_giveup:
        ctx.app_logger.warning(
            f"Room {ctx.web_rid} 探测超过 {invalid_giveup:.0f}s 未恢复，"
            f"结束本次会话: {error}"
        )
        return True
    interval = _invalid_backoff(elapsed)
    ctx.app_logger.warning(
        f"Room {ctx.web_rid} 直播间不存在或已下线: {error}. "
        f"Retrying in {interval}s... (已探测 {elapsed:.0f}s)"
    )
    await _pause(interval, shutdown_event, ctx.stop_event)
    return False


def _invalid_backoff(elapsed: float) -> int:
    """InvalidWebridError 的阶梯退避间隔（秒）。"""
    if elapsed < 120:
        return 5
    if elapsed < 300:
        return 15
    return 30


async def _pause(seconds, *wake_events) -> None:
    """睡眠 seconds 秒，但可被任一唤醒事件（如下播/关机）提前打断。"""
    await wait_first(asyncio.sleep(seconds), *(e.wait() for e in wake_events))
