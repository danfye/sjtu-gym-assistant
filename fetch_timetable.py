"""登录 i.sjtu.edu.cn（正方教务），找出「课表」菜单码，抓取课表页面 HTML 并解析成结构化 JSON。

只读操作：只查课表，不点任何申请/提交类按钮。
凭据从环境变量 JACCOUNT_USERNAME / JACCOUNT_PASSWORD 读取，不打印、不落盘。

产物：
  /tmp/timetable_menu.txt   发现的课表相关菜单码与 URL
  /tmp/timetable_raw.html   课表页面原始 HTML（供解析核对）
  schedule.json（本目录）   结构化课表：{星期: [[起,止,课程名], ...]}
"""
import json
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

import browser_cleanup as BC
import login_and_cookie as L
from strict_login import build_options

PW = os.environ.get('JACCOUNT_PASSWORD', '')
UN = os.environ.get('JACCOUNT_USERNAME', '')
HOME = 'https://i.sjtu.edu.cn/xtgl/index.html'
MENU = 'https://i.sjtu.edu.cn/xtgl/index_initMenu.html?gnmkdm=N101515&layout=default'
COOKIE_OUT = BASE / 'i_sjtu_cookies.json'


def dump_cookies(drv):
    ck = {c['name']: c['value'] for c in drv.get_cookies()}
    COOKIE_OUT.write_text(json.dumps(ck, ensure_ascii=False, indent=2), encoding='utf-8')
    return ck


def main():
    BC.install_handlers()
    drv = webdriver.Chrome(options=build_options(headless=False, anti_detect=True))
    drv.set_page_load_timeout(90)
    try:
        print('打开 i.sjtu 登录入口…', flush=True)
        drv.get('https://i.sjtu.edu.cn/jaccountlogin')
        time.sleep(4)
        cur = drv.current_url or ''
        print('当前 URL:', cur[:120], flush=True)

        if 'i.sjtu.edu.cn/xtgl' in cur and 'login' not in cur:
            print('已是登录状态，直接读菜单', flush=True)
        else:
            if drv.find_elements(By.ID, 'input-login-user'):
                user = drv.find_element(By.ID, 'input-login-user')
                pwd = drv.find_element(By.ID, 'input-login-pass')
                cap_in = drv.find_element(By.ID, 'input-login-captcha')
                cap_img = drv.find_element(By.ID, 'captcha-img')
                submit = drv.find_element(By.ID, 'submit-password-button')
                for attempt in range(1, 7):
                    try:
                        user.clear(); pwd.clear(); cap_in.clear()
                    except Exception:
                        time.sleep(1); continue
                    user.send_keys(UN); pwd.send_keys(PW)
                    try:
                        L._wait_img_complete(drv, cap_img, timeout_s=6.0)
                    except Exception:
                        pass
                    code = L._solve_captcha_png(cap_img.screenshot_as_png)
                    cap_in.send_keys(code)
                    print(f'[{attempt}] 验证码识别 {code!r}，提交', flush=True)
                    submit.click()
                    time.sleep(3.5)
                    if 'i.sjtu.edu.cn' in (drv.current_url or '') and 'login' not in (drv.current_url or '').lower():
                        print('   登录成功 ->', drv.current_url[:110], flush=True)
                        break
                    print('   未通过，重试', flush=True)
                    try:
                        cap_img.click(); time.sleep(1.2)
                        cap_img = drv.find_element(By.ID, 'captcha-img')
                    except Exception:
                        drv.refresh(); time.sleep(2)
                        user = drv.find_element(By.ID, 'input-login-user')
                        pwd = drv.find_element(By.ID, 'input-login-pass')
                        cap_in = drv.find_element(By.ID, 'input-login-captcha')
                        cap_img = drv.find_element(By.ID, 'captcha-img')
                        submit = drv.find_element(By.ID, 'submit-password-button')
                else:
                    print('登录失败（验证码或账号问题）', flush=True)
                    return

        if not drv.find_elements(By.TAG_NAME, 'body'):
            drv.get(HOME)
        time.sleep(2)
        print('登录后 URL:', drv.current_url[:120], flush=True)
        dump_cookies(drv)

        # 菜单页：找课表相关菜单码
        drv.get(MENU)
        time.sleep(3)
        html = drv.page_source
        hits = re.findall(r"""clickMenu\(\s*'([^']+)'\s*,\s*'([^']+)'\s*,\s*'([^']*)'""", html)
        kb = [h for h in hits if ('课表' in h[2] or 'xskb' in h[1] or 'kcb' in h[1])]
        Path('/tmp/timetable_menu.txt').write_text(
            '\n'.join(f'{c}\t{u}\t{t}' for c, u, t in hits) + '\n--- 课表相关 ---\n' +
            '\n'.join(f'{c}\t{u}\t{t}' for c, u, t in kb), encoding='utf-8')
        print(f'菜单总数 {len(hits)}，课表相关 {len(kb)}:', kb[:6], flush=True)
        if not kb:
            print('没找到课表菜单，已把全部菜单写到 /tmp/timetable_menu.txt', flush=True)
            return

        code, path, name = kb[0]
        url = path if path.startswith('http') else 'https://i.sjtu.edu.cn' + path
        sep = '&' if '?' in url else '?'
        full = f'{url}{sep}gnmkdm={code}&layout=default'
        print(f'进入课表页：{name} -> {full}', flush=True)
        drv.get(full)
        time.sleep(6)
        raw = drv.page_source
        Path('/tmp/timetable_raw.html').write_text(raw, encoding='utf-8')
        print('课表页长度', len(raw), '| 含"课表":', raw.count('课表'), '| 含"星期":', raw.count('星期'), flush=True)
        # 页面文本粗览，确认是否已渲染出表格
        try:
            txt = drv.find_element(By.TAG_NAME, 'body').text or ''
        except Exception:
            txt = ''
        print('页面文字前 400：\n', txt[:400].replace(PW, '********'), flush=True)
        Path('/tmp/timetable_text.txt').write_text(txt, encoding='utf-8')
    finally:
        BC.close(drv)


if __name__ == '__main__':
    main()
