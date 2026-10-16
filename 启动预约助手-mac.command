#!/bin/bash
# macOS 启动入口 —— 替代 Windows 的「启动预约助手.bat」
# 用本机的 .venv（Python + selenium/pytesseract 等）和 Homebrew tesseract，
# 跑的还是同一份 portable_start.py / ui.py，功能与 Windows 版一致。
set -e
cd "$(dirname "$0")"

CHROME_BIN="/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
TESS_BIN="/opt/homebrew/bin/tesseract"

if [ ! -x ".venv/bin/python" ]; then
  echo "找不到 .venv，请先在本目录执行：python3 -m venv .venv && .venv/bin/python -m pip install -r requirements.txt"
  read -r -p "按回车键退出" _
  exit 1
fi
if [ ! -x "$CHROME_BIN" ]; then
  echo "未检测到 Google Chrome。请安装 Chrome，或在界面里把浏览器改成 edge/firefox 并设置对应可执行文件路径。"
  read -r -p "按回车键退出" _
  exit 1
fi

export CHROME_BINARY="$CHROME_BIN"          # 本包只列了 Windows 的 chrome.exe 路径，这里显式指定
export TESSERACT_PATH="$TESS_BIN"           # portable_start.py 会把 TESSERACT_CMD 指回 .exe，故用 TESSERACT_PATH 兜底
export PYTHONIOENCODING=utf-8

exec .venv/bin/python -E -s portable_start.py
