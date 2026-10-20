"""浏览器进程兜底清理：drv.quit() 卡死、父进程被杀、Ctrl-C 时不留孤儿 chromedriver/Chrome。

为什么需要它：Selenium 的 `driver.quit()` 只在「Python 正常走到 finally」时才可靠。实测两种失败：
① Python 父进程先退出（被 kill / 终端关闭），chromedriver 和 `--headless=new` 的 Chrome 变成 PPID=1
   的孤儿继续跑；它仍向 LaunchServices 注册 com.google.Chrome，却没有窗口也不响应 quit Apple
   Event，「强制退出」面板杀不到，最后把 macOS 关机卡住。
② quit() 自己卡在已 wedged 的浏览器上，把整轮流程挂住。

这里的策略：quit() 限时等待，超时直接按进程树 SIGKILL；进程退出/收到信号时再兜一遍；启动新
浏览器前先 sweep 一次，把上一次被 SIGKILL（那种情况没人能跑 handler）留下的同类孤儿清掉。

只认「本项目的」浏览器，判据有两条，不会误杀别的自动化 Chrome：
- cmdline 里带本目录路径（chromedriver 的 --log-path 一定指向 logs/，持久化 profile 也在本目录）；
- 或者它的标准输出/错误句柄开着本目录下的日志文件（Chrome 会继承 driver 重定向的 fd）。
"""
import atexit
import os
import signal
import subprocess
import threading
from pathlib import Path

PROJECT_DIR = str(Path(__file__).resolve().parent)
CHROME_PROC = 'Google Chrome.app/Contents/MacOS/Google Chrome'
_QUIT_TIMEOUT = 8.0
_known_pids = set()      # 本进程亲自拉起过的 chromedriver pid
_signals_installed = False
_atexit_installed = False


def _cmd(pid):
    try:
        out = subprocess.run(['ps', '-o', 'command=', '-p', str(pid)],
                             capture_output=True, text=True, timeout=3)
        return out.stdout.strip()
    except Exception:
        return ''


def _pgrep(pattern, exact=False):
    args = ['pgrep', '-x' if exact else '-f', pattern]
    try:
        out = subprocess.run(args, capture_output=True, text=True, timeout=5)
    except Exception:
        return []
    return [int(x) for x in out.stdout.split() if x.strip().isdigit()]


def _children(pid):
    try:
        out = subprocess.run(['pgrep', '-P', str(pid)], capture_output=True, text=True, timeout=5)
    except Exception:
        return []
    return [int(x) for x in out.stdout.split() if x.strip().isdigit()]


def _comm(pid):
    """进程的真实可执行路径。用来区分“真的是 Chrome/chromedriver”和“命令行里恰好提到这些字样的别的程序”。"""
    try:
        out = subprocess.run(['ps', '-o', 'comm=', '-p', str(pid)], capture_output=True, text=True, timeout=5)
    except Exception:
        return ''
    return (out.stdout or '').strip()


def _is_browser_exec(pid):
    """只看可执行文件本身是不是 Chrome / Chrome Helper / chromedriver。

    不能用 `pgrep -f` 的命令行匹配结果直接决定杀谁：任何进程的命令行里恰好出现
    “Google Chrome.app/…”或项目路径（比如启动脚本、shell、编辑器）都会被误判，
    之前就是这样把自己的父 shell 一起 SIGKILL 了。
    """
    c = _comm(pid)
    if not c:
        return False
    if c.endswith('/chromedriver'):
        return True
    return 'Google Chrome' in c and '.app/Contents' in c


def _ancestors():
    pids = set()
    pid = os.getpid()
    for _ in range(24):
        try:
            out = subprocess.run(['ps', '-o', 'ppid=', '-p', str(pid)], capture_output=True, text=True, timeout=5)
            parent = int((out.stdout or '0').strip() or 0)
        except Exception:
            break
        if parent <= 1 or parent in pids:
            break
        pids.add(parent)
        pid = parent
    return pids


def _is_mine(pid, cmdline=None):
    cmd = cmdline if cmdline is not None else _cmd(pid)
    if not cmd:
        return False
    if PROJECT_DIR in cmd:
        return True
    # Chrome 的 cmdline 里没有项目路径，但它继承了 driver 的日志句柄
    if CHROME_PROC in cmd and '--headless' in cmd and _opens_project_log(pid):
        return True
    return False


def _opens_project_log(pid):
    """只看真正被继承下来的日志句柄（REG 且落在本目录 logs/），cwd 不能作为判据。"""
    try:
        out = subprocess.run(['lsof', '-p', str(pid)], capture_output=True, text=True, timeout=5)
    except Exception:
        return False
    logs = os.path.join(PROJECT_DIR, 'logs')
    for line in out.stdout.splitlines():
        cols = line.split()
        if len(cols) > 8 and 'REG' in line and logs in line:
            return True
    return False


def _kill(pids):
    for pid in sorted(set(pids), reverse=True):  # 先叶子后父进程，避免半途复活
        try:
            os.kill(pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass


def register(driver):
    """记下 driver 的 chromedriver pid，后面按整棵树兜底。"""
    try:
        pid = driver.service.process.pid
        if pid:
            _known_pids.add(int(pid))
    except Exception:
        pass
    return driver


def close(driver, timeout=_QUIT_TIMEOUT):
    """先给 quit() 一次机会，卡住就按进程树强杀。"""
    def _quit():
        try:
            driver.quit()
        except Exception:  # quit 失败是常态（浏览器已 wedged 或已退），交给下面的强杀
            pass

    t = threading.Thread(target=_quit, daemon=True)
    t.start()
    t.join(timeout)
    if t.is_alive():
        print(f'[browser_cleanup] quit() 超过 {timeout:.0f}s 未返回，改走强杀', flush=True)
    pid = None
    try:
        pid = int(driver.service.process.pid)
    except Exception:
        pass
    if pid:
        _kill(_descendants_of(pid) + [pid])
    _known_pids.discard(pid)
    return True


def _descendants_of(pid):
    out = []
    for kid in _children(pid):
        out.append(kid)
        out.extend(_descendants_of(kid))
    return out


def kill_service(service):
    """webdriver.Chrome() 抛异常时 chromedriver 可能已经起来了，但没人负责收它。"""
    pid = 0
    try:
        proc = getattr(service, 'process', None)
        pid = int(proc.pid) if proc else 0
    except Exception:
        pid = 0
    if pid:
        _kill(_descendants_of(pid) + [pid])
    try:
        service.stop()
    except Exception:
        pass


def sweep_stale():
    """清掉上一轮泄漏的、属于本项目的 chromedriver / Chrome。返回被杀的 pid。"""
    if os.name != 'posix':
        return []
    drivers = [p for p in _pgrep('chromedriver', exact=True) if _is_browser_exec(p)]
    chromes = [p for p in _pgrep(CHROME_PROC) if _is_browser_exec(p)]
    keep_out = {os.getpid()} | _ancestors()      # 绝不动自己和自己的父进程（pgrep -f 会误伤 shell）
    mine = {pid for pid in drivers + chromes if pid not in keep_out and _is_mine(pid)}
    # 父级兜底：调用方没给 Service(log_output=...) 时，chromedriver 的 cmdline 里没有项目路径，
    # 但它名下的 Chrome 是我们的 —— 这个 driver 必须一起收，否则它下次还会挂着一个僵尸会话。
    for pid in drivers:
        if pid not in mine and any(kid in mine for kid in _children(pid)):
            mine.add(pid)
    killed = []
    for pid in mine:
        killed.append(pid)
        killed.extend(_descendants_of(pid))
    _kill(killed)
    if killed:
        print(f'[browser_cleanup] 清掉 {len(set(killed))} 个遗留浏览器进程: {sorted(set(killed))}', flush=True)
    return sorted(set(killed))


def cleanup_all(sweep=True):
    """收尾：本进程拉过的先清，sweep=True 时再顺带清遗留。幂等。"""
    if os.name != 'posix':
        return
    pids = []
    for pid in list(_known_pids):
        pids.extend(_descendants_of(pid) + [pid])
    _kill(pids)
    _known_pids.clear()
    if sweep:
        sweep_stale()


def install_handlers(include_atexit=True, sweep_on_exit=True):
    """SIGTERM/SIGINT/SIGHUP 兜底；可选再挂个 atexit。

    被 SIGKILL 时没人能跑 handler，所以起新浏览器前还要 sweep_stale() 一次，两道保险互补。
    include_atexit=False 用于「正常退出时故意留着浏览器」的场景（login_with_2fa 等人工二次验证
    那条路径写明 Chrome 保持打开），只让 Ctrl-C / kill 走清理。
    """
    global _signals_installed, _atexit_installed
    if not _signals_installed:
        _signals_installed = True

        def _handler(signum, frame):
            cleanup_all(sweep=sweep_on_exit)
            signal.signal(signum, signal.SIG_DFL)  # 恢复默认，退出码仍是 128+signum
            os.kill(os.getpid(), signum)

        for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
            try:
                signal.signal(sig, _handler)
            except (ValueError, OSError):
                pass  # 非主线程，或平台没有这个信号

    if include_atexit and not _atexit_installed:
        _atexit_installed = True
        atexit.register(lambda: cleanup_all(sweep=sweep_on_exit))
