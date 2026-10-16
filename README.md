# 交大健身房预约助手（macOS 适配版）

上海交通大学「学生中心 · 健身房」预约平台的自动化脚本集合：自动登录 jAccount、查询指定日期时段的空位、按定点/回流策略尝试下单，并提供一个**只查不买**的持续扫描 + 本机告警工具。

原始工程是一个 Windows x64 便携分享包（自带 Python 与 Tesseract）。本仓库保留其全部源码，并补上在 macOS 上的运行方式、二次验证登录流程、配额保护与告警策略。仓库不含原包的 `runtime/` 二进制目录。

## 功能

- 图形界面 `ui.py`：填账号密码、选浏览器、仅登录、检测空位（不下单）、定点预约、回流抢占、扫描日期范围。
- 命令行 `main.py --mode login|check|book|watch|scan7|all`，`check` 只查询不下单。
- `scan_notify.py`：对指定的「日期 + 时段」组合持续轮询，命中后在本机弹告警，**全程不下单**。
- `strict_login.py` / `login_with_2fa.py`：能真正拿到可用会话的登录方式（应对 jAccount 二次验证与风控）。
- `venue_probe2.py`：抓前端真实请求，用来确认/更换场馆的 `venueId`。

## macOS 快速开始

```bash
brew install tesseract
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
# 双击「启动预约助手-mac.command」即可打开图形界面
```

Windows 需要自备 Python 3.12 与 Tesseract-OCR 后 `pip install -r requirements.txt`，或直接使用原分享包自带的 `runtime/`。

`CHROME_BINARY` 和 `TESSERACT_PATH` 两个环境变量在非图形环境里必须显式给出：`portable_start.py` 会把 `TESSERACT_CMD` 指向 Windows 的 `.exe`，所以 macOS 下要靠 `TESSERACT_PATH` 兜底。

## jAccount 二次验证（重要）

现在账号密码 + 图形验证码通过后，jAccount 会强制**二次验证**（交我办/微信扫码，或邮箱/短信验证码）。原包的登录逻辑没有这一步的分支，表现为连续多轮失败、第 2 轮起全是空 message 的超时。

正确做法：

```bash
JACCOUNT_USERNAME=... JACCOUNT_PASSWORD=... .venv/bin/python login_with_2fa.py
```

它用持久化 profile 走到二次验证页，由你本人扫码或填验证码，并勾选「信任此浏览器/应用，下次不再验证」；成功后会话写入 `state/cookie.json`。

自动重登只能用**有窗口 + 去自动化指纹**的形态（`strict_login.py`），`--headless=new` 会被判成新设备再次要求二次验证：

```bash
JACCOUNT_USERNAME=... JACCOUNT_PASSWORD=... .venv/bin/python strict_login.py
```

## 只查不买的持续扫描

```bash
SCAN_TARGETS="2026-10-17:7-22,2026-10-18:7-22" \
JACCOUNT_USERNAME=... JACCOUNT_PASSWORD=... \
.venv/bin/python scan_notify.py --interval 90
```

内置几条保护策略，都是实测踩出来的：

- **每日请求配额**：接口会返回 `每日请求超过限制，无法获取`。轮询间隔别压到十秒级，默认 90 秒；一旦撞到配额会休眠到次日 00:10，连续 3 次重登失败同样休眠，不硬撞。
- **活跃时段**：默认只在 07:00-23:00 发请求（`ACTIVE_START_HOUR` / `ACTIVE_END_HOUR`），`HOLD_UNTIL="YYYY-MM-DD HH:MM"` 可挂起到指定时间再开始。一次查询返回整天 15 个时段，扫全天和扫一小时花费相同。
- **单次命中不可信**：同一段位在几十秒内会在 `status=0` 与 `-3` 之间反复横跳（他人锁位或后端缓存不一致），所以命中后要求连续 5 次读到可约才告警，抖动只写 `[slot_blink]` 记录。
- **会话有效性判据**：`backend_api.validate_cookie` 在接口超时时会返回 True，不能当登录成功依据；本仓库以「查询接口能否拿到 `dateId`」为唯一判据。
- 杀进程会留下占用 `chrome-profile` 的孤儿 Chrome：下次 Chrome 起不来报 `SessionNotCreatedException`，而且这个没有窗口的 headless Chrome 会让 macOS 关机卡住（Chrome 不响应退出请求）。所有会启动浏览器的入口都接了 `browser_cleanup.py`：正常 `quit()`、信号处理、起新浏览器前清扫残留，并按项目目录归属过滤，不会误杀你自己开的 Chrome。

告警走本机静默弹窗（`alert_helper.py`）+ `notifications.txt` 记录，不发声。如果需要在手机收到，可把 Server酱 / 飞书 / 企微任一 webhook 填进 `config.json` 的通知配置或图形界面里对应输入框。

## 接口字段含义

`queryFieldSituation` 每个时段格子包含 `status` / `count` / `price` / `flag`：

| status | 含义 |
| --- | --- |
| `0` | 可预约（`count` 为剩余名额） |
| `-1` | 未到开放时间 |
| `-2` | 时段已过 |
| `-3` | 已售罄 |

学生中心健身房开放时间 12:00-22:00，**每个小时只有 1 个名额**，回流窗口通常只有几十秒到一两分钟。

## 隐私

- 仓库不收录 `state/`、`logs/`、`chrome-profile/`、`ui_settings.json`、`*.log`、`notifications.*`、`detections.log`，会话、Cookie、预约记录、账号密码都不会进版本库。
- 账号密码只通过环境变量 `JACCOUNT_USERNAME` / `JACCOUNT_PASSWORD` 传给子进程，不落盘；界面里勾选「保存密码」才会写入本地 `ui_settings.json`（明文，不建议）。
- `config.json` 里全部是 `${JACCOUNT_USERNAME}` 形式的环境变量占位符，没有真实凭据。

## 免责

仅供个人学习与自用。请遵守学校场馆预约规定，不要把高频请求当成常态，更不要多人共用同一账号刷位。`book` / `watch` / `scan7` 会尝试真实下单，产生学校系统里的实际订单，责任由使用者自负。原分享包及其自带库的许可归原作者。
