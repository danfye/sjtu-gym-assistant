"""严格登录：以“真实查询接口能拿到 dateId”为唯一成功判据，避免 validate_cookie 的宽松误判。

依次尝试几种浏览器形态，哪种能拿到可用 session 就用哪种，并把 cookie 写进 state/cookie.json。
凭据只从环境变量读，不打印、不落盘。
"""
import json
import os
import subprocess
import sys
import time
from pathlib import Path

BASE = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE))

from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC

import login_and_cookie as L
import browser_cleanup as BC
from backend_api import query_date_ids
from cookie_store import save_cookie
from venue_map import VENUE_MAP

VM = VENUE_MAP['stu_gym']
PROBE_DATE = os.environ.get('PROBE_DATE', '2026-10-16')
PW = os.environ.get('JACCOUNT_PASSWORD', '')
UN = os.environ.get('JACCOUNT_USERNAME', '')
PROFILE = str(BASE / 'chrome-profile')


def build_options(headless: bool, anti_detect: bool):
    opts = Options()
    opts.binary_location = os.environ.get('CHROME_BINARY', '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome')
    opts.add_argument(f'--user-data-dir={PROFILE}')
    opts.add_argument('--window-size=1280,900')
    opts.add_argument('--lang=zh-CN')
    # 本机 DNS 解析不出学校域名时，显式走本地代理（由代理端解析）
    proxy = os.environ.get('CHROME_PROXY', '').strip()
    if proxy:
        opts.add_argument(f'--proxy-server={proxy}')
    # 本机 DNS 不可用时，直接把域名映射到 IP，绕开解析（例："MAP sports.sjtu.edu.cn 202.120.35.148, MAP jaccount.sjtu.edu.cn 202.120.2.109"）
    host_rules = os.environ.get('CHROME_HOST_RESOLVER', '').strip()
    if host_rules:
        opts.add_argument(f'--host-resolver-rules={host_rules}')
    if headless:
        opts.add_argument('--headless=new')
    if anti_detect:
        opts.add_experimental_option('excludeSwitches', ['enable-automation'])
        opts.add_experimental_option('useAutomationExtension', False)
        opts.add_argument('--disable-blink-features=AutomationControlled')
    return opts


def harvest(drv):
    return '; '.join(f"{c['name']}={c['value']}" for c in drv.get_cookies())


def probe(cookie):
    """真实判据：能不能拿到 dateId。"""
    if not cookie:
        return False, 'no cookie'
    try:
        d = query_date_ids(cookie, VM['venTypeId'], VM['venueId'], PROBE_DATE)
        if d:
            return True, f'dateIds={len(d)}'
        return False, 'dateIds=0'
    except Exception as exc:
        return False, f'{type(exc).__name__}: {str(exc)[:80]}'


def try_login(headless: bool, anti_detect: bool, label: str):
    print(f'\n--- 形态：{label} (headless={headless}, anti_detect={anti_detect}) ---', flush=True)
    BC.sweep_stale()  # 上一轮遗留的孤儿会占着 chrome-profile，新 Chrome 起不来
    try:
        drv = BC.register(webdriver.Chrome(options=build_options(headless, anti_detect)))
    except Exception:
        BC.sweep_stale()  # session 没建成但 Chrome 可能已经起来了，按进程树收掉
        raise
    drv.set_page_load_timeout(90)
    try:
        drv.get(L.SPORTS_URL)
        WebDriverWait(drv, 45).until(EC.presence_of_element_located((By.CSS_SELECTOR, '#app')))
        time.sleep(2)
        if not L._has_visible_login_button(drv):
            print('看起来已经是登录态，直接取 cookie', flush=True)
        else:
            btn = WebDriverWait(drv, 40).until(lambda d: L._find_school_login_button(d))
            drv.execute_script('arguments[0].click();', btn)
            WebDriverWait(drv, 40).until(lambda d: 'jaccount.sjtu.edu.cn' in d.current_url or d.find_elements(By.NAME, 'user'))
            time.sleep(1.5)
            if drv.find_elements(By.NAME, 'user'):
                print('需要输账号密码', flush=True)
                for attempt in range(1, 4):
                    user = WebDriverWait(drv, 25).until(EC.visibility_of_element_located((By.NAME, 'user')))
                    pwd = WebDriverWait(drv, 25).until(EC.visibility_of_element_located((By.NAME, 'pass')))
                    cap_img = WebDriverWait(drv, 25).until(EC.visibility_of_element_located((By.ID, 'captcha-img')))
                    cap_input = WebDriverWait(drv, 25).until(EC.visibility_of_element_located((By.ID, 'input-login-captcha')))
                    submit = WebDriverWait(drv, 25).until(EC.element_to_be_clickable((By.ID, 'submit-password-button')))
                    user.clear(); pwd.clear(); cap_input.clear()
                    user.send_keys(UN); pwd.send_keys(PW)
                    L._wait_img_complete(drv, cap_img, timeout_s=5.0)
                    code = L._solve_captcha_png(cap_img.screenshot_as_png)
                    print(f'  验证码识别 {code!r}，提交', flush=True)
                    cap_input.send_keys(code)
                    submit.click()
                    time.sleep(3)
                    body = ''
                    try:
                        body = drv.find_element(By.TAG_NAME, 'body').text or ''
                    except Exception:
                        pass
                    if '二次验证' in body:
                        print('  >>> 仍需二次验证，这个形态不行（受信任 profile 没生效？）', flush=True)
                        return False
                    if 'sports.sjtu.edu.cn' in (drv.current_url or ''):
                        break
            else:
                print('  免密跳回：SSO 自动通过', flush=True)
            try:
                WebDriverWait(drv, 30).until(lambda d: 'sports.sjtu.edu.cn' in d.current_url)
            except Exception:
                print('  没回到 sports，放弃本形态', flush=True)
                return False

        # 回到 sports 后多给前端一点时间把会话坐实，再反复取 cookie 验证
        for wait_round in range(1, 5):
            time.sleep(4)
            try:
                drv.refresh()
            except Exception:
                pass
            time.sleep(3)
            ck = harvest(drv)
            ok, why = probe(ck)
            names = [kv.split('=')[0] for kv in ck.split('; ')] if ck else []
            print(f'  取 cookie 第 {wait_round} 次：{why}；names={names}', flush=True)
            if ok:
                path = save_cookie(ck)
                print(f'成功，cookie 已写入 {path}', flush=True)
                return True
        print('连续 4 次取到的 cookie 都不能通过真实查询', flush=True)
        return False
    finally:
        BC.close(drv)


if __name__ == '__main__':
    BC.install_handlers()
    # 有窗口形态排在最前：headless 会被 jAccount 判成新设备、再次要求二次验证（2026-10-15 实测）
    cases = [
        (False, True, '有窗口 + 反自动化指纹'),
        (True, True, 'headless + 反自动化指纹'),
        (True, False, 'headless + 包内原始选项'),
    ]
    for hl, ad, label in cases:
        try:
            if try_login(hl, ad, label):
                sys.exit(0)
        except SystemExit:
            raise
        except Exception as exc:
            # 单个形态失败（页面超时、风控、站点慢）不该让整个脚本退出
            print(f'该形态异常，换下一个：{type(exc).__name__} {str(exc)[:150]}', flush=True)
            time.sleep(3)
    print('\n三种形态都拿不到可用 session，需要人工过一次登录+二次验证：跑 login_with_2fa.py', flush=True)
    sys.exit(1)
