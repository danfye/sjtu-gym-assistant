"""按课表自动算出「每天该扫哪些时段」，供 scan_notify 滚动使用，不用手动改目标。

规则：健身房可约窗口 12:00-22:00（整点格子 12..21），
某个小时格子只要与当天任一节课（前后各留通勤缓冲）没有重叠，就算可扫；连续的整点合并成 "起-止" 区间。

周次：正方页面不给“当前第几周”，所以按 schedule.json 里的 term_start（第 1 教学周的周一）推算；
term_start 不对就改 schedule.json 或设环境变量 TERM_START。
"""
import json
import os
from datetime import date, datetime, timedelta
from pathlib import Path

BASE = Path(__file__).resolve().parent
SCHEDULE = Path(os.environ.get('SCHEDULE_JSON', str(BASE / 'schedule.json')))
GYM_OPEN = int(os.environ.get('GYM_OPEN_HOUR', '12'))
GYM_CLOSE = int(os.environ.get('GYM_CLOSE_HOUR', '22'))     # 可约格子到 21:00-22:00
BUFFER_MIN = int(os.environ.get('CLASS_BUFFER_MIN', '20'))  # 课前课后各留多少分钟通勤/换衣
HORIZON = int(os.environ.get('SCAN_HORIZON', '7'))          # 往后扫几天（含今天）


def _hm(s):
    h, m = s.split(':')
    return int(h) * 60 + int(m)


def load_schedule():
    try:
        d = json.loads(SCHEDULE.read_text(encoding='utf-8'))
    except Exception:
        return None
    return d


def teaching_week(day, term_start_str):
    try:
        ts = datetime.strptime(term_start_str, '%Y-%m-%d').date()
    except Exception:
        return None
    if day < ts:
        return None
    return (day - ts).days // 7 + 1


def busy_intervals(day, sched):
    """当天上课区间（分钟），已含前后缓冲。"""
    wk = teaching_week(day, sched.get('term_start', ''))
    out = []
    items = (sched.get('busy_by_weekday') or {}).get(str(day.isoweekday()), [])
    for c in items:
        w0, w1 = (c.get('weeks') or [1, 30])[:2]
        if wk is None:
            in_week = True          # 推不出周次时保守当作有课
        else:
            in_week = w0 <= wk <= w1
            oe = c.get('odd_even', 'all')
            if in_week and oe == 'odd':
                in_week = wk % 2 == 1
            elif in_week and oe == 'even':
                in_week = wk % 2 == 0
        if not in_week:
            continue
        out.append((_hm(c['start']) - BUFFER_MIN, _hm(c['end']) + BUFFER_MIN, c.get('course', '')))
    return out


def free_ranges(day, sched, min_hour=GYM_OPEN):
    """返回该天可扫的整点区间字符串列表，如 ['12-14', '21-22']。"""
    busy = busy_intervals(day, sched)
    ok_hours = []
    for h in range(max(GYM_OPEN, min_hour), GYM_CLOSE):
        s, e = h * 60, (h + 1) * 60
        if any(s < be and bs < e for bs, be, _ in busy):
            continue
        ok_hours.append(h)
    runs = []
    for h in ok_hours:
        if runs and h == runs[-1][1]:
            runs[-1][1] = h + 1
        else:
            runs.append([h, h + 1])
    return [f'{a}-{b}' for a, b in runs]


def targets(days_ahead=HORIZON, start=None):
    sched = load_schedule()
    start = start or date.today()
    out = []
    for i in range(days_ahead):
        day = start + timedelta(days=i)
        if day == date.today():
            min_hour = max(GYM_OPEN, datetime.now().hour + 1)   # 今天已经过去的小时不再扫
        else:
            min_hour = GYM_OPEN
        rngs = free_ranges(day, sched, min_hour) if sched else [f'{GYM_OPEN}-{GYM_CLOSE}']
        for r in rngs:
            out.append((day.isoformat(), r))
    return out


def targets_spec(**kw):
    return ','.join(f'{d}:{r}' for d, r in targets(**kw))


if __name__ == '__main__':
    spec = targets_spec()
    print('未来自动目标：')
    print(spec)
    sched = load_schedule()
    if sched:
        print('\n逐日明细（含周次与有课时段）：')
        for i in range(HORIZON):
            day = date.today() + timedelta(days=i)
            wk = teaching_week(day, sched.get('term_start', ''))
            def _fmt(m):
                return f'{m // 60:02d}:{m % 60:02d}'
            busy = '; '.join(f'{_fmt(a)}~{_fmt(b)} {n}' for a, b, n in busy_intervals(day, sched)) or '无课'
            print(f'  {day} {["周一","周二","周三","周四","周五","周六","周日"][day.isoweekday()-1]}'
                  f' 第{wk}周 可扫={free_ranges(day, sched) or "无"} 课={busy}')
    else:
        print(f'（没找到 {SCHEDULE}，先跑 parse_timetable.py）')
