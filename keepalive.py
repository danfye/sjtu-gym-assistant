#!/usr/bin/env python3
"""看门狗：扫描器/哨兵进程消失，或日志长时间不前进时，重新拉起。

为什么需要它：2026-11-07 22:43 机器重启把两个任务静默收掉，直到两天后才被发现；
2026-11-10 上午一次重登卡死，日志 89 分钟一行没出，那段时间扫描完全是瞎的。
run_detached.py 能躲开「随 CLI 会话退出」，但躲不开重启和卡死，这里补上。

推送策略（他明确说过：报错别刷屏）：
  - 救回来了就一声不吭，只写 keepalive.log；
  - 只有同一任务在 30 分钟内需要第二次重启（说明自愈无效），或重启本身失败，才推一条；
  - 推送计数落盘，重启看门狗也不会把「每天最多几条」的额度刷回满。

凭证只从自己的环境变量往下传给子进程，绝不写盘。
"""
import json
import os
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

BASE = Path(__file__).resolve().parent
PIDFILE = Path('/tmp/gym_detached.json')
LOG = BASE / 'keepalive.log'
STATE = BASE / 'state' / 'keepalive_state.json'
CHECK_S = float(os.environ.get('KEEPALIVE_CHECK_S', '120'))
STALE_S = float(os.environ.get('KEEPALIVE_STALE_S', '2700'))     # 日志 45 分钟不动才算卡死
RESTART_COOLDOWN_S = float(os.environ.get('KEEPALIVE_COOLDOWN_S', '600'))
ESCALATE_WINDOW_S = float(os.environ.get('KEEPALIVE_ESCALATE_S', '1800'))
PUSH_CAP_PER_DAY = int(os.environ.get('KEEPALIVE_PUSH_CAP', '2'))
ACTIVE_START_HOUR = int(os.environ.get('ACTIVE_START_HOUR', '10'))
ACTIVE_END_HOUR = int(os.environ.get('ACTIVE_END_HOUR', '23'))

JOBS = [
    ('scan_notify.py', ['--interval', '0'], 'scan.log'),
    ('isfull_watch.py', ['--interval', '120'], 'isfull_watch.log'),
]


def log(msg):
    line = f'{time.strftime("%m-%d %H:%M:%S")} {msg}'
    with open(LOG, 'a', encoding='utf-8') as f:
        f.write(line + '\n')
    print(line, flush=True)


def load_state():
    try:
        return json.loads(STATE.read_text(encoding='utf-8'))
    except Exception:
        return {}


def save_state(st):
    try:
        STATE.parent.mkdir(exist_ok=True)
        STATE.write_text(json.dumps(st, ensure_ascii=False), encoding='utf-8')
    except Exception as exc:
        log(f'状态写入失败：{type(exc).__name__}')


def push(st, title, body):
    """手机上只出现真正需要人看一眼的情况，且落盘计数、每天封顶。"""
    today = datetime.now().strftime('%Y-%m-%d')
    rec = st.setdefault('push', {})
    if rec.get('day') != today:
        rec.clear(day=today, n=0)
    if int(rec.get('n', 0)) >= PUSH_CAP_PER_DAY:
        log(f'[push_capped] 今日推送已达 {PUSH_CAP_PER_DAY} 条上限，{title} 只记日志')
        return False
    rec['n'] = int(rec.get('n', 0)) + 1
    save_state(st)
    subprocess.Popen([sys.executable, str(BASE / 'alert_helper.py'), body, '300', '45'],
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
    try:
        import main as APP
        from messages import send_notifications
        cfg = APP.load_config().get('notifications', {})
        res = send_notifications(f'健身房 [看门狗] {title[:30]}', body, cfg)
        bad = [f'{k}: {str(v)[:70]}' for k, v in (res or {}).items() if 'success' not in str(v).lower()]
        if bad:
            log('webhook 被退回 —— ' + ' | '.join(bad))
    except Exception as exc:
        log(f'推送失败：{type(exc).__name__} {str(exc)[:60]}')
    return True


def pids():
    try:
        return json.loads(PIDFILE.read_text(encoding='utf-8'))
    except Exception:
        return {}


def alive(pid):
    try:
        os.kill(int(pid), 0)
        return True
    except Exception:
        return False


def within_active_hours():
    return ACTIVE_START_HOUR <= datetime.now().hour < ACTIVE_END_HOUR


def idle_s(logfile):
    try:
        return time.time() - (BASE / logfile).stat().st_mtime
    except Exception:
        return 0.0


def restart(script, args):
    """静默重启：成功与否都返回给调用方，是否喊人由 main 循环按连续失败次数决定。"""
    pid = pids().get(script)
    if pid and alive(pid):
        log(f'{script} 卡死，先收掉 pid={pid}')
        try:
            os.killpg(int(pid), 15)
        except Exception:
            try:
                os.kill(int(pid), 15)
            except Exception:
                pass
        time.sleep(5)
    try:
        out = subprocess.run([sys.executable, str(BASE / 'run_detached.py'), script, *args],
                             cwd=str(BASE), capture_output=True, text=True, timeout=120)
        log(f'{script} 已静默重启：{(out.stdout or out.stderr).strip()[:120]}')
        return True
    except Exception as exc:
        log(f'{script} 重启失败：{type(exc).__name__} {str(exc)[:100]}')
        return False


def main():
    st = load_state()
    log(f'看门狗启动：{" ".join(j[0] for j in JOBS)}；每 {CHECK_S:.0f}s 查一次；'
        f'日志超过 {STALE_S / 60:.0f} 分钟不动（{ACTIVE_START_HOUR}:00-{ACTIVE_END_HOUR}:00）算卡死；'
        f'自愈静默，{ESCALATE_WINDOW_S / 60:.0f} 分钟内需要第二次重启才推一条；'
        f'凭证{"在环境里，可下传" if os.environ.get("JACCOUNT_PASSWORD") else "不在环境里（子进程只能复用浏览器 profile 登录）"}')
    while True:
        st = load_state()
        jobs = st.setdefault('jobs', {})
        for script, args, logfile in JOBS:
            key = script
            rec = jobs.setdefault(key, {'ts': []})
            pid = pids().get(script)
            reason = None
            if not pid or not alive(pid):
                reason = '进程不在了'
            elif idle_s(logfile) > STALE_S and within_active_hours():
                reason = f'日志 {idle_s(logfile) / 60:.0f} 分钟没动'
            if not reason:
                # 活着就当作已恢复；只在真恢复后清一次计数，避免每次循环都写盘
                if rec['ts'] and time.time() - rec['ts'][-1] > ESCALATE_WINDOW_S:
                    rec['ts'] = []
                    save_state(st)
                continue
            last = rec['ts'][-1] if rec['ts'] else 0
            if time.time() - last < RESTART_COOLDOWN_S:
                log(f'{script} 仍异常（{reason}），距上次重启不足 {RESTART_COOLDOWN_S / 60:.0f} 分钟，先看下一轮')
                continue
            ok = restart(script, args)
            now = time.time()
            rec['ts'] = [t for t in rec['ts'] if now - t < ESCALATE_WINDOW_S] + ([now] if ok else [])
            save_state(st)
            if not ok:
                push(st, f'{script} 重启失败',
                     f'{time.strftime("%m-%d %H:%M")} {reason}，自动重启失败，需要人工起一次。')
            elif len(rec['ts']) >= 2:
                push(st, f'{script} 反复异常',
                     f'{time.strftime("%m-%d %H:%M")} {ESCALATE_WINDOW_S / 60:.0f} 分钟内第 {len(rec["ts"])} 次重启'
                     f'（{reason}），自愈没能稳住，麻烦人工看一眼 scan.log / isfull_watch.log。')
            else:
                log(f'{script} 第 1 次重启（{reason}）——按策略静默，不再推手机')
        time.sleep(CHECK_S)


if __name__ == '__main__':
    main()
