#!/usr/bin/env python3
"""读他自己名下的健身房预约记录（只读，不下单、不取消）。

思路：chrome-profile 里往往还留着 jAccount 可信会话，直接用 Selenium 打开预约平台，
点进「我的预约 / 预约记录」，把页面表格文本抓回来。日期由调用方解析。
"""
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

BASE = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE))
PROFILE = BASE / 'chrome-profile'
LOCK = Path('/tmp/gym_login.lock')

import browser_cleanup as BC  # noqa: E402
import login_and_cookie as L  # noqa: E402
from selenium import webdriver  # noqa: E402
from selenium.webdriver.chrome.options import Options  # noqa: E402
from selenium.webdriver.common.by import By  # noqa: E402


def acquire_lock():
    if LOCK.exists() and time.time() - LOCK.stat().st_mtime < 600:
        print('另一个登录/浏览器任务正在占用 profile，等它结束再跑', flush=True)
        return False
    LOCK.write_text(str(os.getpid()))
    import atexit
    atexit.register(lambda: LOCK.unlink(missing_ok=True))
    return True


def build_driver():
    opts = Options()
    opts.binary_location = os.environ.get(
        'CHROME_BINARY', '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome')
    opts.add_argument(f'--user-data-dir={PROFILE}')
    opts.add_argument('--window-size=1400,1000')
    opts.add_argument('--lang=zh-CN')
    opts.page_load_strategy = os.environ.get('PAGE_LOAD_STRATEGY', 'eager')
    opts.add_experimental_option('excludeSwitches', ['enable-automation'])
    opts.add_experimental_option('useAutomationExtension', False)
    opts.add_argument('--disable-blink-features=AutomationControlled')
    import requests
    try:
        st = requests.Session()
        st.trust_env = False
        if st.get('https://sports.sjtu.edu.cn/', timeout=8, allow_redirects=False).status_code in (200, 301, 302, 400, 401, 403):
            opts.add_argument('--proxy-server=direct://')
    except Exception:
        pass
    return webdriver.Chrome(options=opts)


def menu_texts(drv):
    out = []
    for el in drv.find_elements(By.CSS_SELECTOR, 'a, li, .el-menu-item, .el-tabs__item, span, div'):
        try:
            t = (el.text or '').strip()
        except Exception:
            continue
        if t and len(t) <= 16 and ('预约' in t or '订单' in t or '记录' in t or '我的' in t):
            out.append(t)
    seen, uniq = set(), []
    for t in out:
        if t not in seen:
            seen.add(t)
            uniq.append(t)
    return uniq


def click_entry(drv, needle):
    for el in drv.find_elements(By.XPATH, f'//*[self::a or self::li or self::span or self::div][contains(text(), "{needle}")]'):
        try:
            if el.is_displayed():
                el.click()
                return True
        except Exception:
            continue
    return False


def clean_cell(t):
    return re.sub(r'\s+', ' ', (t or '').strip())


def dump_tables(drv):
    rows = []
    for table in drv.find_elements(By.CSS_SELECTOR, 'table, .el-table, .list, ul'):
        for tr in table.find_elements(By.CSS_SELECTOR, 'tr, li, .el-table__row'):
            txt = clean_cell(tr.text)
            if txt and len(txt) < 400:
                rows.append(txt)
    uniq, seen = [], set()
    for r in rows:
        if r not in seen:
            seen.add(r)
            uniq.append(r)
    return uniq


def silent_sso(drv):
    """走一遍 profile 里的可信会话：打开登录页并点「校内人员登录」，能免密就免密。"""
    drv.get('https://sports.sjtu.edu.cn/pc/#/login')
    time.sleep(4)
    js = """
      for (const e of document.querySelectorAll('*')) {
        const t=[...e.childNodes].filter(n=>n.nodeType===3).map(n=>n.textContent.trim()).join('');
        if (t==='校内人员登录' || t==='登录') { e.click(); return true; }
      }
      return false;
    """
    for _ in range(8):
        try:
            drv.execute_script(js)
        except Exception:
            pass
        time.sleep(3)
        names = {c['name'] for c in drv.get_cookies()}
        if 'JSESSIONID' in names:
            break
    return {c['name'] for c in drv.get_cookies()}


def collect_orders(drv):
    """翻遍订单分页，抓所有「预订成功 / 已签到」的未来日期条目。"""
    hits = {}
    for page in range(1, 8):
        if page > 1:
            clicked = False
            for el in drv.find_elements(By.XPATH, "//*[self::a or self::li or self::button or self::span]"):
                try:
                    if (el.text or '').strip() == str(page) and el.is_displayed():
                        drv.execute_script('arguments[0].click();', el)
                        clicked = True
                        break
                except Exception:
                    continue
            if not clicked:
                break
            time.sleep(4)
        text = clean_cell(drv.find_element(By.TAG_NAME, 'BODY').text)
        if '我的订单' not in text:
            break
        for m in re.finditer(
                r'(预订成功|已签到|待使用|未使用)\s+(20\d\d-\d\d-\d\d)\s*￥?\d*\s*'
                r'(.*?)(?=预订成功|已签退|已取消|已签到|共\s*\d+\s*条|$)',
                text, re.S):
            status, date, rest = m.group(1), m.group(2), m.group(3)[:80]
            slot = re.search(r'(\d\d:\d\d-\d\d:\d\d)', rest)
            hits.setdefault(date, []).append(f'{status} {slot.group(1) if slot else ""} {rest.strip()[:60]}')
        print(f'--- 第 {page} 页 ---', flush=True)
        print(text[:1600], flush=True)
        if f'共' in text and str(page) not in text.split('共')[-1][:20]:
            pass
    return hits


def main():
    BC.sweep_stale()
    (PROFILE / 'SingletonLock').unlink(missing_ok=True)
    (PROFILE / 'SingletonSocket').unlink(missing_ok=True)
    if not acquire_lock():
        return 2
    time.sleep(2)
    drv = None
    try:
        drv = BC.register(build_driver())
        drv.set_page_load_timeout(90)
        print('登录态探测 cookie：', sorted(silent_sso(drv)), flush=True)
        drv.get('https://sports.sjtu.edu.cn/pc/#/Order')
        time.sleep(8)
        print('订单页：', drv.current_url, flush=True)
        if '我的订单' not in drv.page_source:
            drv.get(L.SPORTS_URL)
            time.sleep(5)
            click_entry(drv, '我的订单')
            time.sleep(6)
        hits = collect_orders(drv)
        today = time.strftime('%Y-%m-%d')
        future = {d: v for d, v in hits.items() if d >= today}
        print('SUMMARY ' + json.dumps({'all_dates': sorted(hits),
                                       'future': future}, ensure_ascii=False, indent=2), flush=True)
        return 0
    finally:
        if drv is not None:
            BC.close(drv)


if __name__ == '__main__':
    sys.exit(main())
