"""指定(日期,时段)空位持续扫描 —— 只查询、不下单，发现空位即时通知。

沿用包内的 backend_api/main.check_task_availability，所以加密请求头、日期矩阵这些逻辑和助手本体一致。
通知走三条：macOS 系统通知（带声音）、notifications.json/.txt（随时可查）、以及 config.json 里若填了
Server酱/飞书/企微 webhook 就同时推送。

用法：
  .venv/bin/python scan_notify.py --once        # 只跑一轮，验证连通
  .venv/bin/python scan_notify.py               # 持续扫（默认 90s 一轮）
可选环境变量：SCAN_INTERVAL_S=90  SCAN_TARGETS="2026-10-16:13-14,2026-10-15:14-15"
"""
import argparse
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

BASE = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE))

import requests
from venue_map import VENUE_MAP
from backend_api import AuthError, validate_cookie, query_date_ids, query_field_situation
from backend_api import BASE as API_BASE, _headers
from cookie_store import load_cookie, save_cookie
import browser_cleanup as BC
import main as APP

DEFAULT_TARGETS = [
    ("2026-10-16", "13-14"),
    ("2026-10-15", "14-15"),
    ("2026-10-18", "21-22"),
]

NOTIF_JSON = BASE / 'notifications.json'
NOTIF_TXT = BASE / 'notifications.txt'
STATE_FILE = BASE / 'scan_state.json'
LOG_FILE = BASE / 'scan.log'
MAX_LOG_BYTES = 2_000_000
MAX_NOTIFS = 20
RENOTIFY_COOLDOWN_S = 600          # 同一时段持续有空时，最多 10 分钟提醒一次
AUTH_NOTIFY_COOLDOWN_S = 1800      # 需要重新验证这类提醒半小时最多一次

_uk = os.environ.get('JACCOUNT_USERNAME', '')
_pk = os.environ.get('JACCOUNT_PASSWORD', '')


def log(msg: str):
    line = f"{datetime.now().strftime('%m-%d %H:%M:%S')} {msg}"
    try:
        if LOG_FILE.exists() and LOG_FILE.stat().st_size > MAX_LOG_BYTES:
            data = LOG_FILE.read_bytes()[-MAX_LOG_BYTES // 2:]
            LOG_FILE.write_bytes(b'... [truncated] ...\n' + data)
        with open(LOG_FILE, 'a', encoding='utf-8') as f:
            f.write(line + '\n')
    except Exception:
        pass
    print(line, flush=True)


def macos_notify(title: str, body: str, sound: str = 'Glass'):
    script = (f'display notification {json.dumps(body)} with title {json.dumps(title)}')
    try:
        subprocess.run(['osascript', '-e', script], timeout=10, capture_output=True)
    except Exception as exc:
        log(f'macos_notify failed: {exc}')


def notify_progress(event_type: str, message: str, details=None, sound='Glass', repeat: bool = True):
    """写 notifications.json/.txt + 系统通知；只在真正重要的事件上调用，避免刷屏。"""
    rec = {
        'time': datetime.now().isoformat(timespec='seconds'),
        'event': event_type,
        'message': message,
        'details': details or {},
    }
    try:
        items = json.loads(NOTIF_JSON.read_text(encoding='utf-8')) if NOTIF_JSON.exists() else []
    except Exception:
        items = []
    items.append(rec)
    NOTIF_JSON.write_text(json.dumps(items[-MAX_NOTIFS:], ensure_ascii=False, indent=2), encoding='utf-8')
    with open(NOTIF_TXT, 'a', encoding='utf-8') as f:
        f.write(f"{rec['time']} [{event_type}] {message}\n")
    log(f"NOTIFICATION [{event_type}] {message}")
    macos_notify('健身房空位扫描', message, sound)
    if event_type in ('slot_available', 'slot_still_open', 'auth_required', 'error_occurred'):
        # 静默对话框；空位命中只弹一次（25 秒自动消失），会话失效这类才持续提醒
        total, gap = ('300', '45') if repeat else ('25', '3600')
        try:
            subprocess.Popen([sys.executable, str(BASE / 'alert_helper.py'), message, total, gap],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                             start_new_session=True)
        except Exception as exc:
            log(f'alert_helper 启动失败：{exc}')
    try:                                    # config.json 里填了 webhook 就一并推送
        from messages import send_notifications
        cfg = APP.load_config().get('notifications', {})
        if any(cfg.get(k) for k in ('FANGTANG_KEY', 'FEISHU_WEBHOOK', 'WECOM_WEBHOOK')):
            send_notifications(f'[{event_type}] {message[:40]}', message, cfg)
    except Exception as exc:
        log(f'webhook notify failed: {exc}')


def load_state():
    try:
        return json.loads(STATE_FILE.read_text(encoding='utf-8'))
    except Exception:
        return {}


def save_state(state):
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding='utf-8')


def parse_targets(raw):
    out = []
    for chunk in (raw or '').split(','):
        chunk = chunk.strip()
        if not chunk or ':' not in chunk:
            continue
        d, t = chunk.split(':', 1)
        out.append((d.strip(), t.strip()))
    return out


def burst_probe(cookie: str, date_str: str, hour: int, seconds: float = 16.0, gap: float = 2.0):
    """发现空位后立刻密集复采，判断它是真的一直可约、还是一闪即灭，并把原始格子记进日志。"""
    vm = VENUE_MAP['stu_gym']
    s = requests.Session()
    hits = 0
    reads = 0
    raws = []
    end = time.time() + seconds
    while time.time() < end:
        try:
            did = query_date_ids(cookie, vm['venTypeId'], vm['venueId'], date_str, session=s).get(date_str)
            if did:
                pl, fid = query_field_situation(cookie, date_str, vm['venTypeId'], vm['venueId'], did, session=s)
                if pl and (hour - 7) < len(pl[0]):
                    cell = pl[0][hour - 7]
                    reads += 1
                    raws.append({f'{hour}:00': f"status={cell.get('status')} count={cell.get('count')} flag={cell.get('flag')} price={cell.get('price')}"})
                    if str(cell.get('status')) == '0':
                        hits += 1
        except Exception as exc:
            log(f'  复采异常：{type(exc).__name__} {str(exc)[:100]}')
        time.sleep(gap)
    with open(BASE / 'detections.log', 'a', encoding='utf-8') as f:
        f.write(json.dumps({'at': datetime.now().isoformat(timespec='seconds'),
                            'date': date_str, 'hour': hour, 'hits': hits, 'reads': reads,
                            'cells': raws}, ensure_ascii=False) + '\n')
    return hits, reads


def quota_state(cookie: str, date_str: str):
    """区分“会话失效”和“今日请求配额用尽”，避免把配额限制当成掉线去硬撞重登。"""
    vm = VENUE_MAP['stu_gym']
    try:
        r = requests.post(f"{API_BASE}/manage/fieldDetail/queryFieldReserveSituationIsFull",
                          json={"date": date_str, "feildType": vm['venTypeId'], "id": vm['venueId']},
                          headers=_headers(cookie), timeout=6)
        j = r.json()
        msg = str(j.get('msg') or '')
        if '每日请求超过限制' in msg or 'Daily request exceeds' in str(j.get('msgEn') or ''):
            return 'quota', msg
        if j.get('code') == 401 or '未登录' in msg or '登录超时' in msg:
            return 'dead', msg
        return 'ok', msg
    except Exception as exc:
        return 'unknown', str(exc)[:80]


def probe_streak(cookie: str, date_str: str, hour: int, need: int = 5, gap: float = 4.0):
    """连续确认：连续 need 次都读到 status=0 才算真有位。

    实测接口的 status 会在几十秒里 0 / -3 反复横跳（别人锁位抖动或多节点缓存不一致），
    单次命中不可信，所以必须连续确认，避免狼来了。
    """
    vm = VENUE_MAP['stu_gym']
    s = requests.Session()
    for i in range(need):
        ok = False
        try:
            did = query_date_ids(cookie, vm['venTypeId'], vm['venueId'], date_str, session=s).get(date_str)
            if did:
                pl, _ = query_field_situation(cookie, date_str, vm['venTypeId'], vm['venueId'], did, session=s)
                if pl and (hour - 7) < len(pl[0]):
                    c = pl[0][hour - 7]
                    ok = str(c.get('status')) == '0'
                    last = f"status={c.get('status')} count={c.get('count')}"
        except Exception as exc:
            last = f'{type(exc).__name__} {str(exc)[:60]}'
        if not ok:
            return False, i + 1, i, last
        if i < need - 1:
            time.sleep(gap)
    return True, need, need, last


def scan_once(cookie: str, targets, session):
    """返回 ({target: items}, 是否疑似会话失效)；AuthError 直接向上抛。

    服务端 cookie 失效时的表现是 query_date_ids 拿不到 dateId，并不抛异常，
    所以用“所有目标都拿不到 dateId”作为失效信号。
    """
    found = {}
    statuses = []
    for date_str, slot in targets:
        task = {'venue_key': 'stu_gym', 'weekdays': [1, 2, 3, 4, 5, 6, 7], 'time_ranges': [slot]}
        ok, msg, items = APP.check_task_availability(task, cookie, date_str, session=session)
        key = f'{date_str} {slot}'
        found[key] = items
        note = msg if isinstance(msg, str) else ''
        if items:
            seats = ', '.join(f"{it['time']} {it['site_name']}(¥{it.get('price', '0')})" for it in items[:4])
            log(f'  + {key}: 有 {len(items)} 个 -> {seats}')
            statuses.append('avail')
        elif 'dateId not found' in note:
            log(f'  - {key}: 空 ({note})')
            statuses.append('nodateid')
        elif 'field situation empty' in note:
            log(f'  - {key}: 接口没返回排期数据 ({note})')
            statuses.append('noresult')
        else:
            log(f'  - {key}: 空 ({note or "no slot"})')
            statuses.append('full')   # 正常返回、只是售罄，不能当成“站点没数据”
    dead = bool(statuses) and all(s == 'nodateid' for s in statuses)
    all_empty = bool(statuses) and all(s in ('noresult', 'nodateid') for s in statuses) and 'noresult' in statuses
    return found, dead, all_empty


def refresh_cookie(reason: str):
    """cookie 失效时重新登录。

    实测包内默认的 headless 形态会被 jAccount 当成新设备、再次要求二次验证，因此这里走
    strict_login 的「有窗口 + 去自动化指纹」形态，并用真实查询接口（能否拿到 dateId）判定成功。
    """
    log(f'尝试取得可用会话：{reason}')
    # 先清掉可能占着 chrome-profile 的孤儿 Chrome，否则新 Chrome 起不来
    try:
        BC.sweep_stale()   # 连 chromedriver 和它名下的 Chrome 一起收，别只靠 pkill
        subprocess.run(['pkill', '-f', 'user-data-dir.*chrome-profile'], capture_output=True)
        time.sleep(2)
    except Exception:
        pass
    (BASE / 'chrome-profile' / 'SingletonLock').unlink(missing_ok=True)
    # 快速路径：profile 里往往已经是登录状态，直接取会话。2026-10-17 的连败就是卡在这里 ——
    # 已登录时点“校内人员登录”不会跳 jAccount，strict_login 苦等跳转必然超时。
    try:
        from harvest_session import harvest_existing
        ck = harvest_existing()
        if ck:
            log('复用浏览器现有会话成功，无需重新登录')
            return ck
    except Exception as exc:
        log(f'取现有会话失败：{type(exc).__name__} {str(exc)[:150]}')
    if not (_uk and _pk):
        log('环境变量里没有 JACCOUNT_USERNAME/JACCOUNT_PASSWORD，无法走完整登录')
        return None
    try:
        import strict_login
        ok = strict_login.try_login(False, True, '自动重登（有窗口+反指纹）')
        if not ok:
            return None
        new, _ = load_cookie()
        return new
    except Exception as exc:
        log(f'自动重登失败：{type(exc).__name__} {str(exc)[:200]}')
        return None


def wait_until_active():
    """只在活跃时段（默认 07:00-23:00）发请求，避免凌晨白白消耗每日配额。

    HOLD_UNTIL="YYYY-MM-DD HH:MM" 可以让它在此之前完全不打接口（用于等配额重置）。
    """
    hold = os.environ.get('HOLD_UNTIL', '').strip()
    if hold:
        try:
            until = datetime.strptime(hold, '%Y-%m-%d %H:%M')
            while datetime.now() < until:
                log(f'按计划挂起到 {hold}（还剩 {(until - datetime.now()).total_seconds()/3600:.1f} 小时），暂不打接口')
                time.sleep(min(1800.0, max(60.0, (until - datetime.now()).total_seconds())))
        except ValueError:
            log(f'HOLD_UNTIL 格式无法解析，忽略：{hold}')
    start = int(os.environ.get('ACTIVE_START_HOUR', '7'))
    end = int(os.environ.get('ACTIVE_END_HOUR', '23'))
    napped = False
    while True:
        h = datetime.now().hour
        if start <= h < end:
            return
        nxt = datetime.now().replace(hour=start, minute=0, second=0, microsecond=0)
        if nxt <= datetime.now():
            nxt += timedelta(days=1)
        if not napped:
            log(f'非活跃时段（{start}:00-{end}:00 之外），休眠到 {nxt:%m-%d %H:%M}，不占用每日配额')
            napped = True
        time.sleep(min(900.0, max(60.0, (nxt - datetime.now()).total_seconds())))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--once', action='store_true', help='只跑一轮后退出（用于验证）')
    ap.add_argument('--interval', type=float, default=float(os.environ.get('SCAN_INTERVAL_S', 90)))
    ap.add_argument('--targets', default=os.environ.get('SCAN_TARGETS', ''), help='格式 2026-10-16:13-14,...')
    args = ap.parse_args()
    # 长跑进程被 kill / Ctrl-C 时，别把自动重登拉起来的 chromedriver+Chrome 留成孤儿（会卡关机）
    BC.install_handlers(include_atexit=False)

    targets = parse_targets(args.targets) or DEFAULT_TARGETS
    interval = max(15.0, args.interval)
    log('=== 扫描启动：只查不买 ===')
    log('目标：' + ' | '.join(f'{d} {t}' for d, t in targets) + f'；每 {interval:.0f}s 一轮')

    # 启动时没有可用会话就反复重试，绝不退出（2026-10-16 教训：原来失败即 return，结果白等一天）
    cookie = None
    while not cookie:
        wait_until_active()
        cookie, saved_at = load_cookie()
        if cookie and validate_cookie(cookie, 'stu_gym', targets[0][0]):
            break
        cookie = refresh_cookie('启动时 cookie 缺失或已失效')
        if not cookie:
            log('重登未成功，90 秒后重试（进程不退出）')
            time.sleep(90)
    log(f'使用 cookie（保存于 {load_cookie()[1]}）')

    state = load_state()
    session = requests.Session()
    try:
        from backend_api import warmup_session
        warmup_session(session)
    except Exception:
        pass

    round_no = 0
    empty_streak = 0
    while True:
        wait_until_active()
        round_no += 1
        log(f'第 {round_no} 轮')
        try:
            results, dead, all_empty = scan_once(cookie, targets, session)
        except AuthError as exc:
            log(f'认证失效：{str(exc)[:150]}')
            results, dead, all_empty = {}, True, False
        except Exception as exc:
            log(f'本轮异常：{type(exc).__name__} {str(exc)[:200]}')
            results, dead, all_empty = {}, False, False

        if dead:
            # 用官方 validate_cookie 确认一次，避免把“日期尚未放号”这类正常空结果误判成失效而狂重登
            try:
                alive = validate_cookie(cookie, 'stu_gym', targets[0][0])
            except Exception:
                alive = False
            if alive:
                log('dateId 全空但会话仍有效（可能日期未开放/已过期），继续扫描')
                dead = False

        if dead:
            state_now, why = quota_state(cookie, targets[0][0])
            if state_now == 'quota':
                # 学校接口有每日请求配额，撞上限之后再轮询只是白白消耗，睡到次日凌晨再试
                nxt = (datetime.now().replace(hour=0, minute=10, second=0, microsecond=0))
                if nxt <= datetime.now():
                    nxt = nxt + timedelta(days=1)
                wait_s = (nxt - datetime.now()).total_seconds()
                log(f'今日请求配额已用尽（{why}），休眠 {wait_s/3600:.1f} 小时至 {nxt:%m-%d %H:%M}')
                last_q = state.get('_quota_notify_ts', 0)
                if time.time() - last_q > AUTH_NOTIFY_COOLDOWN_S:
                    notify_progress('quota_exhausted',
                                    f'学校接口今日请求配额用尽，扫描休眠到 {nxt:%m-%d %H:%M}（约 {wait_s/3600:.1f} 小时后）',
                                    repeat=False)
                    state['_quota_notify_ts'] = time.time()
                    save_state(state)
                time.sleep(max(60.0, wait_s))
                continue
            log('会话已失效，尝试自动重新登录')
            new = refresh_cookie('会话失效')
            if not new:
                fails = [t for t in state.get('_login_fails', []) if time.time() - t < 3600]
                fails.append(time.time())
                state['_login_fails'] = fails
                save_state(state)
                if len(fails) >= 3:
                    # 连续失败只退避，不再一口气睡到第二天（2026-10-17 教训：登录逻辑的 bug
                    # 被误判成“配额用尽”，结果一整天没扫）。真正的配额情况由上面的 quota_state 分支处理。
                    notify_progress('auth_required',
                                    f'一小时内 {len(fails)} 次拿不到可用会话，退避 30 分钟后继续重试；'
                                    '如果一直不行，需要人工跑一次 login_with_2fa.py',
                                    repeat=False)
                    state['_login_fails'] = []
                    save_state(state)
                    log('退避 1800 秒后重试')
                    time.sleep(1800)
                    continue
            if new:
                cookie = new
                continue
            last = state.get('_auth_notify_ts', 0)
            if time.time() - last > AUTH_NOTIFY_COOLDOWN_S:
                notify_progress('auth_required',
                                '拿不到可用会话（多数是 profile 里会话过期，不一定还要二次验证）。'
                                '先手动跑 harvest_session.py，不行再跑 login_with_2fa.py',
                                repeat=False)
                state['_auth_notify_ts'] = time.time()
                save_state(state)
            log('5 分钟后重试登录，期间扫描无意义')
            time.sleep(300)
            continue

        if all_empty:
            empty_streak += 1
            if empty_streak >= 3:
                # 排期接口连续返回空（站点慢或会话被顶掉），不能当成“没空位”默默跳过
                try:
                    alive = validate_cookie(cookie, 'stu_gym', targets[0][0])
                except Exception:
                    alive = False
                if alive:
                    log(f'连续 {empty_streak} 轮排期为空，但会话仍有效，判断为站点侧暂无数据')
                else:
                    log(f'连续 {empty_streak} 轮排期为空且会话校验失败，按失效处理')
                    new = refresh_cookie('排期连续为空')
                    if new:
                        cookie = new
                        empty_streak = 0
                        continue
                    notify_progress('auth_required',
                                    '连续查不到排期且会话失效，需要重新登录+二次验证：跑 login_with_2fa.py',
                                    repeat=False)
                    time.sleep(300)
                    continue
        else:
            empty_streak = 0

        for key, items in results.items():
            st = state.get(key, {})
            now_avail = bool(items)
            was_avail = bool(st.get('available'))
            if now_avail and (not was_avail or time.time() - st.get('notified_ts', 0) > RENOTIFY_COOLDOWN_S):
                seats = items[0]
                detail = f'{seats["date"]} {seats["time"]} {seats["site_name"]}'
                # 先连续确认 5 次（约 16 秒），抖动/横跳的不打扰人
                confirmed, reads_n, hits_n, last = probe_streak(cookie, seats['date'], seats['hour'])
                if not confirmed:
                    log(f'  {key}: 命中未连续确认（第 {reads_n} 次变成 {last}），判为抖动，不弹框')
                    with open(NOTIF_TXT, 'a', encoding='utf-8') as f:
                        f.write(f"{datetime.now().isoformat(timespec='seconds')} [slot_blink] {key} —— "
                                f"连续确认失败：第 {reads_n} 次为 {last}，未弹告警\n")
                    state[key] = {'available': False, 'notified_ts': st.get('notified_ts', 0),
                                  'count': 0, 'blink': True}
                    save_state(state)
                    continue
                notify_progress('slot_available', f'连续 {reads_n} 次确认可约：{key} —— {detail}，马上刷新页面下单',
                                repeat=False)
                # 随后密集复采 40 秒：如果还在，说明你有时间，再补一次提醒；同时留下原始证据
                hits, reads = burst_probe(cookie, seats['date'], seats['hour'], seconds=40, gap=5)
                with open(NOTIF_TXT, 'a', encoding='utf-8') as f:
                    f.write(f"{datetime.now().isoformat(timespec='seconds')} [slot_verify] {key} —— "
                            f"提醒后 40 秒内 {reads} 次复采 {hits} 次仍 status=0\n")
                log(f'  复采：40 秒内 {reads} 次里 {hits} 次仍可约')
                if hits >= 3:
                    notify_progress('slot_still_open', f'空位仍在（{reads} 次复采 {hits} 次可约）：{key}', repeat=False)
                state[key] = {'available': True, 'notified_ts': time.time(),
                              'count': len(items), 'seats': [it['site_name'] for it in items]}
            elif not now_avail and was_avail:
                log(f'  {key}: 刚才的空位已被占，状态复位')
                state[key] = {'available': False, 'notified_ts': st.get('notified_ts', 0), 'count': 0}
            elif now_avail:
                state[key]['count'] = len(items)
            else:
                state[key] = {'available': False, 'count': 0}
            save_state(state)

        if args.once:
            log('单轮模式结束')
            return
        log(f'休眠 {interval:.0f}s')
        time.sleep(interval)


if __name__ == '__main__':
    try:
        main()
    except KeyboardInterrupt:
        log('收到 Ctrl-C，退出')
