from datetime import datetime
import os
from encryption import aes_encrypt, rsa_encrypt, get_key, PUBLIC_KEY


def wrap_confirm_body(body_json_str: str, cookie: str):
    """构造 ConfirmOrder 加密体与请求头。

    默认模拟 Chrome UA 与常见前端头，向旧脚本对齐；SPORTS_UA 环境变量可覆盖 UA。
    """
    aes_key = get_key()
    ts = int(datetime.now().timestamp() * 1000)
    UA = os.environ.get(
        'SPORTS_UA',
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_1) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/95.0.4638.69 Safari/537.36"
    )
    headers = {
        "Content-Type": "application/json;charset=utf-8",
        "Accept": "application/json, text/plain, */*",
        # 与旧脚本保持一致：不含 zstd
        "Accept-Encoding": "gzip, deflate, br",
        "Accept-Language": "zh-CN,zh;q=0.9",
        "Connection": "keep-alive",
        "sid": rsa_encrypt(aes_key, PUBLIC_KEY),
        "tim": rsa_encrypt(str(ts), PUBLIC_KEY),
        "Cookie": cookie,
        "User-Agent": UA,
        # Client Hints 与来源头
        "sec-ch-ua": "\"Google Chrome\";v=95, \"Chromium\";v=95, \"Not A Brand\";v=99",
        "sec-ch-ua-platform": "macOS",
        "Origin": "https://sports.sjtu.edu.cn",
        "Sec-Fetch-Site": "same-origin",
        "Sec-Fetch-Mode": "cors",
        "Sec-Fetch-Dest": "empty",
        "Referer": "https://sports.sjtu.edu.cn/pc/",
    }
    encrypted_body = aes_encrypt(body_json_str, aes_key)
    return encrypted_body, headers
