"""把长期任务真正甩出当前会话，避免我这条 shell 一退出就把扫描进程一起带走。

2026-10-26/27 连着两次「进程无声消失」都是这个原因：`nohup ... &` 仍然留在我的 shell 会话里，
CLI 任务结束时整个会话被收掉，进程在睡梦里被杀，日志里什么异常都留不下。子进程用
`start_new_session=True`（setsid）就自成会话，父 shell 退出不会影响它。

用法：
    .venv/bin/python run_detached.py isfull_watch.py --interval 90
    .venv/bin/python run_detached.py scan_notify.py --interval 180
环境变量原样透传，进程号写到 /tmp/gym_detached.json 方便下次精准停。
"""
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

BASE = Path(__file__).resolve().parent
PIDS = Path('/tmp/gym_detached.json')


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)
    cmd = [str(BASE / '.venv/bin/python'), '-u'] + sys.argv[1:]
    old = {}
    if PIDS.exists():
        try:
            old = json.loads(PIDS.read_text())
        except Exception:
            old = {}
    # 同名脚本先收掉旧实例，避免两个哨兵重复推同一条告警
    name = Path(sys.argv[1]).name
    for n, pid in list(old.items()):
        if n == name and pid and pid != os.getpid():
            try:
                os.kill(pid, signal.SIGTERM)
                print(f'已收掉旧的 {n} (pid {pid})')
            except ProcessLookupError:
                pass
            time.sleep(1.5)

    p = subprocess.Popen(cmd, cwd=str(BASE), stdout=subprocess.DEVNULL,
                         stderr=subprocess.STDOUT, start_new_session=True, env=os.environ.copy())
    caf = subprocess.Popen(['caffeinate', '-i', '-w', str(p.pid)], cwd=str(BASE),
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
    old[name] = p.pid
    old['_caffeinate'] = caf.pid
    PIDS.write_text(json.dumps(old, ensure_ascii=False), encoding='utf-8')
    print(f'{name} 已脱离会话启动：pid={p.pid}（caffeinate {caf.pid} 防休眠）')


if __name__ == '__main__':
    main()
