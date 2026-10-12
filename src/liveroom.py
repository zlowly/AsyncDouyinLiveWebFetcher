import asyncio
import logging
import re
from http.cookies import Morsel, SimpleCookie
from urllib.parse import urljoin

import aiohttp
from yarl import URL

from constants import CONSTANTS
from utils import generate_signature
from websocket import DouyinChatWebSocketClient

logger = logging.getLogger(__name__)


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


def _domain_cookies(raw: str, domain: str) -> SimpleCookie:
    """把整串 Cookie 头转成显式带 Domain 的 cookie 集合。

    浏览器发往 live.douyin.com 和 wss://webcast*-ws-web-*.douyin.com
    的是同一批登录 cookie，因为它们是 .douyin.com 的域 cookie；aiohttp
    的 CookieJar 也严格按域过滤，种的时候必须把 Domain 一起写上，否则
    会退化成 host-only cookie，WS 握手时整批被丢掉（表现为登录了却收
    不到礼物推送）。
    """
    scoped: SimpleCookie = SimpleCookie()
    for name, value in _parse_cookie_header(raw).items():
        morsel: Morsel = Morsel()
        # coded_val 交给 aiohttp 的 quote_cookie 再转义，与浏览器一致。
        morsel.set(name, value, value)
        morsel["domain"] = domain
        morsel["path"] = "/"
        scoped[name] = morsel
    return scoped


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
            # 游客 nonce 维持 host-only（只服务 live.douyin.com 的页面
            # 抓取）；登录 cookie 必须带 Domain=.douyin.com，否则
            # CookieJar 会因域不匹配在 WS 握手时把它们整批丢掉。
            instance._session.cookie_jar.update_cookies(
                cookies={"__ac_nonce": CONSTANTS.AC_NONCE},
                response_url=URL(CONSTANTS.BASE),
            )
            if auth_cookie:
                auth_cookies = _domain_cookies(
                    auth_cookie, CONSTANTS.AUTH_COOKIE_DOMAIN
                )
                instance._session.cookie_jar.update_cookies(
                    cookies=auth_cookies,
                    response_url=URL(CONSTANTS.BASE),
                )
                logger.debug(
                    "Seeded %d auth cookies with Domain=%s: %s",
                    len(auth_cookies),
                    CONSTANTS.AUTH_COOKIE_DOMAIN,
                    ", ".join(sorted(auth_cookies)),
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
        self._log_ws_cookies(target)

        return await DouyinChatWebSocketClient.new(
            session=self._session,
            url=target,
            headers=self.headers,
            room_id=self.web_rid,
            stat_logger=self._stat_logger,
            stop_event=self._stop_event,
        )

    def _log_ws_cookies(self, target: str) -> None:
        """DEBUG 下打印 WS 握手实际会带上的 cookie 名字（不打值）。

        登录 cookie 的 Domain 没配对时这里会是空的，是"登录了却收不到
        礼物推送"最直接的排查入口。
        """
        if not logger.isEnabledFor(logging.DEBUG):
            return
        sent = self._session.cookie_jar.filter_cookies(URL(target))
        logger.debug(
            "WS handshake %s will carry %d cookies: %s",
            CONSTANTS.WS_BASE,
            len(sent),
            ", ".join(sorted(sent)) or "(none)",
        )
