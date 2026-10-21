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
REQUEST_SPACING_S = float(os.environ.get('REQUEST_SPACING_S', '1.2'))
RENOTIFY_COOLDOWN_S = 600          # 同一时段持续有空时，最多 10 分钟提醒一次
AUTH_NOTIFY_COOLDOWN_S = 1800      # 需要重新验证这类提醒半小时最多一次
STALL_NOTIFY_S = 1800              # 连续 30 分钟拿不到真实数据 → 提醒“在空转”

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
            did = raw_date_ids(cookie, date_str, s)[0].get(date_str)
            if did:
                pl, fid = raw_situation(cookie, date_str, did, s), None
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
    last = '尚未采到数据'
    for i in range(need):
        ok = False
        try:
            did = raw_date_ids(cookie, date_str, s)[0].get(date_str)
            if did:
                pl, _ = raw_situation(cookie, date_str, did, s), None
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


def raw_date_ids(cookie: str, date_str: str, session=None):
    """单次拿 dateId 映射（这个接口一次就返回约 8 个日期），不重试。返回 (映射, 服务端msg)。"""
    vm = VENUE_MAP['stu_gym']
    s = session or requests
    try:
        r = s.post(f"{API_BASE}/manage/fieldDetail/queryFieldReserveSituationIsFull",
                   json={"date": date_str, "feildType": vm['venTypeId'], "id": vm['venueId']},
                   headers=_headers(cookie), timeout=8)
        j = r.json()
        data = j.get('data') or []
        return ({x.get('date'): x.get('dateId') for x in data if x.get('date') and x.get('dateId')},
                str(j.get('msg') or ''))
    except Exception as exc:
        return {}, f'{type(exc).__name__} {str(exc)[:60]}'


def raw_situation(cookie: str, date_str: str, date_id: str, session=None):
    """单次拿某日排期矩阵，失败不重试（backend_api 那个版本会重试 20 次，限流时会把请求量放大十几倍）。"""
    vm = VENUE_MAP['stu_gym']
    s = session or requests
    try:
        r = s.post(f"{API_BASE}/manage/fieldDetail/queryFieldSituation",
                   json={'fieldType': vm['venTypeId'], 'date': date_str, 'venueId': vm['venueId'], 'dateId': date_id},
                   headers=_headers(cookie), timeout=8)
        j = r.json()
        data = j.get('data') or []
        return [x.get('priceList', []) for x in data], str(j.get('msg') or '')
    except Exception as exc:
        return [], f'{type(exc).__name__} {str(exc)[:60]}'


def scan_once(cookie: str, targets, session):
    """一轮只发 1 + 目标日期数 个请求：一次 dateId 映射 + 每个日期一次排期。

    返回 (found, dead, all_empty, real_data, throttle_msg)。throttle_msg 非空说明这一轮是被
    限流挡住的，上层据此退避，而不是当成“会话失效”去登录（登录一次要发几十个请求）。
    """
    vm = VENUE_MAP['stu_gym']
    found = {}
    statuses = []
    throttle = ''
    dates = sorted({d for d, _ in targets})
    ids, msg = raw_date_ids(cookie, dates[0], session)
    if '每日请求超过限制' in msg or '请求过于频繁' in msg:
        return {}, False, True, False, msg
    by_date = {}
    for date_str, slot in targets:
        by_date.setdefault(date_str, []).append(slot)
    for date_str in sorted(by_date):
        slots = by_date[date_str]
        keys = [f'{date_str} {s}' for s in slots]
        date_id = ids.get(date_str)
        if not date_id:
            ids2, msg2 = raw_date_ids(cookie, date_str, session)
            if '每日请求超过限制' in msg2 or '请求过于频繁' in msg2:
                throttle = throttle or msg2
            date_id = ids2.get(date_str)
        if not date_id:
            for k in keys:
                found[k] = []
            log(f'  - {date_str}: 空 (未取到 dateId，可能未放号或会话失效)')
            statuses.append('nodateid')
            continue
        # 一个日期只查一次排期，同一天的多个时段区间共用这份矩阵。
        # 请求之间留一点间隔：一轮 9 个请求在 1 秒内打完会撞上“请求过于频繁”的突发速率限制。
        time.sleep(REQUEST_SPACING_S)
        price_lists, msg3 = raw_situation(cookie, date_str, date_id, session)
        if '每日请求超过限制' in msg3 or '请求过于频繁' in msg3:
            throttle = throttle or msg3
        if not price_lists or not price_lists[0]:
            for k in keys:
                found[k] = []
            log(f'  - {date_str}: 接口没返回排期数据 ({msg3 or "空 data"})')
            statuses.append('noresult')
            continue
        day_hit = False
        for slot in slots:
            key = f'{date_str} {slot}'
            h1, h2 = (slot.split('-') + [''])[:2]
            try:
                h1, h2 = int(h1), int(h2)
            except ValueError:
                found[key] = []
                continue
            items = []
            for hour in range(h1, h2):
                row = hour - 7
                if row < 0 or row >= len(price_lists[0]):
                    continue
                for f_idx, pl in enumerate(price_lists):
                    cell = pl[row] if row < len(pl) else None
                    if not cell or str(cell.get('status')) != '0':
                        continue
                    items.append({
                        'venue_key': 'stu_gym', 'display': vm.get('display', 'stu_gym'),
                        'date': date_str, 'hour': hour, 'time': f'{hour}:00-{hour+1}:00',
                        'seat_idx': f_idx, 'site_name': f'场地{f_idx + vm["name_start_index"]}',
                        'field_id': None, 'price': cell.get('price', '0'),
                        'count': cell.get('count'), 'has_sign': bool(cell.get('sign')),
                    })
            found[key] = items
            if items:
                day_hit = True
                seats = ', '.join(f"{it['time']} {it['site_name']}(¥{it.get('price', '0')})" for it in items[:4])
                log(f'  + {key}: 有 {len(items)} 个 -> {seats}')
            else:
                log(f'  - {key}: 空 (售罄)')
        statuses.append('avail' if day_hit else 'full')
    dead = bool(statuses) and all(s == 'nodateid' for s in statuses)
    all_empty = bool(statuses) and all(s in ('noresult', 'nodateid') for s in statuses) and 'noresult' in statuses
    real_data = any(s in ('full', 'avail') for s in statuses)
    return found, dead, all_empty, real_data, throttle


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


def situation_probe_msg(cookie: str, date_str: str, session=None):
    """只发一次 queryFieldSituation，取回它的 msg。

    这个接口才是真正被每日配额限制的那个，而 backend_api.query_field_situation 失败时会
    内部重试 20 次 —— 五个日期一轮就是 100 个请求，越限流越猛。所以每轮先单发一次探一下。
    """
    vm = VENUE_MAP['stu_gym']
    s = session or requests
    try:
        did = raw_date_ids(cookie, date_str, s)[0].get(date_str)
    except AuthError:
        raise
    except Exception as exc:
        return f'{type(exc).__name__} {str(exc)[:60]}', None
    if not did:
        # 拿不到 dateId 的原因可能是限流、也可能是真的没放号 —— 单发一次原始请求把服务端回话取回来，
        # 否则上层只能盲目去登录（登录本身要发几十个请求，限流时只会更糟）。
        st_now, why_now = quota_state(cookie, date_str)
        return (why_now or 'no-dateId'), None
    try:
        r = s.post(f"{API_BASE}/manage/fieldDetail/queryFieldSituation",
                   json={'fieldType': vm['venTypeId'], 'date': date_str, 'venueId': vm['venueId'], 'dateId': did},
                   headers=_headers(cookie), timeout=8)
        j = r.json()
        return str(j.get('msg') or ''), (j.get('data') or [])
    except Exception as exc:
        return f'{type(exc).__name__} {str(exc)[:60]}', None


def auto_targets():
    """按课表滚动生成目标（AUTO_SCHEDULE=1）。

    课表文件超过 6 天会自动重抓一次：登教务只用于读取课表，会弹一次 Chrome 窗口。
    """
    try:
        import gym_windows as GW
        sched = GW.load_schedule() or {}
        upd = str(sched.get('updated_at') or '')
        stale = True
        try:
            age = (datetime.now() - datetime.strptime(upd, '%Y-%m-%dT%H:%M:%S')).total_seconds()
            stale = age > 6 * 86400
        except ValueError:
            pass
        if stale and os.environ.get('REFRESH_SCHEDULE', '1') == '1':
            log('课表已超过 6 天，重新抓取（弹一次 Chrome 登录教务，只读课表）')
            for script in ('fetch_timetable.py', 'parse_timetable.py'):
                try:
                    r = subprocess.run([sys.executable, str(BASE / script)], cwd=str(BASE),
                                       stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT, timeout=900)
                    log(f'{script} 退出码 {r.returncode}')
                except Exception as exc:
                    log(f'{script} 失败：{type(exc).__name__} {str(exc)[:100]}')
        return GW.targets_spec()
    except Exception as exc:
        log(f'按课表生成目标失败，沿用原目标：{type(exc).__name__} {str(exc)[:120]}')
        return ''


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
    auto = os.environ.get('AUTO_SCHEDULE', '') == '1'
    cur_day = datetime.now().date()
    if auto:
        got = parse_targets(auto_targets())
        if got:
            targets = got
    interval = max(15.0, args.interval)
    log('=== 扫描启动：只查不买 ===')
    log(('[按课表自动]' if auto else '[手动]') + '目标：' + ' | '.join(f'{d} {t}' for d, t in targets)
        + f'；每 {interval:.0f}s 一轮')

    # 启动时没有可用会话就反复重试，绝不退出（2026-10-16 教训：原来失败即 return，结果白等一天）
    state = load_state()
    if '_last_ok_ts' not in state:
        state['_last_ok_ts'] = time.time()
        save_state(state)
    cookie = None
    while not cookie:
        wait_until_active()
        cookie, saved_at = load_cookie()
        if cookie and validate_cookie(cookie, 'stu_gym', targets[0][0]):
            break
        msg_s, _d = situation_probe_msg(cookie or '', targets[0][0])
        if '请求过于频繁' in msg_s or '每日请求超过限制' in msg_s:
            log(f'接口处于限流中（{msg_s}），15 分钟内不做登录尝试')
            time.sleep(900)
            continue
        cookie = refresh_cookie('启动时 cookie 缺失或已失效')
        if not cookie:
            fails = [t for t in state.get('_boot_fails', []) if time.time() - t < 3600]
            fails.append(time.time())
            state['_boot_fails'] = fails
            save_state(state)
            gap = 1800 if len(fails) >= 3 else 90   # 连败三次就退到 30 分钟，别整夜 hammer 站点
            log(f'重登未成功（一小时内第 {len(fails)} 次），{gap} 秒后重试（进程不退出）')
            time.sleep(gap)
    log(f'使用 cookie（保存于 {load_cookie()[1]}）')

    state = load_state()
    if '_last_ok_ts' not in state:
        state['_last_ok_ts'] = time.time()   # 新进程别一上来就报“空转几十年”
        save_state(state)
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
        if auto and datetime.now().date() != cur_day:
            cur_day = datetime.now().date()
            got = parse_targets(auto_targets())
            if got:
                targets = got
                log('新的一天，按课表更新目标：' + ' | '.join(f'{d} {t}' for d, t in targets))
        round_no += 1
        log(f'第 {round_no} 轮')
        try:
            # 每轮轮换起点：短窗口限流只会打到一轮里排在后面的日期，固定顺序会让
            # 最后那一两个日期永远查不到
            rot = targets[round_no % len(targets):] + targets[:round_no % len(targets)]
            results, dead, all_empty, real_data, throttle = scan_once(cookie, rot, session)
        except AuthError as exc:
            log(f'认证失效：{str(exc)[:150]}')
            results, dead, all_empty, real_data, throttle = {}, True, False, False, ''
        except Exception as exc:
            log(f'本轮异常：{type(exc).__name__} {str(exc)[:200]}')
            results, dead, all_empty, real_data, throttle = {}, False, False, False, ''

        if throttle and '每日请求超过限制' in throttle:
            # 每日配额是全局的，本轮是否拿到过数据都要休眠到次日
            state['_last_ok_ts'] = time.time()
            save_state(state)
            nxt = datetime.now().replace(hour=0, minute=10, second=0, microsecond=0)
            if nxt <= datetime.now():
                nxt += timedelta(days=1)
            wait_s = (nxt - datetime.now()).total_seconds()
            log(f'今日配额已用尽（{throttle}），休眠 {wait_s/3600:.1f} 小时到 {nxt:%m-%d %H:%M}')
            if time.time() - state.get('_quota_notify_ts', 0) > AUTH_NOTIFY_COOLDOWN_S:
                notify_progress('quota_exhausted',
                                f'今日请求配额已用尽，扫描休眠到 {nxt:%m-%d %H:%M}（约 {wait_s/3600:.1f} 小时后恢复）',
                                repeat=False)
                state['_quota_notify_ts'] = time.time()
                save_state(state)
            time.sleep(max(60.0, wait_s))
            continue

        if throttle and not real_data:
            # 整轮都被短窗口节流才退避。只有靠后的几个日期被限流时继续跑，
            # 否则每 5 分钟就会被拖停 15 分钟。
            state['_last_ok_ts'] = time.time()
            save_state(state)
            log(f'被短窗口节流（{throttle}），退避 900 秒')
            time.sleep(900)
            continue

        if dead:
            if '请求过于频繁' in throttle or '每日请求超过限制' in throttle:
                # 拿不到 dateId 是限流造成的，不是会话失效 —— 这时去登录只会让限流更久
                log(f'dateId 全空但原因是限流（{throttle}），不尝试登录，15 分钟后再看')
                state['_last_ok_ts'] = time.time()
                save_state(state)
                time.sleep(900)
                continue
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
                    st_now, why = quota_state(cookie, targets[0][0])
                    if st_now == 'quota':
                        nxt = datetime.now().replace(hour=0, minute=10, second=0, microsecond=0) + timedelta(days=1)
                        wait_s = (nxt - datetime.now()).total_seconds()
                        log(f'排期为空的原因是今日请求配额用尽（{why}），休眠到 {nxt:%m-%d %H:%M}')
                        notify_progress('quota_exhausted',
                                        f'今日请求配额已用尽（{why}），扫描休眠约 {wait_s/3600:.1f} 小时，'
                                        f'到 {nxt:%m-%d %H:%M} 自动恢复',
                                        repeat=False)
                        state['_last_ok_ts'] = time.time()
                        save_state(state)
                        time.sleep(max(60.0, wait_s))
                        continue
                    log(f'连续 {empty_streak} 轮排期为空，会话有效且未触发配额，判断为站点侧暂无数据')
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

        # 看门狗：只要连续 30 分钟没拿到过一次真实数据，就明确提醒“我在空转”，不再让人自己猜
        if real_data:
            state['_last_ok_ts'] = time.time()
            save_state(state)
        last_ok = state.get('_last_ok_ts', 0)
        if time.time() - last_ok > STALL_NOTIFY_S:
            log(f'已经 {(time.time()-last_ok)/60:.0f} 分钟没拿到真实数据，提醒一次')
            notify_progress('stalled',
                            f'扫描已空转 {(time.time()-last_ok)/60:.0f} 分钟（会话或站点问题），'
                            '现在没有可靠监测；需要时手动跑 harvest_session.py 或 login_with_2fa.py',
                            repeat=False)
            state['_last_ok_ts'] = time.time()   # 重新计时，避免每轮都弹
            save_state(state)

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
    except Exception:
        # 未捕获异常会让监测静默停掉，这里落到日志并弹一次告警
        import traceback
        tb = traceback.format_exc()
        log('扫描进程异常退出：\n' + tb[-1200:])
        try:
            notify_progress('crashed', '扫描进程异常退出，监测已停止：' + tb.strip().splitlines()[-1][:120],
                            repeat=False)
        except Exception:
            pass
        raise
