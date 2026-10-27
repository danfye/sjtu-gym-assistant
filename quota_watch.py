"""额度探针：每 N 分钟只发 1 个 queryFieldSituation 请求，一通就自动把扫描器拉起来。

为什么需要：额度是按账号算的（同一时刻换出口 IP 也一样被拒），而重置点到底在几点并没有
被证实过。与其让扫描器死睡到次日 00:10，不如用极小的代价（每 15 分钟 1 个请求）把真实
恢复时刻量出来，恢复后立刻继续扫。探测结果写在 quota_watch.log，方便回看曲线。
"""
import os
import subprocess
import sys
import time
from pathlib import Path

BASE = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE))

import scan_notify as SN

LOG = BASE / 'quota_watch.log'
STEP_S = float(os.environ.get('PROBE_STEP_S', '900'))
DEADLINE_S = float(os.environ.get('PROBE_MAX_S', str(14 * 3600)))
START_CMD = [sys.executable, '-u', str(BASE / 'scan_notify.py')] + sys.argv[1:]


def log(msg):
    line = f'{time.strftime("%m-%d %H:%M:%S")} {msg}'
    with open(LOG, 'a', encoding='utf-8') as f:
        f.write(line + '\n')
    print(line, flush=True)


def probe():
    cookie = SN.load_cookie()[0]
    date_str = time.strftime('%Y-%m-%d', time.localtime(time.time() + 86400))
    ids, msg = SN.is_full_state(cookie, date_str)
    if not ids:
        return 'no_session', msg[:40]
    did = ids.get(date_str, {})
    did = did.get('dateId') if isinstance(did, dict) else did
    m2, _ = SN.situation_probe_msg(cookie, date_str)
    if '每日请求超过限制' in m2:
        return 'quota', m2[:40]
    if '请求过于频繁' in m2:
        return 'throttle', m2[:40]
    return 'open', m2[:40] or 'ok'


if __name__ == '__main__':
    log(f'探针启动：每 {STEP_S / 60:.0f} 分钟 1 个请求，最长 {DEADLINE_S / 3600:.0f} 小时；命令 {" ".join(START_CMD[-2:])}')
    waited = 0
    while waited < DEADLINE_S:
        state, detail = probe()
        log(f'状态={state} 详情={detail or "无"}')
        if state == 'open':
            log('额度已恢复，拉起扫描器：' + ' '.join(START_CMD[-2:]))
            p = subprocess.Popen(START_CMD, cwd=str(BASE), stdout=subprocess.DEVNULL,
                                 stderr=subprocess.STDOUT, start_new_session=True)
            log(f'扫描器 pid={p.pid}')
            break
        if state == 'no_session':
            log('会话也不可用（需要 login_assist.py 重铸），探针继续等')
        time.sleep(STEP_S)
        waited += STEP_S
    else:
        log('探测窗口结束仍未恢复，放弃')
