import json
import os
import subprocess
import sys
import threading
from datetime import datetime, timedelta
from pathlib import Path
from queue import Empty, Queue
import tkinter as tk
from tkinter import messagebox, ttk


BASE_DIR = Path(__file__).resolve().parent
MAIN_FILE = BASE_DIR / "main.py"
CONFIG_FILE = BASE_DIR / "config.json"
SETTINGS_FILE = BASE_DIR / "ui_settings.json"
RUN_LOG = BASE_DIR / "run.log"
CRON_LOG = BASE_DIR / "cron.log"

WEEKDAY_NAMES = {
    "1": "周一",
    "2": "周二",
    "3": "周三",
    "4": "周四",
    "5": "周五",
    "6": "周六",
    "7": "周日",
}


def _default_target_date():
    return (datetime.now() + timedelta(days=7)).strftime("%Y-%m-%d")


def _default_range_start():
    return datetime.now().strftime("%Y-%m-%d")


def _default_range_end():
    return (datetime.now() + timedelta(days=7)).strftime("%Y-%m-%d")


def _read_config():
    try:
        return json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _python_for_worker():
    exe = Path(sys.executable)
    if exe.name.lower() == "pythonw.exe":
        console_python = exe.with_name("python.exe")
        if console_python.exists():
            return str(console_python)
    return sys.executable


class BookerApp(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("交大健身房预约助手")
        self.geometry("1100x760")
        self.minsize(920, 660)

        self.process = None
        self.reader_thread = None
        self.output_queue = Queue()
        self.saved_log_size = 0

        self.vars = self._build_vars()
        self._build_ui()
        self._load_settings(silent=True)
        self._refresh_calendar()
        self.after(100, self._drain_output_queue)
        self.after(1500, self._tail_run_log)
        self.after(5000, self._refresh_calendar_periodic)
        self.protocol("WM_DELETE_WINDOW", self._on_close)

    def _build_vars(self):
        cfg = _read_config()
        tasks = cfg.get("tasks") or []
        default_ranges = ",".join((tasks[0].get("time_ranges") if tasks else None) or ["20-21"])
        return {
            "username": tk.StringVar(value=""),
            "password": tk.StringVar(value=""),
            "fangtang": tk.StringVar(value=""),
            "feishu_webhook": tk.StringVar(value=""),
            "feishu_secret": tk.StringVar(value=""),
            "wecom_webhook": tk.StringVar(value=""),
            "save_password": tk.BooleanVar(value=False),
            "browser": tk.StringVar(value=cfg.get("browser") or "edge"),
            "headless": tk.BooleanVar(value=bool(cfg.get("headless", True))),
            "target_date": tk.StringVar(value=_default_target_date()),
            "days_ahead": tk.StringVar(value="7"),
            "range_start": tk.StringVar(value=_default_range_start()),
            "range_end": tk.StringVar(value=_default_range_end()),
            "time_ranges": tk.StringVar(value=default_ranges),
            "book_at": tk.StringVar(value="12:00:00"),
            "use_server_time": tk.BooleanVar(value=True),
            "soft_poll_window": tk.StringVar(value="5"),
            "soft_poll_interval": tk.StringVar(value="350"),
            "poll_interval": tk.StringVar(value="300000"),
            "watch_until": tk.StringVar(value=""),
            "watch_duration": tk.StringVar(value=""),
            "keep_watching": tk.BooleanVar(value=False),
            "parallel_confirm": tk.StringVar(value=""),
            "confirm_batches": tk.StringVar(value=""),
            "confirm_jitter": tk.StringVar(value=""),
            "confirm_backoff": tk.StringVar(value=""),
            "confirm_timeout": tk.StringVar(value=""),
        }

    def _build_ui(self):
        style = ttk.Style(self)
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass
        style.configure("TButton", padding=(10, 5))
        style.configure("Primary.TButton", padding=(12, 6))
        style.configure("Danger.TButton", padding=(12, 6))
        style.configure("TLabel", padding=(0, 2))

        root = ttk.Frame(self, padding=14)
        root.pack(fill="both", expand=True)
        root.columnconfigure(0, weight=1)
        root.rowconfigure(3, weight=1)

        header = ttk.Frame(root)
        header.grid(row=0, column=0, sticky="ew", pady=(0, 10))
        header.columnconfigure(0, weight=1)
        ttk.Label(header, text="交大健身房预约助手", font=("Microsoft YaHei UI", 16, "bold")).grid(row=0, column=0, sticky="w")
        ttk.Label(header, text="不用敲 PowerShell，点按钮跑健身房登录、定点预约和回流抢占。").grid(row=1, column=0, sticky="w")

        settings = ttk.LabelFrame(root, text="登录与运行设置", padding=12)
        settings.grid(row=1, column=0, sticky="ew")
        for i in range(8):
            settings.columnconfigure(i, weight=1)

        ttk.Label(settings, text="账号").grid(row=0, column=0, sticky="w")
        ttk.Entry(settings, textvariable=self.vars["username"], width=22).grid(row=0, column=1, sticky="ew", padx=(6, 14))
        ttk.Label(settings, text="密码").grid(row=0, column=2, sticky="w")
        ttk.Entry(settings, textvariable=self.vars["password"], width=22, show="*").grid(row=0, column=3, sticky="ew", padx=(6, 14))
        ttk.Label(settings, text="浏览器").grid(row=0, column=4, sticky="w")
        ttk.Combobox(
            settings,
            textvariable=self.vars["browser"],
            values=("edge", "chrome", "firefox"),
            state="readonly",
            width=10,
        ).grid(row=0, column=5, sticky="ew", padx=(6, 14))
        ttk.Checkbutton(settings, text="无头模式", variable=self.vars["headless"]).grid(row=0, column=6, sticky="w")

        ttk.Label(settings, text="Server酱 Key").grid(row=1, column=0, sticky="w", pady=(8, 0))
        ttk.Entry(settings, textvariable=self.vars["fangtang"], width=34, show="*").grid(row=1, column=1, columnspan=3, sticky="ew", padx=(6, 14), pady=(8, 0))
        ttk.Checkbutton(settings, text="保存密码到本地明文设置文件", variable=self.vars["save_password"]).grid(row=1, column=4, columnspan=2, sticky="w", pady=(8, 0))
        ttk.Button(settings, text="保存设置", command=self._save_settings).grid(row=1, column=6, sticky="ew", pady=(8, 0))
        ttk.Button(settings, text="读取设置", command=lambda: self._load_settings(silent=False)).grid(row=1, column=7, sticky="ew", padx=(8, 0), pady=(8, 0))

        ttk.Label(settings, text="飞书 Webhook").grid(row=2, column=0, sticky="w", pady=(8, 0))
        ttk.Entry(settings, textvariable=self.vars["feishu_webhook"], width=34, show="*").grid(row=2, column=1, columnspan=3, sticky="ew", padx=(6, 14), pady=(8, 0))
        ttk.Label(settings, text="飞书 Secret").grid(row=2, column=4, sticky="w", pady=(8, 0))
        ttk.Entry(settings, textvariable=self.vars["feishu_secret"], width=18, show="*").grid(row=2, column=5, columnspan=3, sticky="ew", padx=(6, 0), pady=(8, 0))

        ttk.Label(settings, text="企微 Webhook").grid(row=3, column=0, sticky="w", pady=(8, 0))
        ttk.Entry(settings, textvariable=self.vars["wecom_webhook"], width=34, show="*").grid(row=3, column=1, columnspan=5, sticky="ew", padx=(6, 14), pady=(8, 0))
        ttk.Button(settings, text="测试通知", command=self._send_test_notification).grid(row=3, column=6, columnspan=2, sticky="ew", pady=(8, 0))

        controls = ttk.LabelFrame(root, text="健身房预约与回流抢占", padding=12)
        controls.grid(row=2, column=0, sticky="ew", pady=(10, 10))
        for i in range(10):
            controls.columnconfigure(i, weight=1)

        ttk.Label(controls, text="目标日期").grid(row=0, column=0, sticky="w")
        ttk.Entry(controls, textvariable=self.vars["target_date"], width=13).grid(row=0, column=1, sticky="ew", padx=(6, 10))
        ttk.Button(controls, text="今天+7天", command=self._set_target_days_ahead).grid(row=0, column=2, sticky="ew", padx=(0, 10))
        ttk.Label(controls, text="准点时间").grid(row=0, column=3, sticky="w")
        ttk.Entry(controls, textvariable=self.vars["book_at"], width=10).grid(row=0, column=4, sticky="ew", padx=(6, 10))
        ttk.Checkbutton(controls, text="服务器时间", variable=self.vars["use_server_time"]).grid(row=0, column=5, sticky="w")
        ttk.Label(controls, text="预取秒").grid(row=0, column=6, sticky="w")
        ttk.Entry(controls, textvariable=self.vars["soft_poll_window"], width=6).grid(row=0, column=7, sticky="ew", padx=(6, 10))
        ttk.Label(controls, text="预取间隔ms").grid(row=0, column=8, sticky="w")
        ttk.Entry(controls, textvariable=self.vars["soft_poll_interval"], width=7).grid(row=0, column=9, sticky="ew", padx=(6, 0))

        ttk.Label(controls, text="健身房时段").grid(row=1, column=0, sticky="w", pady=(8, 0))
        ttk.Entry(controls, textvariable=self.vars["time_ranges"], width=16).grid(row=1, column=1, columnspan=2, sticky="ew", padx=(6, 10), pady=(8, 0))
        ttk.Label(controls, text="例：14-15 或 14-15,20-21").grid(row=1, column=3, columnspan=3, sticky="w", pady=(8, 0))

        ttk.Label(controls, text="监控间隔ms").grid(row=2, column=0, sticky="w", pady=(8, 0))
        ttk.Entry(controls, textvariable=self.vars["poll_interval"], width=8).grid(row=2, column=1, sticky="ew", padx=(6, 10), pady=(8, 0))
        ttk.Label(controls, text="监控到").grid(row=2, column=2, sticky="w", pady=(8, 0))
        ttk.Entry(controls, textvariable=self.vars["watch_until"], width=10).grid(row=2, column=3, sticky="ew", padx=(6, 10), pady=(8, 0))
        ttk.Label(controls, text="最多分钟").grid(row=2, column=4, sticky="w", pady=(8, 0))
        ttk.Entry(controls, textvariable=self.vars["watch_duration"], width=8).grid(row=2, column=5, sticky="ew", padx=(6, 10), pady=(8, 0))
        ttk.Checkbutton(controls, text="抢到后继续监控", variable=self.vars["keep_watching"]).grid(row=2, column=6, columnspan=2, sticky="w", pady=(8, 0))

        ttk.Label(controls, text="扫描开始").grid(row=3, column=0, sticky="w", pady=(8, 0))
        ttk.Entry(controls, textvariable=self.vars["range_start"], width=13).grid(row=3, column=1, sticky="ew", padx=(6, 10), pady=(8, 0))
        ttk.Label(controls, text="扫描结束").grid(row=3, column=2, sticky="w", pady=(8, 0))
        ttk.Entry(controls, textvariable=self.vars["range_end"], width=13).grid(row=3, column=3, sticky="ew", padx=(6, 10), pady=(8, 0))
        ttk.Button(controls, text="今天~+7天", command=self._set_scan_range_default).grid(row=3, column=4, sticky="ew", padx=(0, 10), pady=(8, 0))
        ttk.Label(controls, text="日历会额外显示今天和历史已约日期").grid(row=3, column=5, columnspan=5, sticky="w", pady=(8, 0))

        advanced = ttk.Frame(controls)
        advanced.grid(row=4, column=0, columnspan=10, sticky="ew", pady=(10, 0))
        for i in range(10):
            advanced.columnconfigure(i, weight=1)
        ttk.Label(advanced, text="高级参数可留空").grid(row=0, column=0, sticky="w")
        self._small_entry(advanced, "并发", "parallel_confirm", 1)
        self._small_entry(advanced, "批次", "confirm_batches", 3)
        self._small_entry(advanced, "抖动ms", "confirm_jitter", 5)
        self._small_entry(advanced, "退避ms", "confirm_backoff", 7)
        self._small_entry(advanced, "超时s", "confirm_timeout", 9)

        buttons = ttk.Frame(root)
        buttons.grid(row=3, column=0, sticky="nsew")
        buttons.columnconfigure(0, weight=0)
        buttons.columnconfigure(1, weight=1)
        buttons.rowconfigure(0, weight=1)

        left = ttk.Frame(buttons)
        left.grid(row=0, column=0, sticky="nw", padx=(0, 12))
        ttk.Button(left, text="仅登录", style="Primary.TButton", command=self._run_login).grid(row=0, column=0, sticky="ew", pady=(0, 8))
        ttk.Button(left, text="检测空位（不下单）", style="Primary.TButton", command=self._run_check).grid(row=1, column=0, sticky="ew", pady=(0, 8))
        ttk.Button(left, text="扫描指定日期范围", style="Primary.TButton", command=self._run_scan7).grid(row=2, column=0, sticky="ew", pady=(0, 8))
        ttk.Button(left, text="健身房定点预约", style="Primary.TButton", command=self._run_book).grid(row=3, column=0, sticky="ew", pady=(0, 8))
        ttk.Button(left, text="健身房回流抢占", style="Primary.TButton", command=self._run_watch).grid(row=4, column=0, sticky="ew", pady=(0, 8))
        ttk.Button(left, text="停止当前任务", style="Danger.TButton", command=self._stop_process).grid(row=5, column=0, sticky="ew", pady=(0, 8))
        ttk.Button(left, text="打开 run.log", command=lambda: self._open_file(RUN_LOG)).grid(row=6, column=0, sticky="ew", pady=(0, 8))
        ttk.Button(left, text="打开 cron.log", command=lambda: self._open_file(CRON_LOG)).grid(row=7, column=0, sticky="ew", pady=(0, 8))

        right = ttk.Frame(buttons)
        right.grid(row=0, column=1, sticky="nsew")
        right.columnconfigure(0, weight=1)
        right.rowconfigure(1, weight=1)

        calendar_frame = ttk.LabelFrame(right, text="预约日历（历史记录 + 当前范围）", padding=8)
        calendar_frame.grid(row=0, column=0, sticky="ew", pady=(0, 10))
        calendar_frame.columnconfigure(0, weight=1)
        self.calendar_summary = tk.StringVar(value="累计：暂无已训练记录\n本周：0 次\n本月：0 次")
        ttk.Label(calendar_frame, textvariable=self.calendar_summary, justify="left").grid(row=0, column=0, sticky="w", pady=(0, 6))
        self.calendar = ttk.Treeview(
            calendar_frame,
            columns=("date", "weekday", "status", "updated_at", "detail"),
            show="headings",
            height=10,
            selectmode="browse",
        )
        for col, text, width in (
            ("date", "日期", 105),
            ("weekday", "星期", 55),
            ("status", "状态", 70),
            ("updated_at", "记录时间", 145),
            ("detail", "记录", 320),
        ):
            self.calendar.heading(col, text=text)
            self.calendar.column(col, width=width, anchor="w", stretch=(col == "detail"))
        self.calendar.grid(row=1, column=0, sticky="nsew")
        cal_scroll = ttk.Scrollbar(calendar_frame, orient="vertical", command=self.calendar.yview)
        cal_scroll.grid(row=1, column=1, sticky="ns")
        self.calendar.configure(yscrollcommand=cal_scroll.set)
        cal_buttons = ttk.Frame(calendar_frame)
        cal_buttons.grid(row=2, column=0, columnspan=2, sticky="ew", pady=(8, 0))
        ttk.Button(cal_buttons, text="刷新日历", command=self._refresh_calendar).pack(side="left")
        ttk.Button(cal_buttons, text="切换选中日期状态", command=self._toggle_selected_calendar_day).pack(side="left", padx=(8, 0))

        log_frame = ttk.LabelFrame(right, text="运行输出", padding=8)
        log_frame.grid(row=1, column=0, sticky="nsew")
        log_frame.columnconfigure(0, weight=1)
        log_frame.rowconfigure(0, weight=1)
        self.output = tk.Text(log_frame, wrap="word", bg="#111111", fg="#eeeeee", insertbackground="#eeeeee", relief="flat")
        self.output.grid(row=0, column=0, sticky="nsew")
        scroll = ttk.Scrollbar(log_frame, orient="vertical", command=self.output.yview)
        scroll.grid(row=0, column=1, sticky="ns")
        self.output.configure(yscrollcommand=scroll.set)

        self.status = tk.StringVar(value="就绪")
        status_bar = ttk.Label(root, textvariable=self.status, anchor="w")
        status_bar.grid(row=4, column=0, sticky="ew", pady=(8, 0))

    def _small_entry(self, parent, label, key, col):
        ttk.Label(parent, text=label).grid(row=0, column=col, sticky="e", padx=(12, 4))
        ttk.Entry(parent, textvariable=self.vars[key], width=7).grid(row=0, column=col + 1, sticky="ew")

    def _append_output(self, text):
        self.output.configure(state="normal")
        self.output.insert("end", text)
        self.output.see("end")
        self.output.configure(state="disabled")

    def _queue_output(self, text):
        self.output_queue.put(text)

    def _drain_output_queue(self):
        try:
            while True:
                self._append_output(self.output_queue.get_nowait())
        except Empty:
            pass
        if self.process and self.process.poll() is not None:
            code = self.process.returncode
            self.process = None
            self.status.set(f"任务已结束，退出码 {code}")
            self._queue_output(f"\n[任务结束] exit={code}\n")
            self._refresh_calendar()
        self.after(100, self._drain_output_queue)

    def _tail_run_log(self):
        try:
            if RUN_LOG.exists():
                size = RUN_LOG.stat().st_size
                if self.saved_log_size == 0:
                    self.saved_log_size = size
                elif size > self.saved_log_size:
                    with RUN_LOG.open("r", encoding="utf-8", errors="replace") as f:
                        f.seek(self.saved_log_size)
                        chunk = f.read()
                    if chunk:
                        self._queue_output("\n[run.log]\n" + chunk)
                    self.saved_log_size = size
                elif size < self.saved_log_size:
                    self.saved_log_size = size
        except Exception:
            pass
        self.after(1500, self._tail_run_log)

    def _set_target_days_ahead(self):
        try:
            days = int(self.vars["days_ahead"].get() or "7")
        except ValueError:
            days = 7
        self.vars["target_date"].set((datetime.now() + timedelta(days=days)).strftime("%Y-%m-%d"))

    def _set_scan_range_default(self):
        self.vars["range_start"].set(_default_range_start())
        self.vars["range_end"].set(_default_range_end())
        self._refresh_calendar()

    def _normalized_scan_range(self, show_error=False):
        start = self.vars["range_start"].get().strip() or _default_range_start()
        end = self.vars["range_end"].get().strip() or _default_range_end()
        try:
            start_dt = datetime.strptime(start, "%Y-%m-%d")
            end_dt = datetime.strptime(end, "%Y-%m-%d")
        except ValueError:
            if show_error:
                messagebox.showerror("日期范围格式不对", "扫描开始和扫描结束请填 YYYY-MM-DD，例如 2026-04-24。")
            return None, None
        if start_dt > end_dt:
            start_dt, end_dt = end_dt, start_dt
        return start_dt.strftime("%Y-%m-%d"), end_dt.strftime("%Y-%m-%d")

    def _validate_common(self):
        date_text = self.vars["target_date"].get().strip()
        if date_text:
            try:
                datetime.strptime(date_text, "%Y-%m-%d")
            except ValueError:
                messagebox.showerror("日期格式不对", "目标日期请填 YYYY-MM-DD，例如 2026-04-30。")
                return False
        if self.vars["book_at"].get().strip():
            try:
                datetime.strptime(self.vars["book_at"].get().strip(), "%H:%M:%S")
            except ValueError:
                messagebox.showerror("时间格式不对", "准点时间请填 HH:MM:SS，例如 12:00:00。")
                return False
        if self.vars["watch_until"].get().strip():
            try:
                datetime.strptime(self.vars["watch_until"].get().strip(), "%H:%M:%S")
            except ValueError:
                messagebox.showerror("时间格式不对", "监控到请填 HH:MM:SS，例如 22:30:00。")
                return False
        ranges = self.vars["time_ranges"].get().strip().replace("，", ",")
        if ranges:
            try:
                for part in [x.strip() for x in ranges.split(",") if x.strip()]:
                    h1, h2 = part.split("-", 1)
                    h1, h2 = int(h1), int(h2)
                    if h1 < 0 or h2 > 24 or h1 >= h2:
                        raise ValueError(part)
            except Exception:
                messagebox.showerror("时段格式不对", "健身房时段请填 HH-HH，可用逗号分隔，例如 14-15 或 14-15,20-21。")
                return False
        return True

    def _base_env(self):
        env = os.environ.copy()
        env["PYTHONIOENCODING"] = "utf-8"
        env["JACCOUNT_USERNAME"] = self.vars["username"].get().strip()
        env["JACCOUNT_PASSWORD"] = self.vars["password"].get()
        env["FANGTANG_KEY"] = self.vars["fangtang"].get().strip()
        env["FEISHU_WEBHOOK"] = self.vars["feishu_webhook"].get().strip()
        env["FEISHU_SECRET"] = self.vars["feishu_secret"].get().strip()
        env["WECOM_WEBHOOK"] = self.vars["wecom_webhook"].get().strip()
        env["HEADLESS"] = "1" if self.vars["headless"].get() else "0"
        env["BROWSER"] = self.vars["browser"].get().strip() or "edge"
        return env

    def _common_args(self, include_target=True):
        args = ["--browser", self.vars["browser"].get().strip() or "edge"]
        if include_target and self.vars["target_date"].get().strip():
            args += ["--target-date", self.vars["target_date"].get().strip()]
        if include_target and self.vars["time_ranges"].get().strip():
            args += ["--time-ranges", self.vars["time_ranges"].get().strip()]
        return args

    def _append_optional_args(self, args):
        mapping = [
            ("parallel_confirm", "--parallel-confirm"),
            ("confirm_batches", "--confirm-batches"),
            ("confirm_jitter", "--confirm-jitter-ms"),
            ("confirm_backoff", "--confirm-backoff-ms"),
            ("confirm_timeout", "--confirm-timeout-s"),
        ]
        for key, flag in mapping:
            value = self.vars[key].get().strip()
            if value:
                args += [flag, value]
        return args

    def _run_login(self):
        if not self._validate_common():
            return
        self._start_command(["--mode", "login"] + self._common_args(include_target=False), "登录")

    def _run_check(self):
        if not self._validate_common():
            return
        args = ["--mode", "check"] + self._common_args(include_target=True)
        self._start_command(args, "检测空位")

    def _run_book(self):
        if not self._validate_common():
            return
        args = ["--mode", "book"] + self._common_args(include_target=True)
        if self.vars["book_at"].get().strip():
            args += ["--at", self.vars["book_at"].get().strip()]
        if self.vars["use_server_time"].get():
            args.append("--use-server-time")
        if self.vars["soft_poll_window"].get().strip():
            args += ["--soft-poll-window", self.vars["soft_poll_window"].get().strip()]
        if self.vars["soft_poll_interval"].get().strip():
            args += ["--soft-poll-interval-ms", self.vars["soft_poll_interval"].get().strip()]
        self._start_command(self._append_optional_args(args), "健身房定点预约")

    def _run_watch(self):
        if not self._validate_common():
            return
        args = ["--mode", "watch"] + self._common_args(include_target=True)
        if self.vars["poll_interval"].get().strip():
            args += ["--poll-interval-ms", self.vars["poll_interval"].get().strip()]
        if self.vars["watch_until"].get().strip():
            args += ["--watch-until", self.vars["watch_until"].get().strip()]
        if self.vars["watch_duration"].get().strip():
            args += ["--watch-duration-min", self.vars["watch_duration"].get().strip()]
        if self.vars["keep_watching"].get():
            args.append("--keep-watching")
        self._start_command(self._append_optional_args(args), "健身房回流抢占")

    def _run_scan7(self):
        if not self._validate_common():
            return
        range_start, range_end = self._normalized_scan_range(show_error=True)
        if not range_start or not range_end:
            return
        args = [
            "--mode", "scan7",
            "--browser", self.vars["browser"].get().strip() or "edge",
            "--range-start", range_start,
            "--range-end", range_end,
        ]
        if self.vars["time_ranges"].get().strip():
            args += ["--time-ranges", self.vars["time_ranges"].get().strip()]
        if self.vars["poll_interval"].get().strip():
            args += ["--poll-interval-ms", self.vars["poll_interval"].get().strip()]
        if self.vars["watch_until"].get().strip():
            args += ["--watch-until", self.vars["watch_until"].get().strip()]
        if self.vars["watch_duration"].get().strip():
            args += ["--watch-duration-min", self.vars["watch_duration"].get().strip()]
        self._start_command(self._append_optional_args(args), "扫描指定日期范围")

    def _start_command(self, args, label):
        if self.process:
            messagebox.showwarning("任务正在运行", "请先停止或等待当前任务结束。")
            return
        if not MAIN_FILE.exists():
            messagebox.showerror("找不到 main.py", str(MAIN_FILE))
            return
        cmd = [_python_for_worker(), "-u", str(MAIN_FILE)] + args
        self._append_output("\n$ " + " ".join(self._display_cmd(cmd)) + "\n")
        self.status.set(f"{label}运行中...")
        creationflags = 0
        if os.name == "nt":
            creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        try:
            self.process = subprocess.Popen(
                cmd,
                cwd=str(BASE_DIR),
                env=self._base_env(),
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
                creationflags=creationflags,
            )
        except Exception as exc:
            self.process = None
            self.status.set("启动失败")
            messagebox.showerror("启动失败", str(exc))
            return
        self.reader_thread = threading.Thread(target=self._read_process_output, daemon=True)
        self.reader_thread.start()

    def _display_cmd(self, cmd):
        redacted = []
        for item in cmd:
            if "JACCOUNT_PASSWORD" in item or "FANGTANG_KEY" in item:
                redacted.append("***")
            elif " " in item:
                redacted.append(f'"{item}"')
            else:
                redacted.append(item)
        return redacted

    def _read_process_output(self):
        proc = self.process
        if not proc or not proc.stdout:
            return
        try:
            for line in proc.stdout:
                self._queue_output(line)
        except Exception as exc:
            self._queue_output(f"\n[读取输出失败] {exc}\n")

    def _stop_process(self):
        if not self.process:
            self.status.set("没有正在运行的任务")
            return
        if not messagebox.askyesno("停止任务", "确定停止当前运行中的任务吗？"):
            return
        try:
            self.process.terminate()
            self.status.set("正在停止任务...")
            self._queue_output("\n[已请求停止当前任务]\n")
        except Exception as exc:
            messagebox.showerror("停止失败", str(exc))

    def _notification_config(self):
        return {
            "FANGTANG_KEY": self.vars["fangtang"].get().strip(),
            "FEISHU_WEBHOOK": self.vars["feishu_webhook"].get().strip(),
            "FEISHU_SECRET": self.vars["feishu_secret"].get().strip(),
            "WECOM_WEBHOOK": self.vars["wecom_webhook"].get().strip(),
        }

    def _send_test_notification(self):
        cfg = self._notification_config()
        if not any(cfg.values()):
            messagebox.showinfo("没有通知配置", "请先填写 Server酱、飞书 Webhook 或企微 Webhook。")
            return
        try:
            from messages import send_notifications
            result = send_notifications("健身房预约助手测试通知", "如果你收到这条消息，说明通知通道配置成功。", cfg)
            self._queue_output(f"\n[测试通知] {result}\n")
            messagebox.showinfo("测试通知已发送", str(result))
        except Exception as exc:
            messagebox.showerror("测试通知失败", str(exc))

    def _save_settings(self):
        data = {
            "username": self.vars["username"].get().strip(),
            "fangtang": self.vars["fangtang"].get().strip(),
            "feishu_webhook": self.vars["feishu_webhook"].get().strip(),
            "feishu_secret": self.vars["feishu_secret"].get().strip(),
            "wecom_webhook": self.vars["wecom_webhook"].get().strip(),
            "browser": self.vars["browser"].get().strip(),
            "headless": bool(self.vars["headless"].get()),
            "target_date": self.vars["target_date"].get().strip(),
            "days_ahead": self.vars["days_ahead"].get().strip(),
            "range_start": self.vars["range_start"].get().strip(),
            "range_end": self.vars["range_end"].get().strip(),
            "time_ranges": self.vars["time_ranges"].get().strip(),
            "book_at": self.vars["book_at"].get().strip(),
            "use_server_time": bool(self.vars["use_server_time"].get()),
            "soft_poll_window": self.vars["soft_poll_window"].get().strip(),
            "soft_poll_interval": self.vars["soft_poll_interval"].get().strip(),
            "poll_interval": self.vars["poll_interval"].get().strip(),
            "watch_until": self.vars["watch_until"].get().strip(),
            "watch_duration": self.vars["watch_duration"].get().strip(),
            "keep_watching": bool(self.vars["keep_watching"].get()),
            "parallel_confirm": self.vars["parallel_confirm"].get().strip(),
            "confirm_batches": self.vars["confirm_batches"].get().strip(),
            "confirm_jitter": self.vars["confirm_jitter"].get().strip(),
            "confirm_backoff": self.vars["confirm_backoff"].get().strip(),
            "confirm_timeout": self.vars["confirm_timeout"].get().strip(),
            "save_password": bool(self.vars["save_password"].get()),
        }
        if self.vars["save_password"].get():
            data["password"] = self.vars["password"].get()
        try:
            SETTINGS_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
            messagebox.showinfo("已保存", f"设置已保存到：\n{SETTINGS_FILE}")
        except Exception as exc:
            messagebox.showerror("保存失败", str(exc))

    def _load_settings(self, silent=False):
        if not SETTINGS_FILE.exists():
            if not silent:
                messagebox.showinfo("没有设置文件", f"还没有 {SETTINGS_FILE.name}。")
            return
        try:
            data = json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
            for key, var in self.vars.items():
                if key in data:
                    var.set(data[key])
            if "password" in data:
                self.vars["password"].set(data["password"])
                self.vars["save_password"].set(True)
            if not silent:
                messagebox.showinfo("已读取", "设置已载入。")
        except Exception as exc:
            if not silent:
                messagebox.showerror("读取失败", str(exc))

    def _refresh_calendar(self):
        if not hasattr(self, "calendar"):
            return
        try:
            from booking_state import calendar_rows, training_stats
            range_start, range_end = self._normalized_scan_range(show_error=False)
            if not range_start or not range_end:
                raise ValueError("扫描开始和扫描结束格式应为 YYYY-MM-DD")
            rows = calendar_rows(
                start_date=range_start,
                end_date=range_end,
                include_history_booked=True,
            )
            stats = training_stats()
        except Exception as exc:
            rows = [{
                "date": "",
                "weekday": "",
                "status": "读取失败",
                "updated_at": "",
                "detail": str(exc),
            }]
            stats = {"earliest": None, "total": 0, "week": 0, "month": 0}
        if hasattr(self, "calendar_summary"):
            if stats.get("earliest"):
                total_line = f"累计：自 {stats['earliest']} 起已训练 {stats['total']} 次"
            else:
                total_line = "累计：暂无已训练记录"
            self.calendar_summary.set(
                total_line +
                f"\n本周：{stats.get('week', 0)} 次" +
                f"\n本月：{stats.get('month', 0)} 次"
            )
        selected = self.calendar.selection()
        selected_date = None
        if selected:
            try:
                selected_date = self.calendar.item(selected[0], "values")[0]
            except Exception:
                selected_date = None
        for item in self.calendar.get_children():
            self.calendar.delete(item)
        restore_id = None
        for row in rows:
            weekday = WEEKDAY_NAMES.get(row.get("weekday", ""), row.get("weekday", ""))
            iid = row.get("date") or None
            values = (
                row.get("date", ""),
                weekday,
                row.get("status", ""),
                row.get("updated_at", ""),
                row.get("detail", ""),
            )
            inserted = self.calendar.insert("", "end", iid=iid, values=values)
            if selected_date and row.get("date") == selected_date:
                restore_id = inserted
        if restore_id:
            self.calendar.selection_set(restore_id)

    def _refresh_calendar_periodic(self):
        self._refresh_calendar()
        self.after(5000, self._refresh_calendar_periodic)

    def _toggle_selected_calendar_day(self):
        if not hasattr(self, "calendar"):
            return
        selected = self.calendar.selection()
        if not selected:
            messagebox.showinfo("未选择日期", "请先在日历里选中一个日期。")
            return
        date = self.calendar.item(selected[0], "values")[0]
        current_status = self.calendar.item(selected[0], "values")[2]
        if not date:
            return
        try:
            from booking_state import toggle_date_booking
            toggle_date_booking(date)
            self._refresh_calendar()
            if current_status == "已约":
                self._queue_output(f"\n[日历] 已将 {date} 切换为空状态。\n")
            else:
                self._queue_output(f"\n[日历] 已将 {date} 切换为 booked 状态。\n")
        except Exception as exc:
            messagebox.showerror("切换失败", str(exc))

    def _open_file(self, path):
        if not path.exists():
            messagebox.showinfo("文件不存在", str(path))
            return
        try:
            os.startfile(str(path))
        except Exception as exc:
            messagebox.showerror("打开失败", str(exc))

    def _on_close(self):
        if self.process:
            if not messagebox.askyesno("任务仍在运行", "关闭窗口会停止当前任务，确定关闭吗？"):
                return
            try:
                self.process.terminate()
            except Exception:
                pass
        self.destroy()


if __name__ == "__main__":
    app = BookerApp()
    app.mainloop()
