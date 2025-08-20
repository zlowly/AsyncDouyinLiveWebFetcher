import asyncio
import gzip
import logging
import sys
import aiohttp
from rich import print

from protobuf.douyin import (
    ChatMessage,
    ControlMessage,
    EmojiChatMessage,
    FansclubMessage,
    GiftMessage,
    LikeMessage,
    MemberMessage,
    PushFrame,
    Response,
    RoomMessage,
    RoomRankMessage,
    RoomStatsMessage,
    RoomStreamAdaptationMessage,
    RoomUserSeqMessage,
    SocialMessage,
)

logger = logging.getLogger(__name__)
stat_logger = logging.getLogger("stat_logger")

class DouyinChatWebSocketClient:
    _ws_session: aiohttp.ClientWebSocketResponse
    _tasks: list[asyncio.Task]

    def __init__(self):
        raise NotImplementedError(
            "This class cannot be instantiated directly."
        )

    @classmethod
    async def new(
        cls, session: aiohttp.ClientSession, url: str, headers: dict
    ):
        instance = object.__new__(cls)
        instance._tasks = []
        instance._ws_session = await session.ws_connect(url, headers=headers)
        instance._tasks.append(asyncio.create_task(instance._send_heartbeat()))
        instance._tasks.append(asyncio.create_task(instance._receive_loop()))
        return instance

    async def close(self):
        for task in self._tasks:
            task.cancel()
        await self._ws_session.close()

    async def _send_heartbeat(self, interval: int = 5):
        try:
            while not self._ws_session.closed:
                payload = PushFrame(payload=b"hb").SerializeToString()
                await self._ws_session.ping(payload)
                await asyncio.sleep(interval)
        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.exception("Heartbeat error: %s", e)

    async def _receive_loop(self):
        try:
            async for msg in self._ws_session:
                if msg.type == aiohttp.WSMsgType.BINARY:
                    await self._handle_binary(msg.data)
                elif msg.type == aiohttp.WSMsgType.TEXT:
                    await self._handle_text(msg.data)
                elif msg.type in (
                    aiohttp.WSMsgType.CLOSE,
                    aiohttp.WSMsgType.ERROR,
                ):
                    break
        except asyncio.CancelledError:
            pass

    async def _handle_binary(self, data: bytes):
        package = PushFrame().parse(data)
        response = Response().parse(gzip.decompress(package.payload))

        if response.need_ack:
            ack = PushFrame(
                log_id=package.log_id,
                payload_type="ack",
                payload=response.internal_ext.encode("utf-8"),
            ).SerializeToString()
            await self._ws_session.send_bytes(ack)

        handlers = {
            "WebcastChatMessage": self._parseChatMsg,
            "WebcastGiftMessage": self._parseGiftMsg,
            "WebcastLikeMessage": self._parseLikeMsg,
            "WebcastMemberMessage": self._parseMemberMsg,
            "WebcastSocialMessage": self._parseSocialMsg,
            "WebcastRoomUserSeqMessage": self._parseRoomUserSeqMsg,
            "WebcastFansclubMessage": self._parseFansclubMsg,
            "WebcastControlMessage": self._parseControlMsg,
            "WebcastEmojiChatMessage": self._parseEmojiChatMsg,
            "WebcastRoomStatsMessage": self._parseRoomStatsMsg,
            "WebcastRoomMessage": self._parseRoomMsg,
            "WebcastRoomRankMessage": self._parseRankMsg,
            "WebcastRoomStreamAdaptationMessage": self._parseRoomStreamAdaptationMsg,
        }
        for msg in response.messages_list:
            handler = handlers.get(msg.method)
            if handler:
                try:
                    if asyncio.iscoroutinefunction(handler):
                        await handler(msg.payload)
                    else:
                        handler(msg.payload)
                except Exception as e:
                    logging.exception(e)

    async def _handle_text(self, data: str):
        pass

    def _parseChatMsg(self, payload):
        """聊天消息"""
        message = ChatMessage().parse(payload)
        user_name = message.user.nick_name
        #user_id = message.user.id
        pay_lvl = message.user.pay_grade.level
        fans_lvl = message.user.fans_club.data.level
        content = message.content
        logger.debug(message.to_json())
        print(f"【聊天msg】[white on #7386ea]{pay_lvl}[/white on #7386ea] [white on #9d7d30]{fans_lvl}[/white on #9d7d30] [#8CE7FF]{user_name}：[/#8CE7FF]{content}")

    def _parseGiftMsg(self, payload):
        """礼物消息"""
        message = GiftMessage().parse(payload)
        user_name = message.user.nick_name
        pay_lvl = message.user.pay_grade.level
        fans_lvl = message.user.fans_club.data.level
        gift_name = message.gift.name
        gift_cnt = message.combo_count
        logger.debug(message.to_json())
        print(f"【礼物msg】[white on #7386ea]{pay_lvl}[/white on #7386ea] [white on #9d7d30]{fans_lvl}[/white on #9d7d30] [#8CE7FF]{user_name}[/#8CE7FF] [#eba825]送出了 {gift_name}x{gift_cnt}[/#eba825]")

    def _parseLikeMsg(self, payload):
        """点赞消息"""
        message = LikeMessage().parse(payload)
        user_name = message.user.nick_name
        pay_lvl = message.user.pay_grade.level
        fans_lvl = message.user.fans_club.data.level
        count = message.count
        logger.debug(message.to_json())
        # print(f"【点赞msg】{user_name} 点了{count}个赞")

    def _parseMemberMsg(self, payload):
        """进入直播间消息"""
        message = MemberMessage().parse(payload)
        user_name = message.user.nick_name
        pay_lvl = message.user.pay_grade.level
        fans_lvl = message.user.fans_club.data.level
        gender = ["保密", "男", "女"][message.user.gender]
        logger.debug(message.to_json())
        print(f"【进场msg】[white on #7386ea]{pay_lvl}[/white on #7386ea] [white on #9d7d30]{fans_lvl}[/white on #9d7d30] [{gender}] [#8CE7FF]{user_name}[/#8CE7FF] 进入了直播间")

    def _parseSocialMsg(self, payload):
        """关注消息"""
        message = SocialMessage().parse(payload)
        user_name = message.user.nick_name
        pay_lvl = message.user.pay_grade.level
        logger.debug(message.to_json())
        print(f"【关注msg】[{user_id}]{user_name} 关注了主播")

    def _parseRoomUserSeqMsg(self, payload):
        """直播间统计"""
        message = RoomUserSeqMessage().parse(payload)
        current = message.total
        total = message.total_pv_for_anchor
        stat_logger.info({"method": "WebcastRoomUserSeqMessage", "totalUserCount": total, "audienceCount": current })
        logger.debug(message.to_json())
        print(f"【统计msg】当前观看人数: {current}, 累计观看人数: {total}")

    def _parseFansclubMsg(self, payload):
        """粉丝团消息"""
        message = FansclubMessage().parse(payload)
        content = message.content
        logger.debug(message.to_json())
        print(f"【粉丝团msg】 {content}")

    def _parseEmojiChatMsg(self, payload):
        """聊天表情包消息"""
        message = EmojiChatMessage().parse(payload)
        emoji_id = message.emoji_id
        #user = message.user
        user_name = message.user.nick_name
        common = message.common
        default_content = message.default_content
        logger.debug(message.to_json())
        print( f"【聊天表情包id】{user_name}: default_content: {default_content}")

    def _parseRoomMsg(self, payload):
        message = RoomMessage().parse(payload)
        common = message.common
        room_id = common.room_id
        logger.debug(message.to_json())
        print(f"【直播间msg】直播间id:{room_id}")

    def _parseRoomStatsMsg(self, payload):
        message = RoomStatsMessage().parse(payload)
        display_long = message.display_long
        logger.debug(message.to_json())
        print(f"【直播间统计msg】{display_long}")

    def _parseRankMsg(self, payload):
        message = RoomRankMessage().parse(payload)
        ranks_list = message.ranks_list
        logger.debug(message.to_json())
        # print(f"【直播间排行榜msg】{ranks_list}")

    async def _parseControlMsg(self, payload):
        """直播间状态消息"""
        message = ControlMessage().parse(payload)
        logger.debug(message.to_json())

        if message.status == 3:
            print("直播间已结束")
            await self.close()

    def _parseRoomStreamAdaptationMsg(self, payload):
        message = RoomStreamAdaptationMessage().parse(payload)
        adaptationType = message.adaptation_type
        logger.debug(message.to_json())
        # print(f"直播间adaptation: {adaptationType}")
