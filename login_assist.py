"""半自动登录：脚本走到验证码这一步，把验证码图片落到磁盘上，由外部（人或多模态模型）读码回填。

解决两个反复出现的老问题：① tesseract 会把 4 位验证码读成 3 位，自动重试越试越偏；
② jAccount 登录表单在单页应用里（可能在 iframe 内），按顶层文档等元素必然超时。

流程：开 Chrome（有窗口、eager 加载、持久化 profile）→ 点「校内人员登录」→ 找到表单 →
每轮把验证码原图写 /tmp/gym_captcha.png，然后等 /tmp/gym_cap_answer.txt（内容就是验证码，
写一次用一次，用完删除）；没有回填文件时退回 tesseract。提交后如果出现「二次验证」就停下来
等你扫码；只要接口能查到 dateId 就算成功，cookie 写入 state/cookie.json。

用法：
    JACCOUNT_USERNAME=... JACCOUNT_PASSWORD=... .venv/bin/python login_assist.py
"""
import os
import sys
import time
from pathlib import Path

import requests

base = Path(__file__).resolve().parent
sys.path.insert(0, str(base))

from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By

import browser_cleanup as BC
import login_and_cookie as L
import scan_notify as SN
from cookie_store import save_cookie

if __name__ != '__main__':
    raise RuntimeError('login_assist.py 请直接运行，不要 import')

PROFILE = base / 'chrome-profile'
PROFILE.mkdir(exist_ok=True)
UN = os.environ.get('JACCOUNT_USERNAME', '')
PW = os.environ.get('JACCOUNT_PASSWORD', '')
CAP_PNG = Path('/tmp/gym_captcha.png')
CAP_ANS = Path('/tmp/gym_cap_answer.txt')
STATE = Path('/tmp/gym_login_state.txt')
MAX_ATTEMPTS = int(os.environ.get('LOGIN_ATTEMPTS', '8'))
ANSWER_WAIT_S = int(os.environ.get('ANSWER_WAIT_S', '120'))

USER_SELECTORS = [(By.NAME, 'user'), (By.ID, 'input-login-user'), (By.CSS_SELECTOR, "input[name='user']")]
PASS_SELECTORS = [(By.NAME, 'pass'), (By.ID, 'input-login-pass')]
CAP_IMG_SEL = [(By.ID, 'captcha-img'), (By.CSS_SELECTOR, 'img[src*="captcha"]')]
CAP_IN_SEL = [(By.ID, 'input-login-captcha'), (By.NAME, 'captcha')]
SUB_SEL = [(By.ID, 'submit-password-button'), (By.CSS_SELECTOR, "button[type='submit']")]


def find_first(drv, selectors):
    for by, val in selectors:
        try:
            els = [e for e in drv.find_elements(by, val) if e.is_displayed()]
            if els:
                return els[0]
        except Exception:
            continue
    return None


def switch_to_form_frame(drv):
    """表单可能在 iframe 里；先确认顶层，再逐个 frame 进去。"""
    if find_first(drv, USER_SELECTORS):
        drv.switch_to.default_content()
        return 'top'
    for idx, fr in enumerate(drv.find_elements(By.TAG_NAME, 'iframe')):
        try:
            drv.switch_to.default_content()
            drv.switch_to.frame(fr)
            if find_first(drv, USER_SELECTORS):
                return f'iframe[{idx}]'
        except Exception:
            continue
    drv.switch_to.default_content()
    return None


def write_state(text):
    STATE.write_text(text, encoding='utf-8')
    print(text, flush=True)


def session_ready(drv):
    """profile 里若还留着 jAccount 的可信会话，点登录后会静默 SSO 直接回到已登录态，
    这时表单根本不出现 —— 所以判据只看真实接口能不能查到 dateId（2026-10-24 凌晨实测）。"""
    header = '; '.join(f"{c['name']}={c['value']}" for c in drv.get_cookies())
    if 'JSESSIONID' not in header:
        return None
    probe_date = time.strftime('%Y-%m-%d', time.localtime(time.time() + 86400))
    try:
        ids, _ = SN.is_full_state(header, probe_date)
    except Exception:
        return None
    return header if ids else None


opts = Options()
opts.binary_location = os.environ.get('CHROME_BINARY', '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome')
opts.add_argument(f'--user-data-dir={PROFILE}')
opts.add_argument('--window-size=1280,900')
opts.add_argument('--lang=zh-CN')
opts.page_load_strategy = os.environ.get('PAGE_LOAD_STRATEGY', 'eager')
opts.add_experimental_option('excludeSwitches', ['enable-automation'])
opts.add_experimental_option('useAutomationExtension', False)
opts.add_argument('--disable-blink-features=AutomationControlled')
def pick_proxy_mode():
    """校园网直连可用时让 Chrome 走直连：本机代理（Clash）抖一下就会把整轮登录变成
    WebDriverException Bad Gateway / 白屏，而 sports 和 jAccount 都是校园内网可达。
    2026-10-27 17:51 那次「一小时内 3 次拿不到会话」就是这么来的。"""
    try:
        st = requests.Session()
        st.trust_env = False
        code = st.get('https://sports.sjtu.edu.cn/', timeout=8, allow_redirects=False).status_code
        if code in (200, 301, 302, 400, 401, 403):
            return 'direct://'
    except Exception:
        pass
    return ''


_px = os.environ.get('CHROME_PROXY', '').strip() or pick_proxy_mode()
_hr = os.environ.get('CHROME_HOST_RESOLVER', '').strip()
if _px:
    opts.add_argument(f'--proxy-server={_px}')
if _hr:
    opts.add_argument(f'--host-resolver-rules={_hr}')

CAP_ANS.unlink(missing_ok=True)
drv = BC.register(webdriver.Chrome(options=opts))
BC.install_handlers(include_atexit=False)
drv.set_page_load_timeout(60)
drv.implicitly_wait(0)
print('打开登录页...', flush=True)
drv.get(os.environ.get('LOGIN_URL', 'https://sports.sjtu.edu.cn/pc/#/login'))
time.sleep(4)
# /pc/#/login 只渲染两个 tab，凭证表单要点开「校内人员登录」那个 tab 才注入
for _ in range(10):
    if find_first(drv, USER_SELECTORS):
        break
    try:
        drv.execute_script("""
          for (const e of document.querySelectorAll('*')) {
            const t=[...e.childNodes].filter(n=>n.nodeType===3).map(n=>n.textContent.trim()).join('');
            if (t==='校内人员登录' || t==='登录') { e.click(); return true; }
          }
          return false;
        """)
    except Exception:
        pass
    time.sleep(3)
for _ in range(10):
    if L._has_visible_login_button(drv):
        break
    time.sleep(2)
btn = find_first(drv, [(By.XPATH, "//*[contains(.,'校内人员登录')]")])
if btn:
    try:
        drv.execute_script('arguments[0].click();', btn)
        print('已点「校内人员登录」', flush=True)
    except Exception as exc:
        write_state(f'click_failed {type(exc).__name__}')
else:
    write_state('no_school_login_button')
time.sleep(5)
# 先给静默 SSO 一次机会：能直接拿到可用会话就不用填任何东西
for _ in range(12):
    hdr = session_ready(drv)
    if hdr:
        path = save_cookie(hdr)
        write_state(f'OK 静默 SSO，已写入 {path}')
        BC.close(drv)
        sys.exit(0)
    time.sleep(5)
write_state('没有静默 SSO，开始找登录表单')

where = None
for _ in range(20):
    where = switch_to_form_frame(drv)
    if where:
        break
    time.sleep(2)
write_state(f'form_location={where} url={drv.current_url[:90]}')
if not where:
    print('找不到登录表单，页面文本前 300 字：', flush=True)
    try:
        print((drv.find_element(By.TAG_NAME, 'body').text or '')[:300], flush=True)
    except Exception:
        pass

for attempt in range(1, MAX_ATTEMPTS + 1):
    user, pwd = find_first(drv, USER_SELECTORS), find_first(drv, PASS_SELECTORS)
    cap_img, cap_in, sub = find_first(drv, CAP_IMG_SEL), find_first(drv, CAP_IN_SEL), find_first(drv, SUB_SEL)
    if not (user and pwd and cap_in and sub):
        body = ''
        try:
            body = drv.find_element(By.TAG_NAME, 'body').text or ''
        except Exception:
            pass
        if '二次验证' in body or '完成验证' in body:
            # 这两个词 jAccount 在图形验证码页也可能出现，所以把现场写清楚：
            # 到底是二次验证，还是只需要人手工认一次验证码。
            write_state(f'WAIT_HUMAN 需要你在 Chrome 窗口里操作一次 | url={drv.current_url[:90]} | 页面: {body[:120]}')
            deadline = time.time() + int(os.environ.get('WAIT_2FA_SECONDS', '900'))
            while time.time() < deadline:
                time.sleep(3)
                if SN.is_full_state('; '.join(f"{c['name']}={c['value']}" for c in drv.get_cookies()),
                                      time.strftime('%Y-%m-%d', time.localtime(time.time() + 86400)))[0]:
                    path = save_cookie('; '.join(f"{c['name']}={c['value']}" for c in drv.get_cookies()))
                    write_state(f'OK saved={path}')
                    BC.close(drv)
                    sys.exit(0)
            write_state('TIMEOUT_2FA')
            sys.exit(3)
        write_state(f'no_form attempt={attempt} need_user={bool(user)} need_pass={bool(pwd)} '
                    f'need_capin={bool(cap_in)} need_sub={bool(sub)}')
        time.sleep(3)
        continue

    code = ''
    if cap_img:
        try:
            L._wait_img_complete(drv, cap_img, timeout_s=6.0)
            CAP_PNG.write_bytes(cap_img.screenshot_as_png)
        except Exception:
            CAP_ANS.unlink(missing_ok=True)
    if CAP_ANS.exists():
        try:
            code = CAP_ANS.read_text(encoding='utf-8').strip()[:8]
            CAP_ANS.unlink(missing_ok=True)
            print(f'[{attempt}] 用外部回填的验证码 {code!r}', flush=True)
        except Exception:
            code = ''
    if not code and cap_img:
        try:
            code = L._solve_captcha_png(CAP_PNG.read_bytes())
            print(f'[{attempt}] tesseract 识别为 {code!r}（无人回填，先试这个）', flush=True)
        except Exception as exc:
            print(f'[{attempt}] OCR 失败 {type(exc).__name__}', flush=True)
    if not code:
        write_state(f'NEED_CAPTCHA attempt={attempt} png={CAP_PNG}')
        deadline = time.time() + ANSWER_WAIT_S
        while time.time() < deadline:
            if CAP_ANS.exists():
                break
            time.sleep(2)
        continue

    user.clear(); pwd.clear(); cap_in.clear()
    user.send_keys(UN)
    pwd.send_keys(PW)
    cap_in.send_keys(code)
    try:
        drv.execute_script('arguments[0].click();', sub)
    except Exception:
        sub.click()
    write_state(f'SUBMITTED attempt={attempt} code={code}')
    time.sleep(4)
    probe_date = time.strftime('%Y-%m-%d', time.localtime(time.time() + 86400))
    for _ in range(8):
        header = '; '.join(f"{c['name']}={c['value']}" for c in drv.get_cookies())
        ids, msg = SN.is_full_state(header, probe_date)
        if ids:
            path = save_cookie(header)
            write_state(f'OK dates={len(ids)} saved={path}')
            BC.close(drv)
            sys.exit(0)
        if msg and '未登录' not in msg and '登录超时' not in msg:
            write_state(f'submit_reply={msg[:70]}')
            break
        time.sleep(2)

write_state('GIVE_UP')
sys.exit(4)
