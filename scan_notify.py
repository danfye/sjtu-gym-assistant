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
DEEP_SWEEP_MIN = float(os.environ.get('DEEP_SWEEP_MIN', '30'))   # 每 N 分钟一轮全量核对比对粗筛
GATE = {'trust': True}             # 粗筛判据一旦被证伪就自动退回每轮全查
QUOTA_RETRY_S = float(os.environ.get('QUOTA_RETRY_S', '900'))   # 额度按滚动窗口恢复，别一睡到次日

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
            # 飞书自定义机器人普遍配了「自定义关键词」，标题必须带上「健身房」，
            # 否则消息会被 "Key Words Not Found" 静默退回（2026-10-25 实测）。
            res = send_notifications(f'健身房 [{event_type}] {message[:40]}', message, cfg)
            bad = [f'{k}: {v[:70]}' for k, v in (res or {}).items() if 'success' not in str(v).lower()]
            if bad:
                log('webhook 被退回 —— ' + ' | '.join(bad))
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


def sample_slot(cookie: str, date_str: str, hour: int, session=None):
    """采一次某个(日期,整点)的状态，返回 ('open'|'closed'|'unknown', 描述)。

    'unknown' 专指请求被限流或拿不到排期数据 —— 这不能算“空位消失”。
    2026-10-21 晚就是因为把 unknown 当成 not-open，压掉了六个真实窗口。
    """
    vm = VENUE_MAP['stu_gym']
    s = session or requests
    ids, msg = raw_date_ids(cookie, date_str, s)
    if '每日请求超过限制' in msg or '请求过于频繁' in msg:
        return 'unknown', msg
    date_id = ids.get(date_str)
    if not date_id:
        return 'unknown', msg or '未取到 dateId'
    price_lists, msg2 = raw_situation(cookie, date_str, date_id, s)
    if not price_lists or not price_lists[0]:
        return 'unknown', msg2 or '排期数据为空'
    row = hour - 7
    if row < 0 or row >= len(price_lists[0]):
        return 'unknown', f'第 {hour} 点不在矩阵内'
    cell = price_lists[0][row]
    if not cell:
        return 'unknown', '该格无数据'
    desc = f"status={cell.get('status')} count={cell.get('count')}"
    return ('open' if str(cell.get('status')) == '0' else 'closed'), desc


def probe_streak(cookie: str, date_str: str, hour: int, need: int = 3, gap: float = 4.0):
    """命中后再采几枪，把结果分成三档。

    confirmed：累计采到 need 次可约（中间允许夹着售罄/限流），可以直接喊人。
    seen：采到过可约，但没采够连续确认（多为站点限流占用了采样窗口）—— 仍然要喊，
           漏报的代价远高于多弹一次。
    blink：至少 2 次成功采到并且都不是可约，判为空位一闪即灭，不喊。

    返回 (level, 采样成功次数, 可约次数, 最后一次描述)。
    """
    s = requests.Session()
    opens = ok = 0
    last = '未采到样本'
    closes = 0
    for i in range(need * 3):        # 最多 3 倍尝试，容忍其中的 unknown
        outcome, last = sample_slot(cookie, date_str, hour, s)
        if outcome == 'open':
            opens += 1
            ok += 1
        elif outcome == 'closed':
            closes += 1
            ok += 1
        if opens >= need:
            return 'confirmed', ok, opens, last
        if closes >= 2 and opens == 0:      # 一次可约都没采到才判抖动
            # 以前这里不看 opens，2 次可约 + 2 次售罄会被判成 blink 压掉：
            # 2026-10-27 11:59 的 10-27 14:00-15:00 就是这么漏的（真实空位，当天下午能练）。
            return 'blink', ok, opens, last
        time.sleep(gap if outcome != 'unknown' else gap / 2)
    level = 'seen' if opens else 'blink'
    return level, ok, opens, last


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


def is_full_state(cookie: str, date_str: str, session=None):
    """一次请求拿到全部约 8 个日期的 {dateId, isAll, open}。

    queryFieldReserveSituationIsFull 每条记录自带 isAll（true=这天全满）和 open，而
    queryFieldSituation 才是每天要打上千次才会撞到的那个配额。所以用它当粗筛：
    isAll=true 的日期直接跳过排期请求，一轮只花 1 个请求就能覆盖所有日期。
    返回 ({日期: {...}}, 服务端msg)。
    """
    vm = VENUE_MAP['stu_gym']
    s = session or requests
    try:
        r = s.post(f"{API_BASE}/manage/fieldDetail/queryFieldReserveSituationIsFull",
                   json={"date": date_str, "feildType": vm['venTypeId'], "id": vm['venueId']},
                   headers=_headers(cookie), timeout=8)
        j = r.json()
        data = j.get('data') or []
        out = {}
        for x in data:
            if x.get('date') and x.get('dateId'):
                out[x['date']] = {'dateId': x['dateId'], 'isAll': x.get('isAll'), 'open': x.get('open')}
        return out, str(j.get('msg') or '')
    except Exception as exc:
        return {}, f'{type(exc).__name__} {str(exc)[:60]}'


def scan_once(cookie: str, targets, session, deep: bool = True):
    """一轮请求数 = 1（粗筛）+ 需要看排期的日期数；deep=True 时每个日期都看排期。

    返回 (found, dead, all_empty, real_data, throttle_msg)。throttle_msg 非空说明这一轮是被
    限流挡住的，上层据此退避，而不是当成“会话失效”去登录（登录一次要发几十个请求）。
    """
    vm = VENUE_MAP['stu_gym']
    found = {}
    statuses = []
    throttle = ''
    flags = {}
    dates = sorted({d for d, _ in targets})
    ids, msg = is_full_state(cookie, dates[0], session)
    if '每日请求超过限制' in msg or '请求过于频繁' in msg:
        return {}, False, True, False, msg
    for d, v in ids.items():
        flags[d] = v
    skipped = 0
    by_date = {}
    for date_str, slot in targets:
        by_date.setdefault(date_str, []).append(slot)
    for date_str in sorted(by_date):
        slots = by_date[date_str]
        keys = [f'{date_str} {s}' for s in slots]
        info = flags.get(date_str) or {}
        date_id = info.get('dateId')
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
        # 粗筛：isAll 明确为 true 就认定这天满，省掉一次 queryFieldSituation。
        # 判据只认 false —— 字段缺失/为 null 一律回退到查排期，宁可多打一个请求也别漏。
        if not deep and GATE['trust'] and info.get('isAll') is True:
            for k in keys:
                found[k] = []
            log(f'  - {keys[0]}: 空 (粗筛 isAll=true，未打排期接口)')
            statuses.append('full')
            skipped += 1
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
        if day_hit and info.get('isAll') is True:
            GATE['trust'] = False
            log(f'  [flag_mismatch] {date_str} 粗筛 isAll=true 但排期里有位 —— 粗筛不可信，退回每轮全查')
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


def apply_excludes(got):
    """按 EXCLUDE_DATES（逗号分隔，支持完整日期或 YYYY-MM-DD 前缀）剔掉已经抢到/不要的日期。

    请求数跟日期数成正比，所以「这几天已经有了」这件事能直接换成别的日期的轮询频率。
    """
    raw = os.environ.get('EXCLUDE_DATES', '').strip()
    if not raw or not got:
        return got
    bad = {x.strip() for x in raw.split(',') if x.strip()}
    kept = [(d, t) for d, t in got if d not in bad]
    if len(kept) != len(got):
        log('[排除日期] 去掉 ' + ' '.join(sorted(bad)) + f'，剩 {len({d for d, _ in kept})} 个日期')
    return kept or got


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
        got = apply_excludes(parse_targets(auto_targets()))
        if got:
            targets = got
    interval = max(15.0, args.interval)
    log('=== 扫描启动：只查不买 ===')
    log(('[按课表自动]' if auto else '[手动]') + '目标：' + ' | '.join(f'{d} {t}' for d, t in targets)
        + f'；每 {interval:.0f}s 一轮（粗筛 1 请求/轮，每 {DEEP_SWEEP_MIN:.0f} 分钟全量核对排期）')

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
            got = apply_excludes(parse_targets(auto_targets()))
            if got:
                targets = got
                log('新的一天，按课表更新目标：' + ' | '.join(f'{d} {t}' for d, t in targets))
        round_no += 1
        # 粗筛轮：1 个请求覆盖全部日期；每隔 DEEP_SWEEP_MIN 分钟插一轮全量排期核对，
        # 既校准 isAll 判据，也保证就算粗筛字段不可信也不会连着几小时漏位。
        deep_every = max(1, int(round(DEEP_SWEEP_MIN * 60 / interval)))
        deep = GATE['trust'] is False or (round_no - 1) % deep_every == 0
        log(f'第 {round_no} 轮' + ('（全量核对）' if deep and GATE['trust'] else ''))
        try:
            # 每轮轮换起点：短窗口限流只会打到一轮里排在后面的日期，固定顺序会让
            # 最后那一两个日期永远查不到
            rot = targets[round_no % len(targets):] + targets[:round_no % len(targets)]
            results, dead, all_empty, real_data, throttle = scan_once(cookie, rot, session, deep=deep)
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
            nap_s = min(wait_s, QUOTA_RETRY_S)
            log(f'配额已满（{throttle}），原计划睡到 {nxt:%m-%d %H:%M}，改为 {nap_s/60:.0f} 分钟后重探'
                '（限额更像滚动窗口，午夜不一定清账）')
            if time.time() - state.get('_quota_notify_ts', 0) > AUTH_NOTIFY_COOLDOWN_S:
                notify_progress('quota_exhausted',
                                f'今日请求配额已用尽，扫描休眠到 {nxt:%m-%d %H:%M}（约 {wait_s/3600:.1f} 小时后恢复）',
                                repeat=False)
                state['_quota_notify_ts'] = time.time()
                save_state(state)
            time.sleep(max(60.0, nap_s))
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
                wait_s = min(wait_s, QUOTA_RETRY_S)
                log(f'请求配额已满（{why}），{wait_s/60:.0f} 分钟后重探（限额按滚动窗口，午夜不一定清账）')
                last_q = state.get('_quota_notify_ts', 0)
                if time.time() - last_q > AUTH_NOTIFY_COOLDOWN_S:
                    notify_progress('quota_exhausted',
                                    f'学校接口请求配额用尽，扫描每 {wait_s/60:.0f} 分钟重探一次，恢复即继续',
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
                level, reads_n, hits_n, last = probe_streak(cookie, seats['date'], seats['hour'])
                if level == 'blink':
                    log(f'  {key}: 命中后复采到 {reads_n} 次，其中 {hits_n} 次可约（末次 {last}），判为抖动，不弹框')
                    with open(NOTIF_TXT, 'a', encoding='utf-8') as f:
                        f.write(f"{datetime.now().isoformat(timespec='seconds')} [slot_blink] {key} —— "
                                f"复采 {reads_n} 次仅 {hits_n} 次可约，末次 {last}，未弹告警\n")
                    state[key] = {'available': False, 'notified_ts': st.get('notified_ts', 0),
                                  'count': 0, 'blink': True}
                    save_state(state)
                    continue
                if level == 'confirmed':
                    head = f'连续 {hits_n} 次确认可约：{key}'
                else:
                    head = f'采到空位但站点限流未采够确认（{hits_n} 次可约）：{key}'
                notify_progress('slot_available', f'{head} —— {detail}，马上刷新页面下单', repeat=False)
                # 不再在告警后现场复采：那会阻塞主循环几十秒、额外打十几个请求，
                # 而且采不到数据时会被误读成“空位没了”。复验交给下一轮正常轮询。
                state[key] = {'available': True, 'notified_ts': time.time(), 'still_open_sent': False,
                              'count': len(items), 'seats': [it['site_name'] for it in items]}
            elif now_avail and st.get('notified_ts') and not st.get('still_open_sent'):
                # 告警后的下一轮仍然可约，才补一条“空位仍在”
                secs = time.time() - st.get('notified_ts', 0)
                with open(NOTIF_TXT, 'a', encoding='utf-8') as f:
                    f.write(f"{datetime.now().isoformat(timespec='seconds')} [slot_verify] {key} —— "
                            f"告警后 {secs:.0f} 秒的下一轮仍可约（{len(items)} 个）\n")
                notify_progress('slot_still_open', f'空位仍在（告警后 {secs:.0f} 秒复验通过）：{key}，还来得及', repeat=False)
                st['still_open_sent'] = True
                st['count'] = len(items)
                state[key] = st
                save_state(state)
            elif not now_avail and was_avail:
                log(f'  {key}: 刚才的空位已被占，状态复位')
                # 复验的否定结果也要落到 notifications.txt，否则同步通道只看到告警看不到下文
                secs = time.time() - st.get('notified_ts', time.time())
                with open(NOTIF_TXT, 'a', encoding='utf-8') as f:
                    f.write(f"{datetime.now().isoformat(timespec='seconds')} [slot_gone] {key} —— "
                            f"告警后 {secs:.0f} 秒的下一轮已不可约（窗口约 {secs:.0f} 秒）\n")
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
