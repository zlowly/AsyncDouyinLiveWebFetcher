import hashlib
import urllib

from py_mini_racer import MiniRacer


def generate_signature(wss, script_file="sign.js"):
    """
    出现gbk编码问题则修改 python模块subprocess.py的源码中Popen类的__init__函数参数encoding值为 "utf-8"
    """
    params = (
        "live_id,aid,version_code,webcast_sdk_version,"
        "room_id,sub_room_id,sub_channel_id,did_rule,"
        "user_unique_id,device_platform,device_type,ac,"
        "identity"
    ).split(",")
    wss_params = urllib.parse.urlparse(wss).query.split("&")
    wss_maps = {i.split("=")[0]: i.split("=")[-1] for i in wss_params}
    tpl_params = [f"{i}={wss_maps.get(i, '')}" for i in params]
    param = ",".join(tpl_params)
    md5 = hashlib.md5()
    md5.update(param.encode())
    md5_param = md5.hexdigest()

    with open(f"scripts/{script_file}", "r", encoding="utf8") as f:
        script = f.read()

    ctx = MiniRacer()
    ctx.eval(script)

    signature = ctx.call("get_sign", md5_param)
    return signature
