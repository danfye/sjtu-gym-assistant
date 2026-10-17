"""最小网络诊断：逐步打印 Chrome 实际看到了什么，并截图。"""
import os
import sys
import time
from pathlib import Path

BASE = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE))
from selenium import webdriver
from strict_login import build_options
import login_and_cookie as L

drv = webdriver.Chrome(options=build_options(os.environ.get('DBG_HEADLESS', '0') == '1', True))
drv.set_page_load_timeout(120)
try:
    print('1) 打开 sports 首页…', flush=True)
    t = time.time()
    drv.get(L.SPORTS_URL)
    print(f'   用时 {time.time()-t:.1f}s | url={drv.current_url} | title={drv.title}', flush=True)
    for sel in ('#app', 'body'):
        els = drv.find_elements('css selector', sel)
        print(f'   {sel} 元素数={len(els)}', flush=True)
    print('   body 文字前 200：', (drv.find_element('tag name', 'body').text or '')[:200].replace('\n', ' | '), flush=True)
    drv.save_screenshot('/tmp/gym_net_1_home.png')

    print('2) 找校内人员登录按钮…', flush=True)
    time.sleep(2)
    btn = L._find_school_login_button(drv)
    print('   按钮对象：', type(btn).__name__ if btn else None, flush=True)
    if btn:
        print('   按钮文字：', (btn.text or '')[:40], flush=True)
        drv.execute_script('arguments[0].click();', btn)
        for i in range(12):
            time.sleep(5)
            u = drv.current_url
            print(f'   +{(i+1)*5}s url={u[:110]}', flush=True)
            if 'jaccount' in u:
                break
        drv.save_screenshot('/tmp/gym_net_2_after_click.png')
        print('   页面文字前 200：', (drv.find_element('tag name', 'body').text or '')[:200].replace('\n', ' | '), flush=True)
        print('   user 输入框数：', len(drv.find_elements('name', 'user')), flush=True)
    print('截图：/tmp/gym_net_1_home.png /tmp/gym_net_2_after_click.png', flush=True)
finally:
    try:
        drv.quit()
    except Exception:
        pass
