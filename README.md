# 交大健身房预约助手

上海交通大学「学生中心 · 健身房」预约平台的自动化脚本。登录 jAccount 建立会话，查询指定日期时段的空位，按定点或回流策略下单，也能按课表自动挑出每天有空的时间段持续扫描并在本机告警。

主体代码来自一个 Windows x64 便携分享包，本仓库保留其全部 Python 源码，并加入 macOS 运行入口、支持二次验证的登录流程、课表驱动的目标生成和扫描告警脚本。仓库不含原包的 `runtime/` 二进制目录。

## 功能

- 图形界面 `ui.py`：账号密码、浏览器选择、仅登录、检测空位、定点预约、回流抢占、日期范围扫描。
- 命令行 `main.py --mode login|check|book|watch|scan7|all`，其中 `check` 只查询。
- `scan_notify.py`：对指定「日期 + 时段」组合持续轮询，命中后在本机告警，不下单。
- `harvest_session.py`、`strict_login.py`、`login_with_2fa.py`：三种登录路径，分别是复用浏览器已有会话、自动登录、以及需要人工扫码的二次验证。
- `fetch_timetable.py`、`parse_timetable.py`、`gym_windows.py`：抓取并解析教务课表，生成每天的可扫时段。
- `alert_helper.py`：本机对话框告警。
- `browser_cleanup.py`：管理脚本自己启动的 Chrome 与 chromedriver 生命周期。
- `venue_probe2.py`、`net_probe.py`：读取前端真实请求，用于确认场馆 `venueId` 和排查网络。

## 安装

macOS：

```bash
brew install tesseract
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
# 双击「启动预约助手-mac.command」打开图形界面
```

Windows：准备 Python 3.12 与 Tesseract-OCR，`pip install -r requirements.txt`，双击 `启动预约助手.bat`；或使用原分享包自带的 `runtime/`。

macOS 下指定 Chrome 与 Tesseract 的位置：

```bash
export CHROME_BINARY="/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
export TESSERACT_PATH=/opt/homebrew/bin/tesseract
export CHROME_USER_DATA_DIR="$PWD/chrome-profile"
```

域名解析不可用的网络环境再加两项：

```bash
export CHROME_PROXY=http://127.0.0.1:7897
export CHROME_HOST_RESOLVER="MAP sports.sjtu.edu.cn 202.120.35.148, MAP jaccount.sjtu.edu.cn 202.120.2.109"
```

## 登录

首次使用需要人工过一次 jAccount 二次验证。脚本把流程带到验证页面，由使用者扫码或填验证码，勾选「信任此浏览器/应用，下次不再验证」，会话写入 `state/cookie.json`：

```bash
JACCOUNT_USERNAME=... JACCOUNT_PASSWORD=... .venv/bin/python login_with_2fa.py
```

后续维持会话按顺序尝试这两步：

```bash
.venv/bin/python harvest_session.py   # 复用浏览器里已有的会话
.venv/bin/python strict_login.py      # 重新登录，取到的会话需通过排期查询验证
```

自动登录采用有窗口且关闭自动化指纹的形态。

## 扫描与告警

```bash
AUTO_SCHEDULE=1 JACCOUNT_USERNAME=... JACCOUNT_PASSWORD=... \
.venv/bin/python scan_notify.py --interval 240
```

`AUTO_SCHEDULE=1` 时目标由课表生成，每天 09:00 重算未来 7 天。固定目标直接给：

```bash
SCAN_TARGETS="2026-10-21:13-22,2026-10-24:7-22" .venv/bin/python scan_notify.py --interval 240
```

可调参数：

| 环境变量 | 默认 | 作用 |
| --- | --- | --- |
| `SCAN_INTERVAL_S` / `--interval` | 240 | 轮询间隔秒数 |
| `ACTIVE_START_HOUR` / `ACTIVE_END_HOUR` | 7 / 23 | 发请求的时段窗口 |
| `HOLD_UNTIL` | 空 | `YYYY-MM-DD HH:MM`，在此之前完全不打接口 |
| `SCAN_TARGETS` | 空 | `日期:起-止` 逗号分隔 |
| `AUTO_SCHEDULE` | 0 | 置 1 时按课表生成目标 |
| `REFRESH_SCHEDULE` | 1 | 课表超过 6 天时重抓 |
| `CLASS_BUFFER_MIN` | 20 | 课程前后预留的通勤时间 |
| `DEEP_SWEEP_MIN` | 30 | 每隔多少分钟做一轮全量排期核对 |
| `GYM_OPEN_HOUR` / `GYM_CLOSE_HOUR` | 12 / 22 | 健身房开放窗口 |

一次查询返回整天 15 个时段，扫全天与扫一小时调用量相同。会话是否可用以排期查询能否拿到 `dateId` 判定。命中需要连续 3 次复采仍为可约才提示，未通过的写入 `[slot_blink]`；提示时弹一个静默对话框，40 秒后仍有空位再补一个。告警落在本机对话框、`notifications.txt`、`notifications.json` 三处。需要在手机上接收的，把 Server酱 SendKey 或飞书、企业微信机器人 webhook 填进 `config.json` 的通知配置，或界面里对应的输入框。

轮询分两层。粗筛调 `queryFieldReserveSituationIsFull`，一个请求返回全部约 8 个日期的 `dateId`、`isAll`（当天是否全满）和 `open`，`isAll=true` 的日期直接判定为空；核对轮调 `queryFieldSituation`，一天一次，拿到完整排期矩阵。两层各自计限，粗筛把单轮请求数从「日期数」压到 1，60 秒一轮也能覆盖全天所有目标日期。核对轮用于校准 `isAll`，一旦发现粗筛说满而排期有位，程序写入 `[flag_mismatch]` 并退回每轮全查。撞到 `每日请求超过限制` 时休眠到次日 00:10。

## 课表

```bash
JACCOUNT_USERNAME=... JACCOUNT_PASSWORD=... .venv/bin/python fetch_timetable.py
.venv/bin/python parse_timetable.py
```

前者登录 i.sjtu 抓「个人课表查询」（菜单码 `N253508`）的页面 HTML，后者按占用网格展开表格，用交大节次时钟换算成 `schedule.json`，记录每门课的起止时间、周次范围和单双周。教务页面不给当前周次，周次由 `TERM_START`（第 1 教学周的周一，默认 `2026-09-07`）推算。

## 接口字段

排期矩阵每个格子含 `status`、`count`、`price`、`flag`：

| status | 含义 |
| --- | --- |
| `0` | 可预约，`count` 为剩余名额 |
| `-1` | 未到开放时间 |
| `-2` | 时段已过 |
| `-3` | 已售罄 |

学生中心健身房 12:00-22:00 开放，每小时 1 个名额。排期数据在进页面或切换日期时拉取。

## 换场馆

```bash
.venv/bin/python venue_probe2.py "学生中心"
```

登录后点进对应场馆卡片，打印前端请求与 `venueId`，在 `venue_map.py` 增一条即可。

## 隐私

`.gitignore` 排除了 `state/`、`logs/`、`chrome-profile/`、`ui_settings.json`、`schedule.json`、`i_sjtu_cookies.json` 与各类日志，会话、Cookie、预约记录、课表留在本机。账号密码经环境变量传给子进程；界面上的「保存密码」开关写入本地 `ui_settings.json`。`config.json` 中是 `${JACCOUNT_USERNAME}` 形式的环境变量占位符。

## 说明

仅供个人使用。`book`、`watch`、`scan7` 会尝试下单，产生学校系统里的真实订单。原分享包及其自带库的许可归原作者。
