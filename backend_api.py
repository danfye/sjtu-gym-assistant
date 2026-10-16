import time
import logging
import requests
from datetime import datetime
import os
from typing import List, Tuple, Optional
from venue_map import VENUE_MAP


class AuthError(Exception):
    """Raised when server reports authentication/login expired."""
    pass

BASE = "https://sports.sjtu.edu.cn"
# 统一 User-Agent：默认模拟 Chrome，以贴近旧脚本成功配置；也可用环境变量 SPORTS_UA 覆盖
UA = os.environ.get(
    'SPORTS_UA',
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_1) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/95.0.4638.69 Safari/537.36"
)


def _headers(cookie: str):
    return {
        "Cookie": cookie,
        "Content-Type": "application/json;charset=UTF-8",
        "Accept": "application/json, text/plain, */*",
        "Accept-Encoding": "gzip, deflate, br",
        "Accept-Language": "zh-CN,zh;q=0.9",
        "Origin": "https://sports.sjtu.edu.cn",
        "Referer": "https://sports.sjtu.edu.cn/pc/",
        # 补充与旧脚本一致的 UA/Client Hints，增加一致性
        "User-Agent": UA,
        "sec-ch-ua": "\"Google Chrome\";v=95, \"Chromium\";v=95, \"Not A Brand\";v=99",
        "sec-ch-ua-platform": "macOS",
    }

def query_date_ids(cookie: str, venTypeId: str, venueId: str, target_date: str, session: Optional[requests.Session] = None) -> dict:
    """查询可预约日期的 dateId 列表。

    更健壮的解析：仅当 code==0 且 data 存在时返回映射；否则重试并记录日志。
    超时后返回空 dict，避免上层 KeyError。
    """
    url = f"{BASE}/manage/fieldDetail/queryFieldReserveSituationIsFull"
    # 与历史脚本对齐：使用目标预约日期进行查询，降低跨周/窗口边界的不确定性
    body = {"date": target_date, "feildType": venTypeId, "id": venueId}
    last_text = ""
    s = session or requests
    for i in range(5):  # not-ready 最多尝试 5 次，避免长时间卡在同一天
        try:
            r = s.post(url, json=body, headers=_headers(cookie), timeout=10)
            last_text = r.text
            if r.status_code != 200:
                logging.warning("query_date_ids status=%s try=%s", r.status_code, i + 1)
                time.sleep(0.3)
                continue
            j = r.json()
            code = j.get("code")
            data = j.get("data")
            msg = j.get("msg") or j.get("message") or ""
            # 如果服务端明确返回未登录/超时，抛出 AuthError 以便上层触发重新登录
            if code == 401 or (isinstance(msg, str) and ("未登录" in msg or "登录超时" in msg or "登录已失效" in msg)):
                raise AuthError(msg or str(j))
            if code == 0 and isinstance(data, list) and data:
                return {x.get("date"): x.get("dateId") for x in data if x.get("date") and x.get("dateId")}
            # 非开放/未准备好/会话无效等，记录但不抛错
            msg = j.get("msg") or j.get("message") or str(j)[:200]
            logging.info("query_date_ids not-ready try=%s code=%s msg=%s", i + 1, code, msg)
        except Exception as e:
            logging.warning("query_date_ids error try=%s: %s", i + 1, e)
    time.sleep(0.3)
    # 返回空，交由上层根据 target_date 判定
    logging.warning("query_date_ids give up, last response: %s", (last_text or "")[:300])
    return {}

def query_field_situation(cookie: str, date: str, venTypeId: str, venueId: str, dateId: str, session: Optional[requests.Session] = None) -> Tuple[list, list]:
    """查询某日期的场地时段与可用矩阵所需信息。

    仅当 code==0 且 data 存在时返回 (price_lists, field_ids)，否则多次重试；
    超时后返回 ([], [])，由上层决定后续处理。
    """
    url = f"{BASE}/manage/fieldDetail/queryFieldSituation"
    body = {"fieldType": venTypeId, "date": date, "venueId": venueId, "dateId": dateId}
    last_text = ""
    s = session or requests
    for i in range(20):
        try:
            r = s.post(url, json=body, headers=_headers(cookie), timeout=10)
            last_text = r.text
            if r.status_code != 200:
                logging.warning("query_field_situation status=%s try=%s", r.status_code, i + 1)
                time.sleep(0.3)
                continue
            j = r.json()
            code = j.get("code")
            data = j.get("data")
            msg = j.get("msg") or j.get("message") or ""
            if code == 401 or (isinstance(msg, str) and ("未登录" in msg or "登录超时" in msg or "登录已失效" in msg)):
                raise AuthError(msg or str(j))
            if code == 0 and isinstance(data, list) and data:
                price_lists = [x.get("priceList", []) for x in data]
                field_ids = [x.get("fieldId") for x in data]
                return price_lists, field_ids
            msg = msg or str(j)[:200]
            logging.info("query_field_situation not-ready try=%s code=%s msg=%s", i + 1, code, msg)
        except Exception as e:
            logging.warning("query_field_situation error try=%s: %s", i + 1, e)
        time.sleep(0.3)
    logging.warning("query_field_situation give up, last response: %s", (last_text or "")[:300])
    return [], []


def validate_cookie(cookie: str, ven_key: str = 'stu_gym', target_date: str = None) -> bool:
    """轻量校验 cookie 是否被服务端接受。

    方法：使用一个非破坏性的查询接口（queryFieldReserveSituationIsFull）对指定场馆和日期发起请求，
    检查返回是否包含 401 / 未登录提示；若不是 401 则视为 cookie 有效。
    这个检查尽量紧凑（短超时），仅用于登录后确认 session 是否生效。
    """
    # 选择场馆
    vm = VENUE_MAP.get(ven_key)
    if not vm:
        return True
    venTypeId = vm['venTypeId']
    venueId = vm['venueId']
    if not target_date:
        target_date = datetime.now().strftime('%Y-%m-%d')

    url = f"{BASE}/manage/fieldDetail/queryFieldReserveSituationIsFull"
    body = {"date": target_date, "feildType": venTypeId, "id": venueId}

    last_err = None
    for i in range(3):
        try:
            r = requests.post(url, json=body, headers=_headers(cookie), timeout=4)
            if r.status_code != 200:
                last_err = f"status={r.status_code}"
                time.sleep(0.15)
                continue
            j = r.json()
            code = j.get('code')
            msg = j.get('msg') or j.get('message') or ''
            if code == 401 or (isinstance(msg, str) and ('未登录' in msg or '登录超时' in msg or '登录已失效' in msg)):
                return False
            return True
        except Exception as e:
            last_err = str(e)
            time.sleep(0.15)

    # 高峰时接口可能超时/502/非 200，这种情况下不应触发“立即重登”风暴；宁可认为 cookie 有效
    logging.warning('validate_cookie inconclusive after retries (%s); assuming valid', last_err)
    return True


def build_available_matrix(price_lists):
    # 矩阵维度：time x field
    if not price_lists:
        return []
    t_count = len(price_lists[0])
    f_count = len(price_lists)
    mat = [[-1] * f_count for _ in range(t_count)]
    for f in range(f_count):
        for t in range(t_count):
            cell = price_lists[f][t]
            if cell.get('status') == '0':
                mat[t][f] = cell
    return mat


def choose_fields_by_priority(mat, time_hour: int, priority: List[int], count: int) -> List[int]:
    # time_hour 从 7 开始，转换为矩阵索引
    row = time_hour - 7
    if row < 0 or row >= len(mat):
        return []
    chosen = []
    for idx in priority:
        if 0 <= idx < len(mat[row]) and mat[row][idx] != -1:
            chosen.append(idx)
            if len(chosen) >= count:
                break
    return chosen


def get_server_time_offset(session: Optional[requests.Session] = None) -> float:
    """测量服务器时间与本地时间的差值（server - local，单位秒）。

    通过获取 sports.sjtu.edu.cn 的 HTTP Date 响应头，换算为本地时区时间，再与本地 now() 比较。
    失败时返回 0。
    """
    try:
        import email.utils as eut
        s = session or requests
        # 使用一个轻量的 GET 请求
        r = s.get(f"{BASE}/pc/", timeout=5)
        date_hdr = r.headers.get('Date')
        if not date_hdr:
            return 0.0
        # 解析 RFC 1123 时间
        tt = eut.parsedate_to_datetime(date_hdr)
        # 转换为本地时区的时间戳再比较
        server_ts = tt.timestamp()
        local_ts = time.time()
        return float(server_ts - local_ts)
    except Exception:
        return 0.0


def warmup_session(session: requests.Session) -> None:
    """对会话做一次轻量预热，建立 TLS 连接与连接池。忽略结果。"""
    try:
        session.get(f"{BASE}/pc/", timeout=3)
    except Exception:
        pass
