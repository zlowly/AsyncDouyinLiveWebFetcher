import asyncio
import logging
import re
from urllib.parse import urljoin

import aiohttp
from yarl import URL

from constants import CONSTANTS
from utils import generate_signature
from websocket import DouyinChatWebSocketClient


class InvalidWebridError(ValueError):
    """无法从直播间页面解析出 roomId（房间不存在或已下线）。"""

    pass


def _parse_cookie_header(raw: str) -> dict[str, str]:
    """把浏览器复制的整串 Cookie 头解析成 name -> value 映射。

    值里可能含 '='（如 base64），只按第一个 '=' 切分；跳过无 '=' 的碎段。
    """
    parsed: dict[str, str] = {}
    for pair in raw.split(";"):
        name, sep, value = pair.partition("=")
        name = name.strip()
        if sep and name:
            parsed[name] = value.strip()
    return parsed


class DoyinLiveRoom:
    def __init__(
        self,
        web_rid: str,
        session: aiohttp.ClientSession,
        owns_session: bool = True,
        stat_logger: logging.Logger | None = None,
        stop_event: asyncio.Event | None = None,
    ):
        self.web_rid = web_rid
        self.headers = {"User-Agent": CONSTANTS.USER_AGENT}
        # room_id 没有默认值，必须由 new() 解析页面后写入才能用。
        self._session = session
        # 仅当实例自建 session 时才负责关闭它；外部注入的 session 由
        # 调用方管理生命周期。
        self._owns_session = owns_session
        self._stat_logger = stat_logger
        self._stop_event = stop_event

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        if self._owns_session and self._session:
            await self._session.close()

    @classmethod
    async def new(
        cls,
        web_rid: str,
        timeout: float = 10.0,
        session: aiohttp.ClientSession | None = None,
        stat_logger: logging.Logger | None = None,
        stop_event: asyncio.Event | None = None,
        auth_cookie: str = "",
    ):
        owns_session = session is None
        if session is None:
            session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=timeout)
            )
        instance = cls(
            web_rid,
            session,
            owns_session=owns_session,
            stat_logger=stat_logger,
            stop_event=stop_event,
        )
        target = urljoin(CONSTANTS.BASE, web_rid)
        try:
            async with instance._session.get(
                CONSTANTS.BASE, headers=instance.headers
            ) as response:
                if response.status != 200:
                    raise aiohttp.ClientConnectionError(
                        "Failed to connect to Douyin Live."
                    )
            # 先种游客 __ac_nonce 兜底，再用登录 Cookie 覆盖同名项。
            cookies = {"__ac_nonce": CONSTANTS.AC_NONCE}
            if auth_cookie:
                cookies.update(_parse_cookie_header(auth_cookie))
            instance._session.cookie_jar.update_cookies(
                cookies=cookies,
                response_url=URL(CONSTANTS.BASE),
            )
            async with instance._session.get(
                target, headers=instance.headers
            ) as response:
                if response.status != 200:
                    raise aiohttp.ClientConnectionError(
                        "Failed to fetch the live room."
                    )
                match = re.search(
                    r'roomId\\":\\"(\d+)\\"', await response.text()
                )
                if match is None or len(match.groups()) < 1:
                    raise InvalidWebridError(
                        "Invalid webrid format or room ID not found."
                    )
                instance.room_id = match.group(1)
            return instance
        except Exception:
            if owns_session:
                await session.close()
            raise

    async def create_websocket(self):
        target = CONSTANTS.get_websocket_url(self.room_id)
        signature = generate_signature(target)
        target += f"&signature={signature}"

        return await DouyinChatWebSocketClient.new(
            session=self._session,
            url=target,
            headers=self.headers,
            room_id=self.web_rid,
            stat_logger=self._stat_logger,
            stop_event=self._stop_event,
        )
