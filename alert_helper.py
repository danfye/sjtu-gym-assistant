"""持续告警助手：只弹对话框（挡在所有窗口最前），全程静音，反复直到你点掉或超时。

这台 Mac 的系统横幅通知不可用（com.apple.ncprefs 无任何注册记录），所以用对话框代替。
用法：python alert_helper.py "要显示的文字" [总秒数=300] [重复间隔秒=60]
"""
import subprocess
import sys
import time


def _esc(text: str) -> str:
    return text.replace('\\', '\\\\').replace('"', '\\"')


def dialog(msg: str, timeout: int):
    script = (f'display dialog "{_esc(msg)}" with title "健身房空位" '
              f'buttons {"{\"知道了\"}"} default button "知道了" with icon caution '
              f'giving up after {timeout}')
    subprocess.Popen(['osascript', '-e', script],
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


if __name__ == '__main__':
    msg = sys.argv[1] if len(sys.argv) > 1 else '有位置了'
    total = int(sys.argv[2]) if len(sys.argv) > 2 else 300
    gap = int(sys.argv[3]) if len(sys.argv) > 3 else 60
    deadline = time.time() + total
    while time.time() < deadline:
        dialog(msg, max(10, int(deadline - time.time())))
        time.sleep(gap)
