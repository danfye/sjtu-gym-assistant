import base64
import hmac
import time
from hashlib import sha256

import requests

def send_message_fangtang(title: str, content: str, key: str) -> str:
    """
    使用 Server酱 Turbo API 推送到微信
    文档：https://sct.ftqq.com/
    端点：https://sctapi.ftqq.com/<SCTKEY>.send
    """
    if not key:
        return "no key"
    url_candidates = [
        f"https://sctapi.ftqq.com/{key}.send",  # 官方推荐域名
        f"https://sct.ftqq.com/{key}.send",     # 历史域名（备用）
    ]
    last_err = None
    for url in url_candidates:
        try:
            resp = requests.post(url, data={"text": title, "desp": content}, timeout=8)
            return f"{resp.status_code} {resp.text[:300]}"
        except Exception as e:
            last_err = e
    return f"send fail: {last_err}"


def send_message_feishu(title: str, content: str, webhook: str, secret: str = "") -> str:
    """使用飞书自定义机器人 Webhook 推送文本消息。"""
    if not webhook:
        return "no webhook"
    text = f"{title}\n\n{content}".strip()
    payload = {
        "msg_type": "text",
        "content": {"text": text},
    }
    if secret:
        timestamp = str(int(time.time()))
        string_to_sign = f"{timestamp}\n{secret}"
        sign = base64.b64encode(
            hmac.new(string_to_sign.encode("utf-8"), b"", digestmod=sha256).digest()
        ).decode("utf-8")
        payload["timestamp"] = timestamp
        payload["sign"] = sign
    try:
        resp = requests.post(webhook, json=payload, timeout=8)
        return f"{resp.status_code} {resp.text[:300]}"
    except Exception as e:
        return f"send fail: {e}"


def send_message_wecom(title: str, content: str, webhook: str) -> str:
    """使用企业微信/WeCom 群机器人 Webhook 推送文本消息。"""
    if not webhook:
        return "no webhook"
    text = f"{title}\n\n{content}".strip()
    payload = {
        "msgtype": "text",
        "text": {"content": text},
    }
    try:
        resp = requests.post(webhook, json=payload, timeout=8)
        return f"{resp.status_code} {resp.text[:300]}"
    except Exception as e:
        return f"send fail: {e}"


def send_notifications(title: str, content: str, config: dict) -> dict:
    """向已配置的所有通知通道发送消息。"""
    config = config or {}
    results = {}
    fangtang_key = config.get("FANGTANG_KEY") or ""
    feishu_webhook = config.get("FEISHU_WEBHOOK") or ""
    feishu_secret = config.get("FEISHU_SECRET") or ""
    wecom_webhook = config.get("WECOM_WEBHOOK") or ""

    if fangtang_key:
        results["fangtang"] = send_message_fangtang(title, content, fangtang_key)
    if feishu_webhook:
        results["feishu"] = send_message_feishu(title, content, feishu_webhook, feishu_secret)
    if wecom_webhook:
        results["wecom"] = send_message_wecom(title, content, wecom_webhook)
    return results
