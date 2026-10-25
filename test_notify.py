"""配一次飞书机器人，然后当场验证能不能收到。

用法：
    .venv/bin/python test_notify.py <webhook地址> [加签密钥]      # 存进 state/notify.json 并发测试
    .venv/bin/python test_notify.py                              # 用已存的配置再发一条
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from messages import send_message_feishu
import notify_store

if len(sys.argv) > 1:
    hook = sys.argv[1].strip()
    secret = sys.argv[2].strip() if len(sys.argv) > 2 else ''
    stored = notify_store.save(FEISHU_WEBHOOK=hook, FEISHU_SECRET=secret)
    print('已存入', notify_store.FILE, '（0600，已在 .gitignore 的 state/ 下）')
    if not secret:
        stored.pop('FEISHU_SECRET', None)
        notify_store.save(**{k: stored.get(k, '') for k in notify_store.KEYS if stored.get(k)})

cfg = notify_store.read()
hook = cfg.get('FEISHU_WEBHOOK', '')
secret = cfg.get('FEISHU_SECRET', '')
if not hook:
    print('还没有飞书 webhook：把群里自定义机器人的地址当参数传进来即可。')
    sys.exit(1)

print('webhook =', hook[:48] + ('...' if len(hook) > 48 else ''), '| 加签 =', '有' if secret else '无')
res = send_message_feishu('健身房通知通道测试',
                          '如果你在手机飞书里看到这条消息，扫描器命中空位时就会推到这里。', hook, secret)
print('飞书返回：', res)
