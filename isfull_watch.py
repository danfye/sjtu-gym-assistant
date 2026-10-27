"""粗筛哨兵：排期接口被限额时，用 IsFull 这一个还没被限制的接口继续盯。

queryFieldReserveSituationIsFull 一次返回全部日期，每条带 isAll（当天是否全满），且它和
queryFieldSituation 各自计限额 —— 所以排期接口锁着的时段，哨兵照样能盯：只要某个目标日期
的 isAll 从 true 翻成 false，就推飞书 + 本机弹框，让你去页面上确认。它给不出具体钟点
（具体钟点要排期接口），所以文案里明说是粗筛，别当成已核实的空位。

用法：
    AUTO_SCHEDULE=1 EXCLUDE_DATES=2026-10-28 .venv/bin/python isfull_watch.py --interval 90
会话失效时它会自动去跑 login_assist.py（静默 SSO 重铸 JSESSIONID），不用人管。
"""
import argparse
import json
import os
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

BASE = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE))

import scan_notify as SN
from cookie_store import load_cookie

LOG = BASE / 'isfull_watch.log'
STATE = BASE / 'isfull_state.json'
ALERT_COOLDOWN_S = float(os.environ.get('ALERT_COOLDOWN_S', '600'))
LOGIN_RETRY_S = float(os.environ.get('LOGIN_RETRY_S', '1800'))


def log(msg):
    line = f'{time.strftime("%m-%d %H:%M:%S")} {msg}'
    with open(LOG, 'a', encoding='utf-8') as f:
        f.write(line + '\n')
    print(line, flush=True)


def targets():
    if os.environ.get('AUTO_SCHEDULE', '') == '1':
        got = SN.apply_excludes(SN.parse_targets(SN.auto_targets()))
        if got:
            return got
    raw = os.environ.get('SCAN_TARGETS', '').strip()
    if raw:
        return SN.parse_targets(raw)
    d = time.strftime('%Y-%m-%d')
    return [(d, '12-22')]


def push(title, body):
    """走和扫描器同一套通道：本机静默弹框 + notifications.txt + 飞书。"""
    try:
        subprocess.Popen([sys.executable, str(BASE / 'alert_helper.py'), body, '300', '45'],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
    except Exception as exc:
        log(f'弹框启动失败：{type(exc).__name__}')
    with open(BASE / 'notifications.txt', 'a', encoding='utf-8') as f:
        f.write(f"{datetime.now().isoformat(timespec='seconds')} [gate_available] {body}\n")
    try:
        import main as APP
        from messages import send_notifications
        cfg = APP.load_config().get('notifications', {})
        if any(cfg.get(k) for k in ('FANGTANG_KEY', 'FEISHU_WEBHOOK', 'WECOM_WEBHOOK')):
            res = send_notifications(f'健身房 [粗筛] {title[:36]}', body, cfg)
            bad = [f'{k}: {str(v)[:70]}' for k, v in (res or {}).items() if 'success' not in str(v).lower()]
            if bad:
                log('webhook 被退回 —— ' + ' | '.join(bad))
    except Exception as exc:
        log(f'推送失败：{type(exc).__name__} {str(exc)[:60]}')


def try_relogin():
    log('会话不可用，跑 login_assist.py 走一次静默 SSO')
    try:
        subprocess.run([sys.executable, '-u', str(BASE / 'login_assist.py')], cwd=str(BASE),
                       stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT, timeout=420)
    except Exception as exc:
        log(f'重登子进程异常：{type(exc).__name__}')


def detect(dates, ids, prev, last_alert, now=None):
    """返回 (本轮快照, 需要告警的日期列表)。

    只有 isAll 明确为 false 才算空位线索，null/缺失当满处理 —— 否则字段一缺就天天误报。
    同一个日期在 ALERT_COOLDOWN_S 内只告一次，跨进程靠 isfull_state.json 记住上次状态。
    """
    now = now or time.time()
    snap, flips = {}, []
    for d in dates:
        info = ids.get(d)
        if not isinstance(info, dict):
            snap[d] = 'unknown'
            continue
        snap[d] = 'open' if info.get('isAll') is False else 'full'
        if snap[d] == 'open' and prev.get(d) != 'open':
            if now - last_alert.get(d, 0) > ALERT_COOLDOWN_S:
                flips.append(d)
    return snap, flips


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--interval', type=float, default=90.0)
    args = ap.parse_args()
    interval = max(30.0, args.interval)

    prev = {}
    if STATE.exists():
        try:
            prev = json.loads(STATE.read_text(encoding='utf-8'))
        except Exception:
            prev = {}
    last_alert = {}
    last_login_try = 0.0

    dates = sorted({d for d, _ in targets()})
    log(f'粗筛哨兵启动：盯 {" ".join(dates)}；每 {interval:.0f}s 一次请求（IsFull 接口，与排期限额分开计）')
    while True:
        cookie = load_cookie()[0]
        probe_date = dates[0] if dates else time.strftime('%Y-%m-%d')
        ids, msg = SN.is_full_state(cookie, probe_date)
        if not ids:
            if time.time() - last_login_try > LOGIN_RETRY_S:
                last_login_try = time.time()
                try_relogin()
            else:
                log(f'拿不到日期映射（{msg[:40] or "空"}），等下一轮')
            time.sleep(300)
            continue

        snap, flips = detect(dates, ids, prev, last_alert)
        prev = snap
        STATE.write_text(json.dumps(snap, ensure_ascii=False), encoding='utf-8')

        now_s = time.strftime('%H:%M:%S')
        if flips:
            for d in flips:
                last_alert[d] = time.time()
            body = (f'{now_s} 粗筛发现 {"、".join(flips)} 显示有空位（isAll=false）。'
                    '排期接口此刻还在限额里，我给不出具体钟点 —— 马上打开预约页面逐小时看，'
                    '这种回流通常只有一两分钟。')
            log(f'[gate_available] {"、".join(flips)}')
            push('粗筛发现空位', body)
        else:
            log('全部日期 isAll=true（满）' if all(v == 'full' for v in snap.values())
                else f'状态：{snap}')
        time.sleep(interval)


if __name__ == '__main__':
    main()
