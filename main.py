import os
import json
import logging
import argparse
from time import sleep
from datetime import datetime, timedelta
from typing import List, Optional, Dict, Tuple
import random
import requests
from concurrent.futures import ThreadPoolExecutor, as_completed

from venue_map import VENUE_MAP
from messages import send_notifications
from login_and_cookie import login_and_get_cookie
from cookie_store import save_cookie, load_cookie
from backend_api import query_date_ids, query_field_situation, build_available_matrix, choose_fields_by_priority, AuthError, get_server_time_offset, warmup_session
from crypto_wrap import wrap_confirm_body
from booking_state import date_range, is_date_booked, mark_date_booked, upcoming_dates, get_date_booking


def load_config():
    base = os.path.dirname(os.path.abspath(__file__))
    with open(os.path.join(base, 'config.json'), 'r', encoding='utf-8') as f:
        cfg = json.load(f)
    # env 替换
    def subst(v):
        if isinstance(v, str) and v.startswith('${') and v.endswith('}'):
            return os.environ.get(v[2:-1], '')
        return v
    cfg['login']['username'] = subst(cfg['login'].get('username'))
    cfg['login']['password'] = subst(cfg['login'].get('password'))
    cfg.setdefault('notifications', {})
    cfg['notifications']['FANGTANG_KEY'] = subst(cfg['notifications'].get('FANGTANG_KEY'))
    cfg['notifications']['FEISHU_WEBHOOK'] = subst(cfg['notifications'].get('FEISHU_WEBHOOK'))
    cfg['notifications']['FEISHU_SECRET'] = subst(cfg['notifications'].get('FEISHU_SECRET'))
    cfg['notifications']['WECOM_WEBHOOK'] = subst(cfg['notifications'].get('WECOM_WEBHOOK'))
    # 没给环境变量时，退回读本机 state/notify.json（state/ 不进版本库），
    # 免得每次重启扫描都要重新在命令行里带一遍 webhook。
    try:
        import notify_store
        notify_store.apply_local_overrides(cfg['notifications'])
    except Exception:
        pass
    return cfg


def resolve_target_date(target_date: Optional[str] = None, days_ahead: int = 7) -> str:
    if target_date:
        datetime.strptime(target_date, '%Y-%m-%d')
        return target_date
    return (datetime.now() + timedelta(days=int(days_ahead or 7))).strftime('%Y-%m-%d')


def normalize_date(date_text: str) -> str:
    return datetime.strptime(date_text, '%Y-%m-%d').strftime('%Y-%m-%d')


def resolve_scan_range(range_start: Optional[str], range_end: Optional[str],
                       scan_days: int = 7, include_today: bool = False) -> Tuple[str, str]:
    if range_start and range_end:
        start = normalize_date(range_start)
        end = normalize_date(range_end)
        if start > end:
            start, end = end, start
        return start, end

    dates = upcoming_dates(days=scan_days, include_today=include_today)
    if not dates:
        today = datetime.now().strftime('%Y-%m-%d')
        return today, today
    return dates[0], dates[-1]


def parse_time_ranges_arg(value: Optional[str]) -> Optional[List[str]]:
    if not value:
        return None
    parts = [x.strip() for x in value.replace('，', ',').split(',') if x.strip()]
    ranges = []
    for part in parts:
        h1, h2 = part.split('-', 1)
        h1, h2 = int(h1), int(h2)
        if h1 < 0 or h2 > 24 or h1 >= h2:
            raise ValueError(f"invalid time range: {part}")
        ranges.append(f"{h1}-{h2}")
    return ranges


def clone_tasks_with_time_ranges(tasks: List[dict], time_ranges: Optional[List[str]]) -> List[dict]:
    cloned = []
    for task in tasks:
        t = dict(task)
        if time_ranges:
            t['time_ranges'] = list(time_ranges)
        cloned.append(t)
    return cloned


def should_run_for_date(weekdays: List[int], schedule_date: Optional[str] = None) -> bool:
    # Python 周一=1 ... 周日=7
    if schedule_date:
        wd = datetime.strptime(schedule_date, '%Y-%m-%d').isoweekday()
    else:
        wd = datetime.now().isoweekday()
    return wd in weekdays


def check_task_availability(task: dict, cookie: str, target_date: str,
                            session: Optional[requests.Session] = None) -> Tuple[bool, str, List[dict]]:
    venue_key = task['venue_key']
    weekdays = task.get('weekdays', [])
    if not should_run_for_date(weekdays, target_date):
        return False, "skip", []

    vm = VENUE_MAP[venue_key]
    venTypeId, venueId = vm['venTypeId'], vm['venueId']
    display = vm.get('display', venue_key)
    s = session or requests

    date_ids = query_date_ids(cookie, venTypeId, venueId, target_date, session=s)
    date_id = date_ids.get(target_date)
    if not date_id:
        return False, f"{display} {target_date} dateId not found", []

    price_lists, field_ids = query_field_situation(cookie, target_date, venTypeId, venueId, date_id, session=s)
    if not price_lists or not field_ids:
        return False, f"{display} {target_date} field situation empty", []

    mat = build_available_matrix(price_lists)
    found: List[dict] = []
    for tr in task.get('time_ranges', []):
        try:
            h1, h2 = tr.split('-')
            h1, h2 = int(h1), int(h2)
        except Exception:
            logging.warning("invalid time range during check: %s", tr)
            continue
        for hour in range(h1, h2):
            row = hour - 7
            if row < 0 or row >= len(mat):
                continue
            for seat_idx, cell in enumerate(mat[row]):
                if cell == -1:
                    continue
                found.append({
                    "venue_key": venue_key,
                    "display": display,
                    "date": target_date,
                    "hour": hour,
                    "time": f"{hour}:00-{hour+1}:00",
                    "seat_idx": seat_idx,
                    "site_name": f"场地{seat_idx + vm['name_start_index']}",
                    "field_id": field_ids[seat_idx],
                    "price": cell.get('price'),
                    "has_sign": bool(cell.get('sign')),
                })
    msg = f"{display} {target_date} available={len(found)}"
    return bool(found), msg, found


def build_request_json(venTypeId, venueId, scheduleDate, fieldType, seat_idx, field_id, hour, name_start_index, cell):
    scheduleTime = f"{hour}:00-{hour+1}:00"
    subSitename = f"场地{seat_idx + name_start_index}"
    weekParam = str(datetime.strptime(scheduleDate, "%Y-%m-%d").weekday() + 1)
    space_item = {
        "count": 1,
        "venuePrice": str(cell.get('price')),
        "status": 1,
        "scheduleTime": scheduleTime,
        "subSitename": subSitename,
        "subSiteId": field_id,
        "tensity": "1",
        "venueNum": 1
    }
    sign = cell.get('sign')
    if sign:
        space_item['sign'] = sign
    return json.dumps({
        "venTypeId": venTypeId,
        "venueId": venueId,
        "fieldType": fieldType,
        "returnUrl": "https://sports.sjtu.edu.cn/#/paymentResult/1",
        "scheduleDate": scheduleDate,
        "week": weekParam,
        "spaces": [space_item],
        "tenSity": "紧张"
    }, ensure_ascii=False, separators=(',', ':'))


def run_task(task: dict, cookie: str, notification_cfg: dict, session: Optional[requests.Session] = None,
             prefetched: Optional[Dict[str, dict]] = None, parallel_confirm: Optional[int] = None,
             confirm_batches: int = 1, confirm_jitter_ms: int = 0, confirm_backoff_ms: int = 0,
             confirm_timeout_s: int = 6, target_date: Optional[str] = None):
    venue_key = task['venue_key']
    weekdays = task['weekdays']
    time_ranges = task['time_ranges']
    seat_priority = task['seat_priority']
    count_per_slot = int(task.get('count_per_slot', 2))

    # 默认目标日期：7 天后；watch/book 可通过 CLI 覆盖为指定日期。
    target_date = target_date or resolve_target_date()

    if not should_run_for_date(weekdays, target_date):
        logging.info("Skip task %s: target_date %s not in weekdays %s", venue_key, target_date, weekdays)
        return False, "skip"

    vm = VENUE_MAP[venue_key]
    venTypeId, venueId, fieldType, name_start_index = vm['venTypeId'], vm['venueId'], vm['fieldType'], vm['name_start_index']
    display = vm.get('display', f"{venue_key}-{fieldType}")

    # 需要拿目标日期的 dateId（接口返回的是一段日期列表）
    # 使用预取的 dateId / 场地信息（若可用）以降低首包延迟
    pre = (prefetched or {}).get(venue_key) if prefetched else None
    date_ids = {}
    if pre and pre.get('date_id'):
        date_ids = {target_date: pre['date_id']}
    else:
        date_ids = query_date_ids(cookie, venTypeId, venueId, target_date, session=session)
    date_id = date_ids.get(target_date)
    if not date_id:
        logging.info("dateId not ready for %s, stop after query_date_ids retries", target_date)
        return False, f"dateId not found for {target_date}"
    if pre and pre.get('price_lists') and pre.get('field_ids'):
        price_lists, field_ids = pre['price_lists'], pre['field_ids']
    else:
        price_lists, field_ids = query_field_situation(cookie, target_date, venTypeId, venueId, date_id, session=session)
    if not price_lists or not field_ids:
        logging.info("field situation empty for %s, brief retry", target_date)
        for _ in range(10):
            sleep(0.2)
            price_lists, field_ids = query_field_situation(cookie, target_date, venTypeId, venueId, date_id, session=session)
            if price_lists and field_ids:
                break
    mat = build_available_matrix(price_lists)

    def pick_indices_for_hour(hour: int) -> List[int]:
        row = hour - 7
        if row < 0 or row >= len(mat):
            return []
        if not seat_priority:
            any_avail = [i for i, cell in enumerate(mat[row]) if cell != -1]
            if not any_avail:
                return []
            random.shuffle(any_avail)
            idx_list = any_avail[:max(1, count_per_slot)]
        else:
            idx_list = choose_fields_by_priority(mat, hour, seat_priority, count_per_slot)
            if not idx_list:
                logging.info("%s %s %s no available by priority", target_date, hour, venue_key)
                any_avail = [i for i, cell in enumerate(mat[row]) if cell != -1]
                if not any_avail:
                    return []
                random.shuffle(any_avail)
                idx_list = any_avail[:max(1, count_per_slot)]
        if not seat_priority:
            idx_list.sort(key=lambda i: 0 if mat[row][i].get('sign') else 1)
        else:
            idx_list.sort(key=lambda i: (0 if mat[row][i].get('sign') else 1,))
        return idx_list

    def parse_resp(resp: requests.Response) -> Tuple[bool, int, str]:
        try:
            j = resp.json()
            code = int(j.get('code', -1))
            msg = j.get('msg') or j.get('message') or ''
            ok = (resp.status_code == 200 and code == 0)
            return ok, code, (msg or str(j)[:200])
        except Exception:
            txt = (resp.text or '')[:200]
            ok = (resp.status_code == 200 and '"code":0' in txt)
            return ok, -1, txt

    def try_book_hour(hour: int) -> Optional[dict]:
        idx_list = pick_indices_for_hour(hour)
        if not idx_list:
            return None

        url = "https://sports.sjtu.edu.cn/venue/personal/ConfirmOrder"
        s = session or requests
        row = hour - 7

        def submit_one(seat_idx: int) -> Tuple[int, requests.Response, str]:
            jitter = max(0, int(confirm_jitter_ms or 0)) / 1000.0
            if jitter > 0:
                sleep(random.uniform(0, jitter))
            cell = mat[row][seat_idx]
            body_json = build_request_json(
                venTypeId, venueId, target_date, fieldType,
                seat_idx, field_ids[seat_idx], hour, name_start_index, cell,
            )
            enc_body, enc_headers = wrap_confirm_body(body_json, cookie)
            r = s.post(url, headers=enc_headers, data=enc_body, timeout=max(3, int(confirm_timeout_s or 6)))
            return seat_idx, r, body_json

        max_workers_base = parallel_confirm if isinstance(parallel_confirm, int) and parallel_confirm > 0 else max(1, count_per_slot)
        remaining = list(idx_list)
        batches = max(1, int(confirm_batches or 1))
        backoff_ms = max(0, int(confirm_backoff_ms or 0))
        too_freq_seen = False
        base_backoff_ms = backoff_ms if backoff_ms > 0 else 80
        dyn_backoff_ms = base_backoff_ms

        for _ in range(batches):
            if not remaining:
                break
            max_workers = 1 if too_freq_seen else max_workers_base
            seats = remaining[:max_workers]
            remaining = remaining[max_workers:]

            with ThreadPoolExecutor(max_workers=max_workers) as ex:
                futures = [ex.submit(submit_one, seat_idx) for seat_idx in seats]
                batch_total = len(futures)
                booked_conflict = 0
                for fut in as_completed(futures):
                    try:
                        seat_idx, r, _ = fut.result()
                        ok, code, msg = parse_resp(r)
                        logging.info("ConfirmOrder %s %s seat=%s code=%s", target_date, hour, seat_idx, r.status_code)
                        if ok:
                            msg_out = f"Booked: {display} {target_date} {hour}:00 场地#{seat_idx + name_start_index}"
                            logging.info(msg_out)
                            return {
                                "status": "booked",
                                "hour": hour,
                                "seat": seat_idx + name_start_index,
                                "message": msg_out,
                                "source": "confirm_order",
                            }

                        body_text = (r.text or '')
                        logging.warning("ConfirmOrder failed body: %s", body_text[:500])
                        if code == 301:
                            msg_out = f"Already booked: {display} {target_date} {hour}:00"
                            logging.info(msg_out)
                            return {
                                "status": "existing",
                                "hour": hour,
                                "message": msg_out,
                                "source": "server_existing_booking",
                            }
                        if code == 500 and (('已被预定' in body_text) or ('already booked' in body_text.lower())):
                            booked_conflict += 1
                        text_for_check = (msg or '') + ' ' + body_text
                        if code == 1002:
                            too_freq_seen = True
                        if r.status_code == 429 or (code in (429, 500) and (('频繁' in text_for_check) or ('too frequent' in text_for_check.lower()))):
                            too_freq_seen = True
                    except Exception as e:
                        logging.warning("ConfirmOrder error: %s", e)
            try:
                if batch_total and booked_conflict / batch_total >= 0.6:
                    remaining = []
            except Exception:
                pass
            if too_freq_seen and remaining:
                sleep(random.uniform(dyn_backoff_ms * 0.7, dyn_backoff_ms * 1.3) / 1000.0)
                dyn_backoff_ms = min(1200, max(base_backoff_ms, dyn_backoff_ms * 2))
        return None

    def finalize_slot_records(slot_records: List[dict]) -> Tuple[bool, str]:
        if not slot_records:
            return False, "no success"
        combined_msg = " | ".join(item["message"] for item in slot_records)
        hours = [item.get("hour") for item in slot_records if item.get("hour") is not None]
        seats = [item.get("seat") for item in slot_records if item.get("seat") is not None]
        source = "contiguous_booking" if len(slot_records) > 1 else slot_records[-1].get("source", "confirm_order")
        extra = {"hours": hours}
        if seats:
            extra["seats"] = seats
        if len(hours) == 1:
            extra["hour"] = hours[0]
        if len(seats) == 1:
            extra["seat"] = seats[0]
        mark_date_booked(
            target_date,
            combined_msg,
            source=source,
            venue_key=venue_key,
            extra=extra,
        )
        if any(item.get("status") == "booked" for item in slot_records):
            send_res = send_notifications('抢到健身房了!', combined_msg, notification_cfg)
            if send_res:
                logging.info("Notify result: %s", send_res)
        return True, combined_msg

    success_any = False
    for tr in time_ranges:
        try:
            h1, h2 = tr.split('-')
            h1, h2 = int(h1), int(h2)
        except Exception:
            logging.warning("invalid time range: %s", tr)
            continue
        for hour in range(h1, h2):
            first_record = try_book_hour(hour)
            if not first_record:
                continue
            slot_records = [first_record]
            next_record = try_book_hour(hour + 1)
            if next_record:
                slot_records.append(next_record)
            has_new_booking = any(item.get("status") == "booked" for item in slot_records)
            if first_record.get("status") == "existing" and not has_new_booking and len(slot_records) == 1:
                msg_out = f"{first_record['message']} | Adjacent {hour + 1}: unavailable"
                logging.info(msg_out)
                return False, msg_out
            success_any = True
            return finalize_slot_records(slot_records)

    return success_any, ("no success" if not success_any else "done")


def main():
    base = os.path.dirname(os.path.abspath(__file__))
    logging.basicConfig(
        filename=os.path.join(base, 'run.log'),
        level=logging.INFO,
        format='%(asctime)s %(levelname)s %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S'
    )
    parser = argparse.ArgumentParser(description='Hybrid SJTU Sports Booker')
    parser.add_argument('--mode', choices=['login', 'book', 'all', 'watch', 'check', 'scan7'], default='all', help='login: 仅登录保存cookie; book: 使用已保存cookie预约; all: 登录后立刻预约; watch: 单日回流抢占; check: 只检测空位不下单; scan7: 每5分钟扫描指定日期范围并每天最多预约1个空位')
    parser.add_argument('--browser', choices=['firefox', 'chrome', 'edge'], help='登录使用的浏览器；也可在 config.json 写 browser 或设置 BROWSER 环境变量')
    parser.add_argument('--target-date', help='预约/监控的目标日期，格式 YYYY-MM-DD；不填则使用今天 + --days-ahead')
    parser.add_argument('--days-ahead', type=int, default=7, help='未指定 --target-date 时预约几天后的场次（默认7天后）')
    parser.add_argument('--time-ranges', help='覆盖配置中的预约/监控时段，逗号分隔，如 14-15 或 14-15,20-21')
    parser.add_argument('--range-start', help='scan7模式扫描起始日期，格式 YYYY-MM-DD')
    parser.add_argument('--range-end', help='scan7模式扫描结束日期，格式 YYYY-MM-DD')
    parser.add_argument('--at', help='book模式等待直到指定时刻再开始，格式 HH:MM:SS (本地时间)')
    parser.add_argument('--precheck-seconds', type=int, default=3, help='在 --at 指定时间前多少秒进行 cookie 验证与必要的重登（默认 3s）')
    parser.add_argument('--use-server-time', action='store_true', help='等待逻辑基于服务器时间（通过HTTP Date校准），降低本地时钟偏差影响')
    parser.add_argument('--soft-poll-window', type=float, default=0.0, help='在开抢前的最后N秒轻量轮询dateId/场地，范围建议 2~10 秒')
    parser.add_argument('--soft-poll-interval-ms', type=int, default=600, help='轻量轮询间隔(毫秒)，建议 300~1000ms')
    parser.add_argument('--poll-interval-ms', type=int, default=300000, help='watch/scan7模式轮询间隔(毫秒)，默认300000ms=5分钟')
    parser.add_argument('--watch-duration-min', type=float, default=0.0, help='watch模式最多监控多少分钟；0表示不按时长停止')
    parser.add_argument('--watch-until', help='watch模式监控到本地时间 HH:MM:SS；若该时间已过则按明天计算')
    parser.add_argument('--watch-cookie-check-min', type=float, default=5.0, help='watch模式每隔多少分钟验证一次cookie，失效则自动重登')
    parser.add_argument('--keep-watching', action='store_true', help='watch模式抢到后继续监控；默认抢到一次后停止')
    parser.add_argument('--scan-days', type=int, default=7, help='scan7模式在未指定起止日期时扫描多少天，默认7天')
    parser.add_argument('--include-today', action='store_true', help='scan7模式在未指定起止日期时是否包含今天；默认从明天开始')
    parser.add_argument('--once', action='store_true', help='scan7模式只跑一轮后退出')
    # 更激进的默认值，贴近旧脚本的“尽快发出首包”策略
    parser.add_argument('--parallel-confirm', type=int, default=1, help='并发下单请求的最大数量（默认1）')
    parser.add_argument('--confirm-batches', type=int, default=1, help='同一小时的批次尝试次数（默认1批）')
    parser.add_argument('--confirm-jitter-ms', type=int, default=0, help='每个请求前的抖动延迟(毫秒)，默认0ms')
    parser.add_argument('--confirm-backoff-ms', type=int, default=0, help='检测到频率受限(500/频繁)后的退避时间(毫秒)，默认0ms')
    parser.add_argument('--confirm-timeout-s', type=int, default=6, help='ConfirmOrder 单次请求超时时长（秒），默认6s')
    args = parser.parse_args()

    cfg = load_config()
    target_date = resolve_target_date(args.target_date, args.days_ahead)
    try:
        time_ranges_override = parse_time_ranges_arg(args.time_ranges)
    except Exception as e:
        print(f"--time-ranges 格式错误：{e}", flush=True)
        return
    configured_tasks = clone_tasks_with_time_ranges(list(cfg.get('tasks', [])), time_ranges_override)
    headless = bool(cfg.get('headless', True))
    browser = args.browser or cfg.get('browser') or os.environ.get('BROWSER')
    username = cfg['login']['username'] or os.environ.get('JACCOUNT_USERNAME', '')
    password = cfg['login']['password'] or os.environ.get('JACCOUNT_PASSWORD', '')
    notification_cfg = cfg.get('notifications', {})

    def do_login_and_save(require_valid: bool = False):
        t0 = datetime.now()
        # 执行登录并验证 cookie
        max_attempts = 3
        last_cookie = None
        for attempt in range(1, max_attempts + 1):
            cookie = login_and_get_cookie(username, password, headless=headless, browser=browser)
            last_cookie = cookie
            # 验证 cookie 是否被服务端接受
            from backend_api import validate_cookie
            ok = validate_cookie(cookie)
            dt = (datetime.now() - t0).total_seconds()
            if ok:
                path = save_cookie(cookie)
                logging.info('Login done in %.2fs, cookie saved to %s (attempt %s)', dt, path, attempt)
                print(f'Login OK. Took {dt:.2f}s. Cookie saved to {path}', flush=True)
                suggest = max(5, int(dt) + 12)  # 耗时 + 缓冲
                print(f'建议在目标时间点前约 {suggest}s 开始登录', flush=True)
                return cookie
            else:
                logging.warning('Login attempt %s produced cookie that failed validation; retrying', attempt)
                print(f'Login attempt {attempt} cookie validation failed, retrying...', flush=True)
                # 指数退避短等待
                sleep(0.5 * attempt)
        if require_valid:
            logging.warning('All login attempts failed validation; no cookie returned in strict mode')
            print('Login failed: cookie validation failed after retries.', flush=True)
            return None
        # 若所有尝试都失败，仍保存最后一次 cookie 以便人工检查
        path = save_cookie(last_cookie)
        logging.warning('All login attempts failed validation; saved last cookie to %s for inspection', path)
        print('Login completed but cookie validation failed after retries; saved last cookie for inspection', flush=True)
        return last_cookie

    def do_book_with_cookie(cookie: str, session: Optional[requests.Session] = None,
                            prefetched: Optional[Dict[str, dict]] = None,
                            target_date_arg: Optional[str] = None,
                            tasks_override: Optional[List[dict]] = None,
                            quiet: bool = False):
        tasks = list(tasks_override if tasks_override is not None else configured_tasks)
        if not tasks:
            return []
        # 跨场馆并行，提升首发命中率；限制并行度避免触发全局频控
        max_workers = min(3, len(tasks))
        results: List[Tuple[bool, str]] = [(
            False, 'pending') for _ in range(len(tasks))]
        with ThreadPoolExecutor(max_workers=max_workers) as ex:
            future_map = {}
            for idx, t in enumerate(tasks):
                # 任务级覆盖并发/批次/抖动/退避/超时；无则回落到 CLI 参数
                t_parallel = t.get('parallel_confirm')
                if not (isinstance(t_parallel, int) and t_parallel > 0):
                    t_parallel = (args.parallel_confirm if args.parallel_confirm and args.parallel_confirm > 0 else None)
                t_batches = int(t.get('confirm_batches', args.confirm_batches or 1))
                t_jitter = int(t.get('confirm_jitter_ms', args.confirm_jitter_ms or 0))
                t_backoff = int(t.get('confirm_backoff_ms', args.confirm_backoff_ms or 0))
                t_timeout = int(t.get('confirm_timeout_s', args.confirm_timeout_s or 4))
                fut = ex.submit(
                    run_task,
                    t,
                    cookie,
                    notification_cfg,
                    session,
                    prefetched,
                    t_parallel,
                    t_batches,
                    t_jitter,
                    t_backoff,
                    t_timeout,
                    target_date_arg,
                )
                future_map[fut] = idx
            for fut in as_completed(future_map):
                idx = future_map[fut]
                try:
                    ok, msg = fut.result()
                except Exception as e:
                    ok, msg = False, f"error: {e}"
                results[idx] = (ok, msg)
        success = sum(1 for ok, _ in results if ok)
        total = len(results)
        if (not quiet) or success:
            date_part = f" {target_date_arg}" if target_date_arg else ""
            print(f"Booked{date_part}. Success {success}/{total}.", flush=True)
        return results

    def compute_deadline():
        deadlines = []
        now0 = datetime.now()
        if args.watch_duration_min and args.watch_duration_min > 0:
            deadlines.append(now0 + timedelta(minutes=float(args.watch_duration_min)))
        if args.watch_until:
            try:
                end_clock = datetime.strptime(args.watch_until, '%H:%M:%S').time()
                end_dt = datetime.combine(now0.date(), end_clock)
                if end_dt <= now0:
                    end_dt += timedelta(days=1)
                deadlines.append(end_dt)
            except ValueError:
                print('--watch-until 格式应为 HH:MM:SS，例如 22:30:00', flush=True)
                return None, False
        return (min(deadlines) if deadlines else None), True

    if args.mode == 'login':
        try:
            cookie = do_login_and_save()
            ok = bool(cookie)
            title = '登录完成' if ok else '登录失败'
            content = '登录成功并保存 Cookie' if ok else '未能获取有效 Cookie，请查看 run.log'
            send_res = send_notifications(title, content, notification_cfg)
            if send_res:
                logging.info('Notify result: %s', send_res)
        except Exception as e:
            logging.exception('Login failed: %s', e)
            send_res = send_notifications('登录失败', str(e), notification_cfg)
            if send_res:
                logging.info('Notify result: %s', send_res)
        return

    if args.mode == 'book':
        cookie, saved_at = load_cookie()
        if not cookie:
            msg = '没有找到已保存的 cookie，请先运行 --mode login'
            print(msg)
            send_res = send_notifications('预约失败', msg, notification_cfg)
            if send_res:
                logging.info('Notify result: %s', send_res)
            return

        # 创建可复用会话，减少TLS/握手延迟
        session = requests.Session()
        # 服务器时间偏差
        server_offset = 0.0
        if args.use_server_time:
            try:
                server_offset = get_server_time_offset(session=session)
                logging.info('Server time offset measured: %.3fs', server_offset)
            except Exception:
                server_offset = 0.0

        def now_ref():
            return datetime.now() + timedelta(seconds=server_offset)

        prefetched: Dict[str, dict] = {}

        if args.at:
            today = datetime.now().strftime('%Y-%m-%d')
            target = datetime.strptime(f"{today} {args.at}", '%Y-%m-%d %H:%M:%S')
            precheck_seconds = int(args.precheck_seconds or 3)
            precheck_time = target - timedelta(seconds=precheck_seconds)
            # 等待直到 precheck_time 到达
            while True:
                now = now_ref()
                if now >= precheck_time:
                    break
                sleep(min(0.2, max(0.01, (precheck_time - now).total_seconds())))
            # 在 precheck_time 做一次轻量验证，必要时重登（不会改 crontab）
            from backend_api import validate_cookie
            try:
                valid = validate_cookie(cookie)
            except Exception:
                valid = False
            if not valid:
                logging.info('Precheck: cookie invalid, performing immediate re-login')
                cookie = do_login_and_save()
            # 预热 TLS 连接
            try:
                warmup_session(session)
            except Exception:
                pass
            # 轻量轮询（可选）：最后 N 秒，低频探测 dateId/场地，降低开抢首包延迟
            window = float(args.soft_poll_window or 0.0)
            interval_ms = int(args.soft_poll_interval_ms or 600)
            if window > 0:
                start_soft = target - timedelta(seconds=window)
                while True:
                    now = now_ref()
                    if now >= target:
                        break
                    if now >= start_soft:
                        # 对所有当日会用到的场馆尝试拉取一次 dateId 与场地信息
                        for t in configured_tasks:
                            venue_key = t['venue_key']
                            vm = VENUE_MAP[venue_key]
                            venTypeId, venueId = vm['venTypeId'], vm['venueId']
                            try:
                                date_ids = query_date_ids(cookie, venTypeId, venueId, target_date, session=session)
                                date_id = date_ids.get(target_date)
                                if not date_id:
                                    continue
                                price_lists, field_ids = query_field_situation(cookie, target_date, venTypeId, venueId, date_id, session=session)
                                if price_lists and field_ids:
                                    prefetched[venue_key] = {
                                        'date_id': date_id,
                                        'price_lists': price_lists,
                                        'field_ids': field_ids,
                                    }
                            except Exception:
                                continue
                    sleep(max(0.05, min(1.0, interval_ms / 1000.0)))
            # 等待直到 target 开始
            while True:
                now = now_ref()
                if now >= target:
                    break
                sleep(min(0.2, max(0.01, (target - now).total_seconds())))

        try:
            results = do_book_with_cookie(cookie, session=session, prefetched=prefetched if prefetched else None, target_date_arg=target_date)
        except AuthError as e:
            logging.info('AuthError detected during booking: %s. Will re-login and retry once.', e)
            # 立即重新登录一次，刷新 cookie 后重试全部任务一遍
            cookie = do_login_and_save()
            results = do_book_with_cookie(cookie, session=session, prefetched=None, target_date_arg=target_date)
        except Exception as e:
            logging.exception('Booking failed: %s', e)
            send_res = send_notifications('预约失败', str(e), notification_cfg)
            if send_res:
                logging.info('Notify result: %s', send_res)
            return
        # 发送通知汇总
        success = sum(1 for ok, _ in results if ok)
        total = len(results)
        lines = []
        for t, (ok, msg) in zip(configured_tasks, results):
            name = t.get('venue_key')
            status = '跳过' if msg == 'skip' else ('成功' if ok else '失败')
            lines.append(f"- {name}: {status} {msg}")
        content = "\n".join(lines) if lines else "无任务"
        send_res = send_notifications(f'预约结果 {success}/{total}', content, notification_cfg)
        if send_res:
            logging.info('Notify result: %s', send_res)
        return

    if args.mode == 'check':
        active_tasks = [
            t for t in configured_tasks
            if should_run_for_date(t.get('weekdays', []), target_date)
        ]
        if not active_tasks:
            print(f"{target_date} 没有匹配 config.json 中 weekdays 的任务，check 不启动。", flush=True)
            return

        cookie, saved_at = load_cookie()
        if not cookie:
            if not username or not password:
                print('没有找到已保存的 cookie，且未配置 JACCOUNT_USERNAME/JACCOUNT_PASSWORD，无法自动登录。', flush=True)
                return
            print('没有找到已保存 Cookie，开始登录...', flush=True)
            cookie = do_login_and_save(require_valid=True)
            if not cookie:
                print('登录失败，无法检测空位。', flush=True)
                return

        from backend_api import validate_cookie
        try:
            valid = validate_cookie(cookie, 'stu_gym', target_date)
        except Exception:
            valid = False
        if not valid:
            if not username or not password:
                print('Cookie 已失效，但未配置账号密码，无法检测空位。', flush=True)
                return
            print('Cookie 已失效，开始重新登录...', flush=True)
            cookie = do_login_and_save(require_valid=True)
            if not cookie:
                print('重新登录失败，无法检测空位。', flush=True)
                return

        session = requests.Session()
        total_found = 0
        for t in active_tasks:
            ok, msg, items = check_task_availability(t, cookie, target_date, session=session)
            total_found += len(items)
            print(msg, flush=True)
            if items:
                for item in items:
                    print(
                        f"  可约：{item['display']} {item['date']} {item['time']} {item['site_name']} "
                        f"price={item['price']} sign={'yes' if item['has_sign'] else 'no'}",
                        flush=True,
                    )
        print(f"检测完成：共发现 {total_found} 个匹配时段空位；未下单。", flush=True)
        return

    if args.mode == 'scan7':
        scan_days = max(1, int(args.scan_days or 7))
        interval_s = max(60.0, int(args.poll_interval_ms or 300000) / 1000.0)
        deadline, ok_deadline = compute_deadline()
        if not ok_deadline:
            return
        range_start, range_end = resolve_scan_range(
            args.range_start,
            args.range_end,
            scan_days=scan_days,
            include_today=args.include_today,
        )

        cookie, saved_at = load_cookie()
        if not cookie:
            if not username or not password:
                print('没有找到已保存的 cookie，且未配置 JACCOUNT_USERNAME/JACCOUNT_PASSWORD，无法自动登录。', flush=True)
                return
            print('没有找到已保存 Cookie，开始登录...', flush=True)
            cookie = do_login_and_save(require_valid=True)
            if not cookie:
                print('登录失败，无法进入指定日期范围扫描。', flush=True)
                return

        session = requests.Session()
        try:
            warmup_session(session)
        except Exception:
            pass

        end_text = deadline.strftime('%Y-%m-%d %H:%M:%S') if deadline else ('本轮后退出' if args.once else '手动停止')
        print(f"开始健身房范围扫描：{range_start} ~ {range_end}；间隔 {interval_s:.0f}s；结束：{end_text}", flush=True)

        from backend_api import validate_cookie
        loop_count = 0
        not_ready_skip_dates = set()
        while True:
            now = datetime.now()
            if deadline and now >= deadline:
                print(f"scan7 已到结束时间：{deadline.strftime('%Y-%m-%d %H:%M:%S')}", flush=True)
                return

            loop_count += 1
            dates = date_range(range_start, range_end)
            print(f"\n[{datetime.now().strftime('%H:%M:%S')}] 第 {loop_count} 轮扫描：{', '.join(dates)}", flush=True)

            try:
                valid = validate_cookie(cookie, 'stu_gym', dates[-1] if dates else None)
            except Exception:
                valid = False
            if not valid:
                if not username or not password:
                    print('Cookie 已失效，但未配置账号密码，scan7 停止。', flush=True)
                    return
                print('Cookie 已失效，开始重新登录...', flush=True)
                cookie = do_login_and_save(require_valid=True)
                if not cookie:
                    print('重新登录失败，scan7 停止。', flush=True)
                    return
                session = requests.Session()

            cycle_success = 0
            cycle_skipped = 0
            for day in dates:
                if day in not_ready_skip_dates:
                    print(f"  跳过 {day}：本次任务中该日期已出现 dateId not-ready，后续轮次不再尝试", flush=True)
                    cycle_skipped += 1
                    continue

                if is_date_booked(day):
                    booking = get_date_booking(day) or {}
                    detail = booking.get('message') or booking.get('detail') or ''
                    print(f"  跳过 {day}：本地状态已约 {detail}", flush=True)
                    cycle_skipped += 1
                    continue

                day_tasks = [
                    t for t in configured_tasks
                    if should_run_for_date(t.get('weekdays', []), day)
                ]
                if not day_tasks:
                    print(f"  跳过 {day}：没有匹配 weekdays 的健身房任务", flush=True)
                    cycle_skipped += 1
                    continue

                print(f"  扫描 {day}...", flush=True)
                try:
                    results = do_book_with_cookie(
                        cookie,
                        session=session,
                        target_date_arg=day,
                        tasks_override=day_tasks,
                        quiet=True,
                    )
                except AuthError as e:
                    logging.info('scan7 AuthError: %s. Will re-login and retry current cycle later.', e)
                    if not username or not password:
                        print('预约时检测到登录失效，但未配置账号密码，scan7 停止。', flush=True)
                        return
                    print('预约时检测到登录失效，开始重新登录...', flush=True)
                    cookie = do_login_and_save(require_valid=True)
                    if not cookie:
                        print('重新登录失败，scan7 停止。', flush=True)
                        return
                    session = requests.Session()
                    break
                except Exception as e:
                    logging.exception('scan7 day failed: %s', e)
                    print(f"  {day} 扫描异常：{e}", flush=True)
                    continue

                success = sum(1 for ok, _ in results if ok)
                if success:
                    cycle_success += success
                    for ok, msg in results:
                        if ok:
                            print(f"  {day} 已记录预约状态：{msg}", flush=True)
                else:
                    messages = [msg for _, msg in results if isinstance(msg, str)]
                    if any("dateId not found" in msg for msg in messages):
                        not_ready_skip_dates.add(day)
                        cycle_skipped += 1
                        print(f"  跳过 {day}：query_date_ids not-ready 达到上限，当前任务后续轮次不再尝试这一天", flush=True)
                    else:
                        print(f"  {day} 暂无匹配空位", flush=True)

            print(
                f"[{datetime.now().strftime('%H:%M:%S')}] 本轮结束：成功/已确认 {cycle_success} 天，"
                f"跳过 {cycle_skipped} 天，累计 not-ready 跳过 {len(not_ready_skip_dates)} 天。",
                flush=True,
            )

            if args.once:
                return

            sleep_s = interval_s
            if deadline:
                sleep_s = min(sleep_s, max(0.0, (deadline - datetime.now()).total_seconds()))
            if sleep_s > 0:
                sleep(sleep_s)

    if args.mode == 'watch':
        active_tasks = [
            t for t in configured_tasks
            if should_run_for_date(t.get('weekdays', []), target_date)
        ]
        if not active_tasks:
            print(f"{target_date} 没有匹配 config.json 中 weekdays 的任务，watch 不启动。", flush=True)
            return
        if is_date_booked(target_date):
            print(f"{target_date} 本地日历状态已约；本次仍会尝试补后面紧挨的一小时，做到连续两小时。", flush=True)

        cookie, saved_at = load_cookie()
        if not cookie:
            if not username or not password:
                print('没有找到已保存的 cookie，且未配置 JACCOUNT_USERNAME/JACCOUNT_PASSWORD，无法自动登录。', flush=True)
                return
            print('没有找到已保存 Cookie，开始登录...', flush=True)
            cookie = do_login_and_save(require_valid=True)
            if not cookie:
                print('登录失败，无法进入回流监控。', flush=True)
                return

        session = requests.Session()
        try:
            warmup_session(session)
        except Exception:
            pass

        deadlines = []
        now0 = datetime.now()
        if args.watch_duration_min and args.watch_duration_min > 0:
            deadlines.append(now0 + timedelta(minutes=float(args.watch_duration_min)))
        if args.watch_until:
            try:
                end_clock = datetime.strptime(args.watch_until, '%H:%M:%S').time()
                end_dt = datetime.combine(now0.date(), end_clock)
                if end_dt <= now0:
                    end_dt += timedelta(days=1)
                deadlines.append(end_dt)
            except ValueError:
                print('--watch-until 格式应为 HH:MM:SS，例如 22:30:00')
                return
        deadline = min(deadlines) if deadlines else None

        interval_s = max(1.0, int(args.poll_interval_ms or 5000) / 1000.0)
        cookie_check_s = max(60.0, float(args.watch_cookie_check_min or 5.0) * 60.0)
        last_cookie_check = datetime.min
        loop_count = 0
        task_names = []
        for t in active_tasks:
            vm = VENUE_MAP.get(t.get('venue_key'), {})
            task_names.append(vm.get('display', t.get('venue_key', 'unknown')))
        end_text = deadline.strftime('%Y-%m-%d %H:%M:%S') if deadline else '手动停止'
        print(f"开始监控 {target_date} 回流/退订：{', '.join(task_names)}；间隔 {interval_s:.1f}s；结束：{end_text}", flush=True)

        from backend_api import validate_cookie

        while True:
            now = datetime.now()
            if deadline and now >= deadline:
                print(f"watch 已到结束时间：{deadline.strftime('%Y-%m-%d %H:%M:%S')}", flush=True)
                return

            loop_count += 1
            if (now - last_cookie_check).total_seconds() >= cookie_check_s:
                last_cookie_check = now
                try:
                    valid = validate_cookie(cookie)
                except Exception:
                    valid = False
                if not valid:
                    if not username or not password:
                        print('Cookie 已失效，但未配置账号密码，watch 停止。', flush=True)
                        return
                    logging.info('Watch: cookie invalid, performing re-login')
                    print('Cookie 已失效，开始重新登录...', flush=True)
                    cookie = do_login_and_save(require_valid=True)
                    if not cookie:
                        print('重新登录失败，watch 停止。', flush=True)
                        return
                    session = requests.Session()
                    try:
                        warmup_session(session)
                    except Exception:
                        pass

            try:
                results = do_book_with_cookie(
                    cookie,
                    session=session,
                    target_date_arg=target_date,
                    tasks_override=active_tasks,
                    quiet=True,
                )
            except AuthError as e:
                logging.info('Watch AuthError: %s. Will re-login and continue.', e)
                if not username or not password:
                    print('预约时检测到登录失效，但未配置账号密码，watch 停止。', flush=True)
                    return
                print('预约时检测到登录失效，开始重新登录...', flush=True)
                cookie = do_login_and_save(require_valid=True)
                if not cookie:
                    print('重新登录失败，watch 停止。', flush=True)
                    return
                session = requests.Session()
                continue
            except Exception as e:
                logging.exception('Watch loop failed: %s', e)
                print(f"{datetime.now().strftime('%H:%M:%S')} 本轮查询异常：{e}", flush=True)
                results = []

            success = sum(1 for ok, _ in results if ok)
            total = len(results)
            if success:
                lines = []
                for t, (ok, msg) in zip(active_tasks, results):
                    name = t.get('venue_key')
                    status = '成功' if ok else ('跳过' if msg == 'skip' else '未成功')
                    lines.append(f"- {name}: {status} {msg}")
                content = "\n".join(lines) if lines else "无任务"
                print(f"{datetime.now().strftime('%H:%M:%S')} 发现回流并完成预约：{success}/{total}", flush=True)
                send_res = send_notifications(f'回流预约结果 {success}/{total}', content, notification_cfg)
                if send_res:
                    logging.info('Notify result: %s', send_res)
                if not args.keep_watching:
                    return
            else:
                messages = [msg for _, msg in results if isinstance(msg, str)]
                if any("dateId not found" in msg for msg in messages):
                    print(f"{datetime.now().strftime('%H:%M:%S')} {target_date} query_date_ids not-ready 已达到上限，当前监控任务停止。", flush=True)
                    return
                status_every = max(1, int(60 / interval_s))
                if loop_count == 1 or loop_count % status_every == 0:
                    print(f"{datetime.now().strftime('%H:%M:%S')} 暂无可预约回流，继续监控...", flush=True)

            sleep_s = interval_s
            if deadline:
                sleep_s = min(sleep_s, max(0.0, (deadline - datetime.now()).total_seconds()))
            if sleep_s > 0:
                sleep(sleep_s)

    # mode == 'all'
    cookie = do_login_and_save()
    try:
        results = do_book_with_cookie(cookie, target_date_arg=target_date)
        success = sum(1 for ok, _ in results if ok)
        total = len(results)
        lines = []
        for t, (ok, msg) in zip(configured_tasks, results):
            name = t.get('venue_key')
            status = '跳过' if msg == 'skip' else ('成功' if ok else '失败')
            lines.append(f"- {name}: {status} {msg}")
        content = "\n".join(lines) if lines else "无任务"
        send_res = send_notifications(f'预约结果 {success}/{total}', content, notification_cfg)
        if send_res:
            logging.info('Notify result: %s', send_res)
    except Exception as e:
        logging.exception('Booking failed: %s', e)
        send_res = send_notifications('预约失败', str(e), notification_cfg)
        if send_res:
            logging.info('Notify result: %s', send_res)


if __name__ == '__main__':
    main()
