"""解析教务课表 HTML（正方 xskbcx 表格），输出结构化 schedule.json。

不联网，只吃本地 HTML；标准库实现。rowspan/colspan 用“占用网格”算法展开 ——
不能按“每行第几个单元格”取列：“上午/中午/下午”那列跨多行合并，按序号取会把课程串到隔壁星期。

用法：python parse_timetable.py [html路径]
"""
import json
import os
import re
import sys
from datetime import datetime
from pathlib import Path
from html.parser import HTMLParser

HTML_IN = Path(sys.argv[1]) if len(sys.argv) > 1 else Path('/tmp/timetable_raw.html')
OUT = Path(__file__).resolve().parent / 'schedule.json'

# 上海交大标准节次时钟（同 sjtu skill 的 sjtu_classroom.TIME_SLOTS）
TIME_SLOTS = {
    1: ("08:00", "08:45"), 2: ("08:55", "09:40"), 3: ("10:00", "10:45"), 4: ("10:55", "11:40"),
    5: ("12:00", "12:45"), 6: ("12:55", "13:40"), 7: ("14:00", "14:45"), 8: ("14:55", "15:40"),
    9: ("16:00", "16:45"), 10: ("16:55", "17:40"), 11: ("18:00", "18:45"), 12: ("18:55", "19:40"),
    13: ("19:50", "20:35"), 14: ("20:45", "21:30"),
}
WEEKDAYS = ['星期一', '星期二', '星期三', '星期四', '星期五', '星期六', '星期日']


class TableReader(HTMLParser):
    """把第一张表格记成原始单元 (row, rowspan, colspan, text)。"""

    def __init__(self):
        super().__init__()
        self.tables = []
        self.depth = 0
        self.cur = None
        self.row_idx = -1
        self.cell = None
        self.span = (1, 1)

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == 'table':
            self.depth += 1
            if self.depth == 1:
                self.cur = []
        elif tag == 'tr':
            if self.cur is not None:
                self.row_idx += 1
        elif tag in ('td', 'th'):
            if self.cur is not None:
                self.cell = []
                self.span = (int(a.get('rowspan', 1) or 1), int(a.get('colspan', 1) or 1))

    def handle_data(self, data):
        if self.cell is not None:
            self.cell.append(data)

    def handle_endtag(self, tag):
        if tag == 'table':
            if self.depth == 1 and self.cur is not None:
                self.tables.append(self.cur)
                self.cur = None
            self.depth -= 1
        elif tag in ('td', 'th'):
            if self.cell is not None:
                text = re.sub(r'\s+', ' ', ''.join(self.cell)).strip()
                if self.cur is not None:
                    self.cur.append((self.row_idx, self.span[0], self.span[1], text))
                self.cell = None


def build_grid(cells):
    """返回 {row: {col: text}}：每个单元放到本行第一个未被占用的列，并把跨行跨列的格子一起填满。"""
    grid = {}
    taken = set()
    for row, rspan, cspan, text in cells:
        col = 0
        while (row, col) in taken:
            col += 1
        for dr in range(max(1, rspan)):
            for dc in range(max(1, cspan)):
                taken.add((row + dr, col + dc))
                grid.setdefault(row + dr, {})[col + dc] = text
    return grid


def main():
    html = HTML_IN.read_text(encoding='utf-8', errors='replace')
    reader = TableReader()
    reader.feed(html)
    if not reader.tables or not reader.tables[0]:
        print('没找到表格')
        return
    grid = build_grid(reader.tables[0])
    header = grid.get(1, {})
    col_of = {}
    for i, name in enumerate(WEEKDAYS):
        for c, cell in header.items():
            if (cell or '').strip() == name:
                col_of[i + 1] = c
                break
    print('行数', max(grid) + 1, '| 表头', [header.get(c) for c in sorted(header)])
    print('星期→列号', col_of)

    busy = {str(w): [] for w in range(1, 8)}
    seen = set()
    for row in sorted(r for r in grid if r >= 2):
        for w, col in col_of.items():
            text = (grid[row].get(col) or '').strip()
            if not text or '节' not in text:
                continue
            pm = re.search(r'\((\d+)(?:-(\d+))?节\)', text)
            if not pm:
                continue
            p1 = int(pm.group(1))
            p2 = int(pm.group(2) or pm.group(1))
            if p1 not in TIME_SLOTS or p2 not in TIME_SLOTS:
                continue
            wm = re.search(r'(\d+)-(\d+)周', text)
            weeks = [int(wm.group(1)), int(wm.group(2))] if wm else [1, 30]
            odd_even = 'odd' if '单周' in text else ('even' if '双周' in text else 'all')
            name = re.split(r'[◇●○★▲☆]', text)[0].strip()[:28]
            key = (w, p1, p2, name)
            if key in seen:
                continue
            seen.add(key)
            busy[str(w)].append({
                'course': name or text[:24],
                'start': TIME_SLOTS[p1][0],
                'end': TIME_SLOTS[p2][1],
                'periods': f'{p1}-{p2}节',
                'weeks': weeks,
                'odd_even': odd_even,
                'raw': text[:120],
            })
    for k in busy:
        busy[k].sort(key=lambda x: (x['start'], x['course']))

    OUT.write_text(json.dumps({
        'updated_at': datetime.now().isoformat(timespec='seconds'),
        'source': str(HTML_IN),
        # 第 1 教学周的周一。教务页面不给当前周次，按学期起始日推算；不准就改这里或环境变量 TERM_START
        'term_start': os.environ.get('TERM_START', '2026-09-07'),
        'busy_by_weekday': busy,
    }, ensure_ascii=False, indent=2), encoding='utf-8')
    print(f'\n已写入 {OUT}')
    zh = ['周一', '周二', '周三', '周四', '周五', '周六', '周日']
    for i, w in enumerate(range(1, 8)):
        items = busy[str(w)]
        if not items:
            print(f'  {zh[i]}: 无课')
            continue
        print(f'  {zh[i]}: ' + '; '.join(
            f"{x['start'][:-3]}-{x['end'][:-3]} {x['course']}({x['weeks'][0]}-{x['weeks'][1]}周)" for x in items))


if __name__ == '__main__':
    main()
