"""自动重登的快速路径：浏览器 profile 里往往已经是登录状态，先直接取会话并验证，
取不到再走完整登录流程（避免“已登录却苦等 jAccount 跳转”导致的连败）。

用法：python harvest_session.py            # 单独运行，把会话写进 state/cookie.json
      from harvest_session import harvest_existing   # 供 scan_notify 调用
"""
import os
import sys
import time
from pathlib import Path

BASE = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE))

from selenium import webdriver

import login_and_cookie as L
import browser_cleanup as BC
from cookie_store import save_cookie
from strict_login import build_options, harvest, probe


def harvest_existing(attempts: int = 4, wait: float = 5.0):
    """返回可用 cookie 字符串，取不到返回 None。不会点击任何登录按钮。"""
    drv = webdriver.Chrome(options=build_options(headless=False, anti_detect=True))
    drv.set_page_load_timeout(120)
    try:
        drv.get(L.SPORTS_URL)
        time.sleep(6)
        for i in range(1, attempts + 1):
            ck = harvest(drv)
            ok, why = probe(ck)
            if ok:
                save_cookie(ck)
                print(f'现有会话可直接复用（第 {i} 次探测：{why}）', flush=True)
                return ck
            print(f'第 {i} 次取会话不可用：{why}', flush=True)
            time.sleep(wait)
            try:
                drv.refresh()
                time.sleep(4)
            except Exception:
                pass
        return None
    finally:
        BC.close(drv)


if __name__ == '__main__':
    BC.install_handlers()
    ck = harvest_existing()
    if not ck:
        print('profile 里没有可用会话，请跑 strict_login.py 或 login_with_2fa.py', flush=True)
    sys.exit(0 if ck else 1)
