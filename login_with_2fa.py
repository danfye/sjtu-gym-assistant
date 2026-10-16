"""人工辅助登录：脚本走到 jAccount 二次验证页为止，验证码/扫码由你本人在弹出的 Chrome 里完成。
成功后把 sports 域名的 cookie 存进 state/cookie.json，后续 check/book 直接复用，不再触发 2FA。
密码只从环境变量读取，不打印、不落盘。
"""
import os
import sys
import time
from pathlib import Path

base = Path(__file__).resolve().parent
sys.path.insert(0, str(base))

from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC

import login_and_cookie as L
import browser_cleanup as BC
from cookie_store import save_cookie

PROFILE_DIR = base / 'chrome-profile'
PROFILE_DIR.mkdir(exist_ok=True)
PW = os.environ.get('JACCOUNT_PASSWORD', '')
UN = os.environ.get('JACCOUNT_USERNAME', '')
WAIT_SECONDS = int(os.environ.get('WAIT_2FA_SECONDS', '600'))

opts = Options()
opts.binary_location = os.environ.get('CHROME_BINARY', '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome')
opts.add_argument(f'--user-data-dir={PROFILE_DIR}')   # 持久化 profile：jAccount 勾了“信任此浏览器”后下次免 2FA
opts.add_argument('--window-size=1280,900')
opts.add_argument('--lang=zh-CN')
opts.add_experimental_option('excludeSwitches', ['enable-automation'])

drv = BC.register(webdriver.Chrome(options=opts))
# Ctrl-C / kill 时收掉浏览器；正常超时退出仍按原设计「Chrome 保持打开」，所以不注册 atexit
BC.install_handlers(include_atexit=False)
drv.set_page_load_timeout(90)
print('Chrome 已打开（持久化 profile: chrome-profile）', flush=True)
drv.get(L.SPORTS_URL)
wait = WebDriverWait(drv, 45)
wait.until(EC.presence_of_element_located((By.CSS_SELECTOR, '#app')))
btn = wait.until(lambda d: L._find_school_login_button(d))
drv.execute_script('arguments[0].click();', btn)
wait.until(lambda d: 'jaccount.sjtu.edu.cn' in d.current_url or d.find_elements(By.NAME, 'user'))
print('已进入 jAccount 登录页，正在填账号密码并识别验证码...', flush=True)

for attempt in range(1, 5):
    user = wait.until(EC.visibility_of_element_located((By.NAME, 'user')))
    pwd = wait.until(EC.visibility_of_element_located((By.NAME, 'pass')))
    cap_img = wait.until(EC.visibility_of_element_located((By.ID, 'captcha-img')))
    cap_input = wait.until(EC.visibility_of_element_located((By.ID, 'input-login-captcha')))
    submit = wait.until(EC.element_to_be_clickable((By.ID, 'submit-password-button')))
    user.clear(); pwd.clear(); cap_input.clear()
    user.send_keys(UN)
    pwd.send_keys(PW)
    L._wait_img_complete(drv, cap_img, timeout_s=5.0)
    code = L._solve_captcha_png(cap_img.screenshot_as_png)
    print(f'[{attempt}] 验证码识别为 {code!r}，提交中...', flush=True)
    cap_input.send_keys(code)
    submit.click()
    time.sleep(2.5)

    # 进入二次验证 -> 交给人
    body = ''
    try:
        body = drv.find_element(By.TAG_NAME, 'body').text or ''
    except Exception:
        pass
    if '二次验证' in body or '完成验证' in body:
        print('\n>>> 需要你本人操作：Chrome 窗口里现在是「二次验证」页。', flush=True)
        print('>>> 建议勾选「信任此浏览器/应用，下次不再验证」，然后扫码或填验证码。', flush=True)
        try:
            for cb in drv.find_elements(By.CSS_SELECTOR, 'input[type=checkbox]'):
                if not cb.is_selected():
                    cb.click()
                    print('>>> 已尝试自动勾选「信任此浏览器」', flush=True)
        except Exception:
            pass
        break
    try:
        WebDriverWait(drv, 10).until(lambda d: 'sports.sjtu.edu.cn' in d.current_url)
        print('免 2FA 直接登录成功', flush=True)
        break
    except Exception:
        print(f'[{attempt}] 未通过，换一张验证码重试', flush=True)
        continue

deadline = time.time() + WAIT_SECONDS
while time.time() < deadline:
    url = drv.current_url or ''
    if 'sports.sjtu.edu.cn' in url:
        time.sleep(2.0)  # 等前端把会话种稳
        cookies = drv.get_cookies()
        header = '; '.join(f"{c['name']}={c['value']}" for c in cookies)
        path = save_cookie(header)
        print(f'登录完成，共 {len(cookies)} 个 cookie，已写入 {path}', flush=True)
        BC.close(drv)
        sys.exit(0)
    time.sleep(3)
    try:
        print('等待你完成二次验证... 当前: %s' % (drv.current_url or '')[:80], flush=True)
    except Exception:
        break

print('超时未完成，Chrome 保持打开，可手动继续；完成后重跑本脚本即可保存 cookie', flush=True)
