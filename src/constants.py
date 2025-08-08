from functools import lru_cache
from time import time
from urllib.parse import urlencode

from yarl import URL


class CONSTANTS:
    BROWSER_NAME = "Mozilla"
    BROWSER_VERSION = (
        "5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko)"
        "Chrome/138.0.0.0 Safari/537.36"
    )
    USER_AGENT = f"{BROWSER_NAME}/{BROWSER_VERSION}"
    BASE = "https://live.douyin.com/"
    WS_BASE = "wss://webcast100-ws-web-lq.douyin.com/"

    @staticmethod
    @lru_cache(maxsize=10)
    def get_status_url(web_rid: str, room_id: str) -> str:
        path = URL(CONSTANTS.BASE) / "webcast/room/web/enter/"
        query = urlencode(
            {
                "aid": 6383,
                "app_name": "douyin_web",
                "live_id": 1,
                "device_platform": "web",
                "language": "zh-CN",
                "enter_from": "web_live",
                "cookie_enabled": "true",
                "screen_width": 1920,
                "screen_height": 1080,
                "browser_language": "zh-CN",
                "browser_platform": "Win32",
                "browser_name": CONSTANTS.BROWSER_NAME,
                "browser_version": CONSTANTS.BROWSER_VERSION,
                "web_rid": web_rid,
                "room_id_str": room_id,
                "enter_source": "",
                "is_need_double_stream": "false",
                "insert_task_id": "",
                "live_reason": "",
                "msToken": "",
                "a_bogus": "",
            }
        )
        return str(path.update_query(query))

    @staticmethod
    @lru_cache(maxsize=10)
    def get_websocket_url(room_id: str):
        path = URL(CONSTANTS.WS_BASE) / "webcast/im/push/v2/"
        now = int(time())
        query = {
            "app_name": "douyin_web",
            "version_code": "180800",
            "webcast_sdk_version": "1.0.14-beta.0",
            "update_version_code": "1.0.14-beta.0",
            "compress": "gzip",
            "device_platform": "web",
            "cookie_enabled": "true",
            "screen_width": "1920",
            "screen_height": "1080",
            "browser_language": "zh-CN",
            "browser_platform": "Win32",
            "browser_name": CONSTANTS.BROWSER_NAME,
            "browser_version": CONSTANTS.BROWSER_VERSION,
            "browser_online": "true",
            "tz_name": "Asia/Shanghai",
            "cursor": "t-{now}-1_d-1_u-1_h-1",
            "internal_ext": (
                f"internal_src:dim|wss_push_room_id:{room_id}|wss_push_did:7319483754668557238"
                f"|first_req_ms:1721106114541|fetch_time:{now}|seq:1|wss_info:0-{now}-0-0|"
                f"wrds_v:7392094459690748497"
            ),
            "host": "https://live.douyin.com",
            "aid": "6383",
            "live_id": "1",
            "did_rule": "3",
            "endpoint": "live_pc",
            "support_wrds": "1",
            "user_unique_id": "7319483754668557238",
            "im_path": "/webcast/im/fetch/",
            "identity": "audience",
            "need_persist_msg_count": "15",
            "insert_task_id": "",
            "live_reason": "",
            "room_id": room_id,
            "heartbeatDuration": "0",
        }
        return str(path.update_query(query))
