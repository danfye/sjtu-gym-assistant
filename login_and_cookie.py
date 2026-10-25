import os
import logging
from time import sleep
from datetime import datetime
from selenium import webdriver
from selenium.webdriver.chrome.options import Options as ChromeOptions
from selenium.webdriver.chrome.service import Service as ChromeService
from selenium.webdriver.edge.options import Options as EdgeOptions
from selenium.webdriver.edge.service import Service as EdgeService
from selenium.webdriver.firefox.options import Options as FirefoxOptions
from selenium.webdriver.firefox.service import Service as FirefoxService
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.common.by import By
from selenium.common.exceptions import WebDriverException, StaleElementReferenceException
import requests
import pytesseract
from PIL import Image
from io import BytesIO
from pathlib import Path
from shutil import which

# 与本体同目录；保证从别处 cwd 也能 import 到
import sys as _sys
_sys.path.insert(0, str(Path(__file__).resolve().parent))
import browser_cleanup as BC

SPORTS_URL = 'https://sports.sjtu.edu.cn/pc/'

# 可选：使用远程 ResNet 接口识别验证码（若配置了 URL，则优先使用）
CAPTCHA_SOLVER_URL = os.environ.get('CAPTCHA_SOLVER_URL', '').strip()


def _first_existing_path(candidates):
    for candidate in candidates:
        if not candidate:
            continue
        try:
            path = Path(candidate).expanduser()
            if path.exists():
                return str(path)
        except Exception:
            continue
    return None


def _apply_chromium_options(options, headless: bool, binary_path: str = None):
    if binary_path:
        options.binary_location = binary_path
    if headless:
        options.add_argument('--headless=new')
    options.add_argument('--disable-gpu')
    options.add_argument('--window-size=1280,900')
    options.add_argument('--lang=zh-CN')
    # 站点首页会去拉外部 CDN（这台机器直连外网是不通的，只有校园网可达），
    # 默认的 normal 策略要等所有子资源加载完，于是每次都卡在 "Timed out receiving message
    # from renderer"。eager 只要 DOM 建好就返回，登录表单和同源验证码不受影响。
    options.page_load_strategy = 'eager'
    # 可选：指定持久化 profile 目录，让 jAccount 的“信任此浏览器”在下次自动登录时仍然免二次验证
    user_data_dir = os.environ.get('CHROME_USER_DATA_DIR', '').strip()
    if user_data_dir and Path(user_data_dir).expanduser().is_dir():
        options.add_argument(f'--user-data-dir={Path(user_data_dir).expanduser()}')


def _element_text(el) -> str:
    try:
        return ((el.text or el.get_attribute('textContent') or '')).strip()
    except Exception:
        return ''


def _visible_enabled(el) -> bool:
    try:
        return el.is_displayed() and el.is_enabled()
    except Exception:
        return False


def _find_school_login_button(driver):
    # 新版首页顶部按钮文字为“校内人员登录”；旧版曾在 #logoin 下。
    candidates = []
    for selector in ('#app #logoin button', 'button.el-button--primary', 'button', 'a', '[role="button"]'):
        try:
            candidates.extend(driver.find_elements(By.CSS_SELECTOR, selector))
        except Exception:
            continue

    seen = set()
    unique = []
    for el in candidates:
        try:
            key = el.id
        except Exception:
            key = id(el)
        if key in seen:
            continue
        seen.add(key)
        unique.append(el)

    for el in unique:
        text = _element_text(el)
        if _visible_enabled(el) and ('校内' in text and '登录' in text):
            return el
    for el in unique:
        text = _element_text(el)
        if _visible_enabled(el) and ('登录' in text and '校外' not in text):
            return el
    return None


def _has_visible_login_button(driver) -> bool:
    return _find_school_login_button(driver) is not None


def _encode_captcha_as_jpeg(captcha_bytes: bytes) -> bytes:
    """将验证码图片压缩为较小的 JPEG，避免外部识别接口因 payload 过大返回 413。

    输入通常是 selenium 的 PNG bytes；这里会：
    - 转为 RGB
    - 约束最大尺寸
    - 以较低质量输出 JPEG
    """
    img = Image.open(BytesIO(captcha_bytes))
    # 某些验证码可能带透明通道
    if img.mode not in ('RGB', 'L'):
        img = img.convert('RGB')
    # 控制体积：油猴脚本 canvas 为 110x40，这里尽量对齐该尺寸
    # 先缩小到合理上限，再强制到 110x40（验证码本来就接近这个比例）
    img.thumbnail((260, 120))
    try:
        if img.size != (110, 40):
            img = img.resize((110, 40))
    except Exception:
        pass
    out = BytesIO()
    img.save(out, format='JPEG', quality=70, optimize=True)
    data = out.getvalue()
    # 若仍偏大，再降低质量一次
    if len(data) > 120_000:
        out = BytesIO()
        img.save(out, format='JPEG', quality=55, optimize=True)
        data = out.getvalue()
    return data


def recognize_captcha_resnet(captcha_bytes: bytes) -> str:
    """使用外部 ResNet 接口识别验证码。

    - 通过环境变量 `CAPTCHA_SOLVER_URL` 配置接口地址；留空则不启用。
    - 请求格式：multipart/form-data，字段名 image，文件名 captcha.jpg。
    - 返回 JSON，期望字段 result 为识别结果。
    """
    if not CAPTCHA_SOLVER_URL:
        raise RuntimeError('CAPTCHA_SOLVER_URL 未配置，无法使用 ResNet 识别接口')

    # 对齐油猴脚本：multipart/form-data，字段名 image，文件名 captcha.jpg
    # requests 会自动生成 boundary；不要手动设置 Content-Type。
    form_data = {
        "image": ("captcha.jpg", captcha_bytes, "image/jpeg"),
    }
    headers = {
        # 保持最小化 header，避免服务端对 Referer/Origin 的潜在校验
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Accept": "application/json, text/plain, */*",
    }
    resp = requests.post(CAPTCHA_SOLVER_URL, files=form_data, headers=headers, timeout=10)
    if resp.status_code != 200:
        raise RuntimeError(f"ResNet HTTP {resp.status_code}: {(resp.text or '')[:300]}")
    try:
        data = resp.json()
    except Exception:
        raise RuntimeError(f"ResNet non-JSON response: {(resp.text or '')[:300]}")
    code = (data.get("result") or "").strip()
    if not code:
        raise RuntimeError(f"ResNet 接口未返回有效验证码: {data}")
    return code


def _wait_img_complete(driver, img_element, timeout_s: float = 3.0) -> bool:
    """等待验证码图片加载完成（对齐油猴脚本对 img.complete 的轮询）。"""
    deadline = datetime.now().timestamp() + float(timeout_s)
    while datetime.now().timestamp() < deadline:
        try:
            ok = driver.execute_script(
                "return arguments[0] && arguments[0].complete && arguments[0].naturalWidth > 0;",
                img_element,
            )
            if ok:
                return True
        except Exception:
            pass
        sleep(0.02)
    return False


def _init_driver(headless: bool = True, browser: str = None):
    # 路径按“环境变量 -> PATH -> 常见安装位置 -> 旧 Linux 路径”的顺序探测。
    # Linux 无 GUI 环境下强制非无头时，自动切回无头以避免崩溃。
    browser = (browser or os.environ.get('BROWSER') or 'firefox').strip().lower()
    if not headless and os.name != 'nt' and 'DISPLAY' not in os.environ:
        logging.warning('未检测到 DISPLAY，自动切换为无头模式 (headless=true)')
        headless = True

    base_dir = Path(__file__).resolve().parent
    logs_dir = base_dir / 'logs'
    logs_dir.mkdir(parents=True, exist_ok=True)
    log_file = logs_dir / f'{browser}_driver_{datetime.now().strftime("%Y%m%d_%H%M%S")}.log'

    if browser in ('chrome', 'google-chrome', 'chromium'):
        options = ChromeOptions()
        chrome_bin = _first_existing_path([
            os.environ.get('CHROME_BINARY'),
            os.environ.get('CHROME_PATH'),
            which('chrome'),
            which('chrome.exe'),
            which('google-chrome'),
            which('chromium'),
            which('chromium-browser'),
            r'C:\Program Files\Google\Chrome\Application\chrome.exe',
            r'C:\Program Files (x86)\Google\Chrome\Application\chrome.exe',
            '/usr/bin/google-chrome',
            '/usr/bin/chromium-browser',
        ])
        chromedriver = _first_existing_path([
            os.environ.get('CHROMEDRIVER_PATH'),
            os.environ.get('CHROME_DRIVER_PATH'),
            which('chromedriver'),
            which('chromedriver.exe'),
            r'C:\Tools\chromedriver.exe',
            r'D:\Tools\chromedriver.exe',
        ])
        _apply_chromium_options(options, headless, chrome_bin)
        service = ChromeService(executable_path=chromedriver, log_output=str(log_file)) if chromedriver else ChromeService(log_output=str(log_file))
        # 上一次被硬杀留下的孤儿会占着 profile、也会把 macOS 关机卡住，起新进程前先收干净
        BC.sweep_stale()
        try:
            drv = webdriver.Chrome(options=options, service=service)
        except WebDriverException as e:
            BC.kill_service(service)
            raise RuntimeError(
                f"Chrome/ChromeDriver 启动失败。chrome={chrome_bin or '自动探测'}, chromedriver={chromedriver or 'Selenium Manager'}。\n"
                "Windows 下可安装 Chrome，并通过 CHROME_BINARY / CHROMEDRIVER_PATH 指定 chrome.exe 与 chromedriver.exe。"
            ) from e
        return BC.register(drv)

    if browser in ('edge', 'msedge', 'microsoft-edge'):
        options = EdgeOptions()
        edge_bin = _first_existing_path([
            os.environ.get('EDGE_BINARY'),
            os.environ.get('EDGE_PATH'),
            os.environ.get('MSEDGE_BINARY'),
            os.environ.get('MSEDGE_PATH'),
            which('msedge'),
            which('msedge.exe'),
            r'C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe',
            r'C:\Program Files\Microsoft\Edge\Application\msedge.exe',
        ])
        edgedriver = _first_existing_path([
            os.environ.get('EDGEDRIVER_PATH'),
            os.environ.get('MSEDGEDRIVER_PATH'),
            which('msedgedriver'),
            which('msedgedriver.exe'),
            r'C:\Tools\msedgedriver.exe',
            r'D:\Tools\msedgedriver.exe',
        ])
        _apply_chromium_options(options, headless, edge_bin)
        service = EdgeService(executable_path=edgedriver, log_output=str(log_file)) if edgedriver else EdgeService(log_output=str(log_file))
        BC.sweep_stale()
        try:
            drv = webdriver.Edge(options=options, service=service)
        except WebDriverException as e:
            BC.kill_service(service)
            raise RuntimeError(
                f"Edge/EdgeDriver 启动失败。edge={edge_bin or '自动探测'}, edgedriver={edgedriver or 'Selenium Manager'}。\n"
                "Windows 下可通过 EDGE_BINARY / EDGEDRIVER_PATH 指定 msedge.exe 与 msedgedriver.exe。"
            ) from e
        return BC.register(drv)

    if browser not in ('firefox', 'ff'):
        logging.warning("未知 browser=%s，回退到 Firefox", browser)

    options = FirefoxOptions()
    if headless:
        options.headless = True
        # 某些环境更依赖该环境变量来启用真正无头
        os.environ.setdefault('MOZ_HEADLESS', '1')

    firefox_bin = _first_existing_path([
        os.environ.get('FIREFOX_BINARY'),
        os.environ.get('FIREFOX_PATH'),
        which('firefox'),
        which('firefox.exe'),
        r'C:\Program Files\Mozilla Firefox\firefox.exe',
        r'C:\Program Files (x86)\Mozilla Firefox\firefox.exe',
        '/home/yxc/miniconda3/envs/sjtu/bin/firefox',
    ])
    gecko_path = _first_existing_path([
        os.environ.get('GECKODRIVER_PATH'),
        os.environ.get('GECKODRIVER_BINARY'),
        which('geckodriver'),
        which('geckodriver.exe'),
        r'C:\Tools\geckodriver.exe',
        r'D:\Tools\geckodriver.exe',
        '/home/yxc/miniconda3/envs/sjtu/bin/geckodriver',
    ])

    if firefox_bin:
        options.binary_location = firefox_bin
    # geckodriver 日志输出到项目 logs 目录，便于诊断
    # geckodriver 没找到时交给 Selenium Manager 自动解析/下载。
    service = FirefoxService(executable_path=gecko_path, log_output=str(log_file)) if gecko_path else FirefoxService(log_output=str(log_file))
    try:
        return webdriver.Firefox(options=options, service=service)
    except WebDriverException as e:
        raise RuntimeError(
            f"Firefox/GeckoDriver 启动失败。firefox={firefox_bin or '自动探测'}, geckodriver={gecko_path or 'Selenium Manager'}。\n"
            "Windows 下可安装 Firefox，并通过 FIREFOX_BINARY / GECKODRIVER_PATH 指定 firefox.exe 与 geckodriver.exe。"
        ) from e


def _solve_captcha_png(img_bytes: bytes) -> str:
    """优先尝试 ResNet 接口，失败则回退到本地 tesseract。"""
    # 1) 优先远程 ResNet
    if CAPTCHA_SOLVER_URL:
        try:
            # 将 PNG 截图压缩为 JPEG，避免 413 REQUEST ENTITY TOO LARGE
            jpg_bytes = _encode_captcha_as_jpeg(img_bytes)
            return recognize_captcha_resnet(jpg_bytes)
        except Exception as e:
            logging.warning('ResNet 验证码识别失败，将回退到 tesseract：%s', e)

    # 2) 本地 tesseract 兜底
    img = Image.open(BytesIO(img_bytes))
    text = pytesseract.image_to_string(
        img,
        config='--psm 7 -c tessedit_char_whitelist=abcdefghijklmnopqrstuvwxyz'
    )
    text = ''.join(ch for ch in text.lower() if ch.isalpha())
    return text


def login_and_get_cookie(username: str, password: str, headless: bool = True, browser: str = None) -> str:
    if os.environ.get('HEADLESS'):
        headless = os.environ.get('HEADLESS') == '1'
    # 配置 tesseract 路径，避免 cron 下 PATH 丢失
    tesseract_bin = _first_existing_path([
        os.environ.get('TESSERACT_CMD'),
        os.environ.get('TESSERACT_PATH'),
        which('tesseract'),
        which('tesseract.exe'),
        r'C:\Program Files\Tesseract-OCR\tesseract.exe',
        r'C:\Program Files (x86)\Tesseract-OCR\tesseract.exe',
        '/home/yxc/miniconda3/envs/sjtu/bin/tesseract',
    ])
    if tesseract_bin:
        try:
            import pytesseract as _pt
            _pt.pytesseract.tesseract_cmd = tesseract_bin
        except Exception:
            pass
    # 配置 TESSDATA_PREFIX 指向 tessdata 目录（conda 环境常见路径）
    tessdata_dir = _first_existing_path([
        os.environ.get('TESSDATA_PREFIX'),
        r'C:\Program Files\Tesseract-OCR\tessdata',
        r'C:\Program Files (x86)\Tesseract-OCR\tessdata',
        '/home/yxc/miniconda3/envs/sjtu/share/tessdata',
    ])
    if tessdata_dir and os.path.isdir(tessdata_dir):
        os.environ.setdefault('TESSDATA_PREFIX', tessdata_dir)
    elif not tesseract_bin:
        logging.warning('未在 PATH 找到 tesseract，可安装到 sjtu 环境或将其加入 PATH；否则验证码 OCR 可能失败')
    drv = _init_driver(headless, browser)
    # jaccount / sports 首页在高峰时会更慢，略放宽避免误超时导致多次重试
    drv.set_page_load_timeout(35)
    drv.get(SPORTS_URL)
    wait = WebDriverWait(drv, 15)
    # 如果首页没有可见登录按钮，通常说明已经登录，直接取 cookie。
    try:
        wait.until(EC.presence_of_element_located((By.CSS_SELECTOR, '#app')))
        try:
            WebDriverWait(drv, 6).until(lambda d: _has_visible_login_button(d))
        except Exception:
            pass
        if not _has_visible_login_button(drv):
            cookies = drv.get_cookies()
            cookie_header = '; '.join([f"{c['name']}={c['value']}" for c in cookies])
            return cookie_header  # 交给外层 finally 的 BC.close()，这里不用自己 quit
    except Exception:
        pass

    # 点击首页“校内人员登录”按钮，进入 jAccount。
    try:
        btn = wait.until(lambda d: _find_school_login_button(d))
        drv.execute_script('arguments[0].click();', btn)
        wait.until(lambda d: 'jaccount.sjtu.edu.cn' in d.current_url or d.find_elements(By.NAME, 'user'))
    except Exception as e:
        logging.warning('未能点击校内登录按钮或进入 jAccount：url=%s title=%s err=%s', getattr(drv, 'current_url', ''), getattr(drv, 'title', ''), e)

    # 登录表单循环中每次重新定位，避免 stale
    for attempt in range(1, 8):
        try:
            drv.switch_to.default_content()
            user = wait.until(EC.visibility_of_element_located((By.NAME, 'user')))
            pwd = wait.until(EC.visibility_of_element_located((By.NAME, 'pass')))
            cap_img = wait.until(EC.visibility_of_element_located((By.ID, 'captcha-img')))
            cap_input = wait.until(EC.visibility_of_element_located((By.ID, 'input-login-captcha')))
            submit = wait.until(EC.element_to_be_clickable((By.ID, 'submit-password-button')))

            # 清空并输入
            try:
                user.clear(); pwd.clear(); cap_input.clear()
            except StaleElementReferenceException:
                # 元素刚刷新，下一轮重试
                logging.info("login inputs stale on clear, retrying (attempt %s)", attempt)
                continue

            user.send_keys(username)
            pwd.send_keys(password)

            if attempt > 1:
                try:
                    cap_img.click(); sleep(0.2)
                    # 重新获取验证码元素，避免刷新后的 stale
                    cap_img = wait.until(EC.visibility_of_element_located((By.ID, 'captcha-img')))
                except Exception:
                    pass

            # 等待验证码图片真正加载完成，避免截到空白/占位图
            try:
                _wait_img_complete(drv, cap_img, timeout_s=3.0)
            except Exception:
                pass

            # 识别前给一点点缓冲即可（主要靠 complete 检测保证截图有效）
            sleep(0.15)
            code = _solve_captcha_png(cap_img.screenshot_as_png)
            if not code or len(code) < 3:
                continue
            cap_input.send_keys(code)
            submit.click()
            sleep(0.8)
            # 登录判定：jAccount 成功后会跳回 sports 域名。
            try:
                WebDriverWait(drv, 8).until(lambda d: 'sports.sjtu.edu.cn' in d.current_url and d.find_elements(By.CSS_SELECTOR, '#app'))
                break
            except Exception:
                # 未跳转表示验证码/账号密码可能没过，下一轮刷新验证码重试。
                logging.info("login attempt %s did not return to sports, retrying", attempt)
                continue
        except StaleElementReferenceException:
            logging.info("stale elements encountered, retrying (attempt %s)", attempt)
            continue
        except Exception as e:
            logging.info("login attempt %s error: %s", attempt, e)
            continue

    # 抓 cookie（以 requests 可用的 Cookie 头格式返回）
    try:
        if 'sports.sjtu.edu.cn' not in drv.current_url:
            logging.warning('登录流程结束但未回到 sports 域名：url=%s title=%s', drv.current_url, drv.title)
        cookies = drv.get_cookies()
        cookie_header = '; '.join([f"{c['name']}={c['value']}" for c in cookies])
        return cookie_header
    finally:
        BC.close(drv)
