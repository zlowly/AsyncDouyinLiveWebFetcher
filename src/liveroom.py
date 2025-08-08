import re
from urllib.parse import urljoin

import aiohttp
from yarl import URL

from constants import CONSTANTS
from utils import generate_signature
from websocket import DouyinChatWebSocketClient


class DoyinLiveRoom:
    _session: aiohttp.ClientSession
    web_rid: str
    room_id: str
    headers: dict = {"User-Agent": CONSTANTS.USER_AGENT}

    def __init__(self):
        raise NotImplementedError(
            "This class cannot be instantiated directly."
        )

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        if self._session:
            await self._session.close()

    @classmethod
    async def new(
        cls,
        web_rid: str,
        timeout: float = 10.0,
        session: aiohttp.ClientSession | None = None,
    ):
        instance = object.__new__(cls)
        instance._session = (
            session
            if session
            else aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=timeout)
            )
        )
        instance.web_rid = web_rid
        target = urljoin(CONSTANTS.BASE, web_rid)
        try:
            async with instance._session.get(
                CONSTANTS.BASE, headers=instance.headers
            ) as response:
                if response.status != 200:
                    raise aiohttp.ClientConnectionError(
                        "Failed to connect to Douyin Live."
                    )
            instance._session.cookie_jar.update_cookies(
                cookies={"__ac_nonce": "0123407cc00a9e438deb4"},
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
                    raise ValueError(
                        "Invalid webrid format or room ID not found."
                    )
                instance.room_id = match.group(1)
            return instance
        except Exception as e:
            await instance._session.close()
            raise e

    async def get_info(self):
        target = CONSTANTS.get_status_url(
            web_rid=self.web_rid, room_id=self.room_id
        )
        async with self._session.get(target, headers=self.headers) as response:
            if response.status != 200:
                raise aiohttp.ClientConnectionError(
                    "Failed to fetch the room status."
                )
            return await response.json()

    async def get_is_alive(self) -> bool:
        return (await self.get_info()).get("data", {}).get("room_status", 2) == 0

    async def create_websocket(self):
        target = CONSTANTS.get_websocket_url(self.room_id)
        signature = generate_signature(target)
        target += f"&signature={signature}"

        return await DouyinChatWebSocketClient.new(
            session=self._session, url=target, headers=self.headers
        )
