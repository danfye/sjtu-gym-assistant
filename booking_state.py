import json
import os
from datetime import datetime, timedelta
from typing import Dict, List, Optional


STATE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'state')
BOOKINGS_FILE = os.path.join(STATE_DIR, 'bookings.json')


def _empty_state() -> dict:
    return {"bookings": {}}


def load_booking_state() -> dict:
    if not os.path.exists(BOOKINGS_FILE):
        return _empty_state()
    try:
        with open(BOOKINGS_FILE, 'r', encoding='utf-8') as f:
            data = json.load(f)
        if not isinstance(data, dict):
            return _empty_state()
        data.setdefault("bookings", {})
        if not isinstance(data["bookings"], dict):
            data["bookings"] = {}
        return data
    except Exception:
        return _empty_state()


def save_booking_state(state: dict) -> str:
    os.makedirs(STATE_DIR, exist_ok=True)
    with open(BOOKINGS_FILE, 'w', encoding='utf-8') as f:
        json.dump(state, f, ensure_ascii=False, indent=2)
    return BOOKINGS_FILE


def mark_date_booked(date: str, message: str = "", source: str = "script",
                     venue_key: str = "stu_gym", extra: Optional[dict] = None) -> str:
    datetime.strptime(date, "%Y-%m-%d")
    state = load_booking_state()
    item = {
        "status": "booked",
        "venue_key": venue_key,
        "message": message,
        "source": source,
        "updated_at": datetime.now().isoformat(timespec="seconds"),
    }
    if extra:
        item.update(extra)
    state["bookings"][date] = item
    return save_booking_state(state)


def clear_date_booking(date: str) -> str:
    state = load_booking_state()
    state.get("bookings", {}).pop(date, None)
    return save_booking_state(state)


def toggle_date_booking(date: str, venue_key: str = "stu_gym") -> str:
    datetime.strptime(date, "%Y-%m-%d")
    if is_date_booked(date):
        return clear_date_booking(date)

    today = datetime.now().strftime("%Y-%m-%d")
    if date <= today:
        message = f"手动标记训练：{date}"
    else:
        message = f"手动标记已约：{date}"
    return mark_date_booked(
        date,
        message=message,
        source="manual_toggle",
        venue_key=venue_key,
        extra={"manual": True},
    )


def get_date_booking(date: str) -> Optional[dict]:
    state = load_booking_state()
    item = state.get("bookings", {}).get(date)
    return item if isinstance(item, dict) else None


def is_date_booked(date: str) -> bool:
    item = get_date_booking(date)
    return bool(item and item.get("status") == "booked")


def date_range(start_date: str, end_date: str) -> List[str]:
    start = datetime.strptime(start_date, "%Y-%m-%d").date()
    end = datetime.strptime(end_date, "%Y-%m-%d").date()
    if start > end:
        start, end = end, start
    days = (end - start).days
    return [(start + timedelta(days=i)).strftime("%Y-%m-%d") for i in range(days + 1)]


def upcoming_dates(days: int = 7, include_today: bool = False) -> List[str]:
    start = 0 if include_today else 1
    today = datetime.now().date()
    return [(today + timedelta(days=i)).strftime("%Y-%m-%d") for i in range(start, start + int(days))]


def historical_booked_dates() -> List[str]:
    state = load_booking_state()
    bookings = state.get("bookings", {})
    dates = []
    for date, item in bookings.items():
        try:
            datetime.strptime(date, "%Y-%m-%d")
        except Exception:
            continue
        if isinstance(item, dict) and item.get("status") == "booked":
            dates.append(date)
    return sorted(set(dates))


def training_stats(today: Optional[str] = None) -> Dict[str, object]:
    today_date = datetime.strptime(today, "%Y-%m-%d").date() if today else datetime.now().date()
    booked_dates = []
    for date in historical_booked_dates():
        try:
            dt = datetime.strptime(date, "%Y-%m-%d").date()
        except Exception:
            continue
        if dt <= today_date:
            booked_dates.append(dt)

    week_start = today_date - timedelta(days=today_date.weekday())
    month_start = today_date.replace(day=1)
    earliest = min(booked_dates).strftime("%Y-%m-%d") if booked_dates else None

    return {
        "earliest": earliest,
        "total": len(booked_dates),
        "week": sum(1 for dt in booked_dates if week_start <= dt <= today_date),
        "month": sum(1 for dt in booked_dates if month_start <= dt <= today_date),
    }


def calendar_rows(days: int = 7, include_today: bool = False,
                  start_date: Optional[str] = None, end_date: Optional[str] = None,
                  include_history_booked: bool = True) -> List[Dict[str, str]]:
    today_str = datetime.now().strftime("%Y-%m-%d")
    if start_date and end_date:
        dates = date_range(start_date, end_date)
    else:
        dates = upcoming_dates(days=days, include_today=include_today)

    if include_history_booked:
        dates = sorted(set(dates) | set(historical_booked_dates()) | {today_str})

    rows = []
    for date in dates:
        item = get_date_booking(date)
        if item and item.get("status") == "booked":
            status = "已约"
            detail = item.get("message", "")
            updated_at = item.get("updated_at", "")
        else:
            status = "今天" if date == today_str else "未约"
            detail = ""
            updated_at = ""
        rows.append({
            "date": date,
            "weekday": str(datetime.strptime(date, "%Y-%m-%d").isoweekday()),
            "status": status,
            "detail": detail,
            "updated_at": updated_at,
        })
    return rows
