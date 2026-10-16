"""登录 -> 点进指定场馆 -> 点健身房分类，打印期间前端调用的所有 /venue/ 接口和页面里出现的 venueId。

只读：不点“预约/提交订单”。
用法：JACCOUNT_USERNAME=... JACCOUNT_PASSWORD=... .venv/bin/python venue_probe2.py 学生中心
"""
import os
import re
import sys
import time
from pathlib import Path

BASE = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE))

from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC

import login_and_cookie as L
from strict_login import build_options

NAME = sys.argv[1] if len(sys.argv) > 1 else '学生中心'
PW = os.environ.get('JACCOUNT_PASSWORD', '')
UN = os.environ.get('JACCOUNT_USERNAME', '')


def dump_api(drv, stage):
    urls = drv.execute_script("return performance.getEntriesByType('resource').map(e=>e.name);") or []
    hits = [u for u in urls if '/venue/' in u and not re.search(r'\.(js|css|png|jpg|svg|woff2?|gif|ico)$', u)]
    print(f'\n[{stage}] 接口调用 {len(hits)} 条：', flush=True)
    for u in sorted(set(hits)):
        print('   ', u[:220], flush=True)
    return hits


def main():
    opts = build_options(headless=False, anti_detect=True)
    opts.set_capability('goog:loggingPrefs', {'performance': 'ALL'})
    drv = webdriver.Chrome(options=opts)
    drv.set_page_load_timeout(90)
    try:
        drv.get(L.SPORTS_URL)
        WebDriverWait(drv, 60).until(EC.presence_of_element_located((By.CSS_SELECTOR, '#app')))
        time.sleep(3)
        dump_api(drv, '首页加载后')

        if L._has_visible_login_button(drv):
            btn = WebDriverWait(drv, 40).until(lambda d: L._find_school_login_button(d))
            drv.execute_script('arguments[0].click();', btn)
            WebDriverWait(drv, 40).until(lambda d: 'jaccount.sjtu.edu.cn' in d.current_url or d.find_elements(By.NAME, 'user'))
            time.sleep(1.5)
            for attempt in range(1, 5):
                if not drv.find_elements(By.NAME, 'user'):
                    break
                user = WebDriverWait(drv, 25).until(EC.visibility_of_element_located((By.NAME, 'user')))
                pwd = drv.find_element(By.NAME, 'pass')
                cap_img = drv.find_element(By.ID, 'captcha-img')
                cap_input = drv.find_element(By.ID, 'input-login-captcha')
                submit = drv.find_element(By.ID, 'submit-password-button')
                user.clear(); pwd.clear(); cap_input.clear()
                user.send_keys(UN); pwd.send_keys(PW)
                L._wait_img_complete(drv, cap_img, timeout_s=5.0)
                code = L._solve_captcha_png(cap_img.screenshot_as_png)
                cap_input.send_keys(code)
                submit.click()
                time.sleep(3)
                if 'sports.sjtu.edu.cn' in (drv.current_url or ''):
                    break
            try:
                WebDriverWait(drv, 40).until(lambda d: 'sports.sjtu.edu.cn' in d.current_url)
            except Exception:
                print('登录没回到 sports，可能仍需二次验证', flush=True)
                return
        time.sleep(4)
        print('登录完成，URL:', drv.current_url, flush=True)
        dump_api(drv, '登录后')

        for label in (NAME, '更多'):
            els = drv.find_elements(By.XPATH, f"//*[normalize-space(text())='{label}']")
            print(f'查找“{label}”：{len(els)} 个', flush=True)
            if not els:
                els = drv.find_elements(By.XPATH, f"//*[contains(text(),'{label}')]")
                print(f'  模糊匹配：{len(els)} 个 ->', [ (e.text or '')[:24] for e in els ][:8], flush=True)
            if els:
                try:
                    drv.execute_script("arguments[0].scrollIntoView({block:'center'});", els[0])
                    time.sleep(0.6)
                    drv.execute_script("arguments[0].click();", els[0])
                    time.sleep(5)
                    print(f'点完“{label}”后 URL:', drv.current_url, flush=True)
                    dump_api(drv, f'点{label}后')
                except Exception as exc:
                    print('  点击失败：', exc, flush=True)

        gyms = drv.find_elements(By.XPATH, "//*[normalize-space(text())='健身房']")
        print('页面上“健身房”元素：', len(gyms), flush=True)
        for i, g in enumerate(gyms[:3]):
            try:
                drv.execute_script("arguments[0].click();", g)
                time.sleep(5)
                print(f'点了第 {i+1} 个“健身房”，URL:', drv.current_url, flush=True)
                dump_api(drv, f'点健身房#{i+1}后')
                break
            except Exception as exc:
                print('  失败：', exc, flush=True)

        # 排期请求里应当带 venueId/dateId，直接从 URL 参数里抠出来
        urls = drv.execute_script("return performance.getEntriesByType('resource').map(e=>e.name);") or []
        blob = ' '.join(urls) + ' ' + drv.page_source
        ids = set(re.findall(r'[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}', blob))
        print('\n本页面/请求里出现的所有 GUID（去重）：', len(ids), flush=True)
        for x in sorted(ids):
            print('  ', x, flush=True)
        text = ''
        try:
            text = (drv.find_element(By.TAG_NAME, 'body').text or '')[:600]
        except Exception:
            pass
        print('\n页面文字片段：\n', text.replace(PW, '********'), flush=True)

        # 从 DevTools 性能日志里把前端真正发出的 POST 请求体抠出来（venueId 在 body 里，不在 URL 上）
        import base64 as _b64
        import json as _json
        try:
            entries = drv.get_log('performance')
        except Exception as exc:
            print('性能日志不可用：', exc, flush=True)
            entries = []
        seen = set()
        print(f'\n=== POST/GET 请求明细（共 {len(entries)} 条日志）===', flush=True)
        for e in entries:
            try:
                msg = _json.loads(e['message'])['message']
            except Exception:
                continue
            if msg.get('method') != 'Network.requestWillBeSent':
                continue
            req = msg['params']['request']
            u = req.get('url', '')
            if '/venue/' not in u:
                continue
            pd = req.get('postData', '')
            if pd.startswith('{'):
                try:
                    pd = _json.dumps(_json.loads(pd), ensure_ascii=False)
                except Exception:
                    pass
            key = (u, pd)
            if key in seen:
                continue
            seen.add(key)
            print('  ', req.get('method'), u.split('/venue/')[-1][:90], '| body:', pd[:200], flush=True)
        time.sleep(2)
    finally:
        try:
            drv.quit()
        except Exception:
            pass


if __name__ == '__main__':
    main()
