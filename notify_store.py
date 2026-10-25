"""通知通道的本机保管箱：state/notify.json（state/ 已在 .gitignore 里）。

为什么要有这个文件：包里的通道配置本来只认 config.json 里的 ${FEISHU_WEBHOOK} 这类环境
变量占位符，于是每次重启扫描都得在命令行里重新带一遍 webhook；webhook 本身就是密钥，
留在 shell 历史里不合适。这里把它放进 0600 权限的本地文件，main.load_config 会在环境
变量为空时自动读它。
"""
import json
import os
import stat
from pathlib import Path

BASE = Path(__file__).resolve().parent
FILE = BASE / 'state' / 'notify.json'
KEYS = ('FEISHU_WEBHOOK', 'FEISHU_SECRET', 'FANGTANG_KEY', 'WECOM_WEBHOOK')


def read() -> dict:
    try:
        return json.loads(FILE.read_text(encoding='utf-8'))
    except Exception:
        return {}


def save(**kv) -> dict:
    cur = read()
    cur.update({k: v.strip() for k, v in kv.items() if k in KEYS and v and v.strip()})
    FILE.parent.mkdir(exist_ok=True)
    tmp = FILE.with_suffix('.tmp')
    tmp.write_text(json.dumps(cur, ensure_ascii=False, indent=2), encoding='utf-8')
    os.chmod(tmp, stat.S_IRUSR | stat.S_IWUSR)   # 0600：只有本用户可读
    tmp.replace(FILE)
    return cur


def apply_local_overrides(notifications: dict) -> dict:
    """环境变量/config.json 没给出的通道，用本机文件补上（已有的值不覆盖）。"""
    local = read()
    for k in KEYS:
        if not (notifications.get(k) or '').strip() and local.get(k):
            notifications[k] = local[k]
    return notifications


if __name__ == '__main__':
    print(json.dumps(read(), ensure_ascii=False, indent=2) if FILE.exists() else '（还没有存任何通道）')
