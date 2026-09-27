import json
import os


def load_whitelist() -> dict:
    """读取 config/whitelist.json，返回 主播名 -> web_rid 映射。"""
    script_dir = os.path.dirname(os.path.abspath(__file__))
    config_path = os.path.join(script_dir, "..", "config", "whitelist.json")
    if os.path.exists(config_path):
        with open(config_path, "r", encoding="utf-8") as f:
            config = json.load(f)
            return config.get("whitelist", {})
    return {}


def match_whitelist(streamer_name: str, whitelist: dict) -> str | None:
    """在白名单中查找与 ntfy 通知主播名匹配的房间 web_rid。

    沿用历史子串匹配语义：白名单的键只要是通知里主播名的一部分即命中。
    """
    for name, web_rid in whitelist.items():
        if name in streamer_name:
            return web_rid
    return None
