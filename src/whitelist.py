def match_whitelist(streamer_name: str, whitelist: dict) -> str | None:
    """在白名单中查找与 ntfy 通知主播名匹配的房间 web_rid。

    沿用历史子串匹配语义：白名单的键只要是通知里主播名的一部分即命中。
    """
    for name, web_rid in whitelist.items():
        if name in streamer_name:
            return web_rid
    return None
