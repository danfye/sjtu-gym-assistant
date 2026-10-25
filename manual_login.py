"""完全交给人的一键登录：脚本只负责开浏览器 + 等会话，登录动作全部由你本人做。

为什么需要它：jAccount 的图形验证码 OCR 会读错（实测把 4 位读成 3 位），自动填表反而卡在
「验证码错误」反复重试；而登录表单在单页应用里，脚本按元素名等待又会超时。这个脚本不做
任何自动点击，只反复用真实接口验证「浏览器里现在的会话能不能查排期」，能查就写
state/cookie.json 并退出。密码不需要经过这个脚本，也不会落盘。

用法（在项目目录里）：
    CHROME_USER_DATA_DIR="$PWD/chrome-profile" .venv/bin/python manual_login.py
环境变量：WAIT_MANUAL_SECONDS（默认 1200）、CHROME_PROXY、CHROME_HOST_RESOLVER。
"""
import os
import sys
import time
from pathlib import Path

base = Path(__file__).resolve().parent
sys.path.insert(0, str(base))

from selenium import webdriver
from selenium.webdriver.chrome.options import Options

import browser_cleanup as BC
import scan_notify as SN
from cookie_store import save_cookie

if __name__ != '__main__':
    raise RuntimeError('manual_login.py 请直接运行：python manual_login.py（不要 import 本模块）')

PROFILE_DIR = base / 'chrome-profile'
PROFILE_DIR.mkdir(exist_ok=True)
WAIT_SECONDS = int(os.environ.get('WAIT_MANUAL_SECONDS', '1200'))
PROBE_DATE = (time.time() + 86400)
PROBE_DATE = time.strftime('%Y-%m-%d', time.localtime(PROBE_DATE))

opts = Options()
opts.binary_location = os.environ.get(
    'CHROME_BINARY', '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome')
opts.add_argument(f'--user-data-dir={PROFILE_DIR}')
opts.add_argument('--window-size=1180,860')
opts.add_argument('--lang=zh-CN')
opts.add_experimental_option('excludeSwitches', ['enable-automation'])
opts.add_experimental_option('useAutomationExtension', False)
opts.add_argument('--disable-blink-features=AutomationControlled')
_proxy = os.environ.get('CHROME_PROXY', '')
_resolver = os.environ.get('CHROME_HOST_RESOLVER', '')
if _proxy:
    opts.add_argument(f'--proxy-server={_proxy}')
if _resolver:
    opts.add_argument(f'--host-resolver-rules={_resolver}')

drv = BC.register(webdriver.Chrome(options=opts))
BC.install_handlers(include_atexit=False)
drv.set_page_load_timeout(90)
try:
    drv.get('https://sports.sjtu.edu.cn/')
except Exception as exc:
    print(f'首页没加载出来（{type(exc).__name__}），页面停在哪儿你都可以在窗口里自己点开登录。', flush=True)

print('Chrome 已打开。请在这个窗口里完成登录：账号 + 密码 + 你自己看得懂的验证码，'
      '出现二次验证就扫码或填码，记得勾「信任此浏览器」。', flush=True)
print(f'我会每 5 秒用真实接口验一次会话，成功就写入 state/cookie.json（最长等 {WAIT_SECONDS // 60} 分钟）。', flush=True)

deadline = time.time() + WAIT_SECONDS
tick = 0
while time.time() < deadline:
    time.sleep(5)
    tick += 1
    try:
        header = '; '.join(f"{c['name']}={c['value']}" for c in drv.get_cookies())
    except Exception as exc:
        print(f'浏览器已经不在了（{type(exc).__name__}），结束等待。', flush=True)
        sys.exit(2)
    if 'JSESSIONID' not in header:
        if tick % 6 == 1:
            print('还没拿到 JSESSIONID，继续等你在窗口里操作...', flush=True)
        continue
    try:
        ids, msg = SN.is_full_state(header, PROBE_DATE)
    except Exception as exc:
        ids, msg = {}, f'{type(exc).__name__} {str(exc)[:60]}'
    if ids:
        path = save_cookie(header)
        print(f'会话可用：{PROBE_DATE} 拿到 {len(ids)} 个日期，已写入 {path}', flush=True)
        BC.close(drv)
        sys.exit(0)
    if tick % 6 == 1:
        print(f'已有 JSESSIONID 但接口不认（{msg[:60] or "空返回"}），继续等...', flush=True)

print('超时未拿到可用会话，Chrome 保持打开；你完成后重跑本脚本即可保存 cookie。', flush=True)
sys.exit(3)
