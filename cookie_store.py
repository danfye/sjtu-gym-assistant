import os
import json
from datetime import datetime

STATE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'state')
STATE_FILE = os.path.join(STATE_DIR, 'cookie.json')


def save_cookie(cookie_header: str):
    os.makedirs(STATE_DIR, exist_ok=True)
    data = {
        'cookie': cookie_header,
        'saved_at': datetime.now().isoformat(timespec='seconds')
    }
    with open(STATE_FILE, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    return STATE_FILE


def load_cookie():
    if not os.path.exists(STATE_FILE):
        return None, None
    with open(STATE_FILE, 'r', encoding='utf-8') as f:
        data = json.load(f)
    return data.get('cookie'), data.get('saved_at')
