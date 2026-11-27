#!/usr/bin/env python
"""X 监听管理器（Tkinter，零额外依赖）。

功能：增删改监控用户、改轮询间隔、启停后台监听、查看登录/任务状态和日志。
修改会写入 config.json，正在运行的监听器会在下一轮自动读取。
"""
from __future__ import annotations

import json
import os
import queue
import re
import shutil
import subprocess
import sys
import threading
import time
import tkinter as tk
import webbrowser
from pathlib import Path
from tkinter import messagebox, ttk

import browser_util
import updater
import version
import x_monitor
from telegram_notify import TelegramError, TelegramNotifier

BASE = Path(__file__).resolve().parent
CFG_FILE = BASE / "config.json"
STATE_FILE = BASE / "state.json"
LOG_FILE = BASE / "monitor.log"
TRIGGER_FILE = BASE / "trigger.flag"
TASK_NAME = "X-Monitor"
CREATE_NO_WINDOW = 0x08000000
NAME_RE = re.compile(r"^[A-Za-z0-9_]{1,15}$")

STATE_TEXT = {"Running": "运行中", "Ready": "已就绪", "Disabled": "已禁用",
              "未安装": "未安装", "Unknown": "未知"}


def dir_size(path: Path) -> int:
    total = 0
    for root, _dirs, files in os.walk(path):
        for name in files:
            try:
                total += os.path.getsize(os.path.join(root, name))
            except OSError:
                pass
    return total


def run_cmd(cmd):
    return subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                          errors="replace", creationflags=CREATE_NO_WINDOW)


def task_exists() -> bool:
    return run_cmd(["schtasks", "/query", "/tn", TASK_NAME]).returncode == 0


def task_state() -> str:
    if not task_exists():
        return "未安装"
    r = run_cmd(["powershell", "-NoProfile", "-Command",
                 f"[string](Get-ScheduledTask -TaskName {TASK_NAME}).State"])
    return (r.stdout or "").strip() or "未知"


def install_task():
    return run_cmd(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass",
                    "-File", str(BASE / "install_autostart.ps1")])


def start_task():
    if not task_exists():
        install_task()
    return run_cmd(["schtasks", "/run", "/tn", TASK_NAME])


def stop_task():
    return run_cmd(["schtasks", "/end", "/tn", TASK_NAME])


class UserDialog(tk.Toplevel):
    def __init__(self, master, user: dict | None = None, taken: set[str] | None = None,
                 edited: str = ""):
        super().__init__(master)
        self.title("编辑用户" if user else "添加用户")
        self.result = None
        self.taken = taken or set()
        self.edited = edited.lower()
        self.resizable(False, False)
        self.transient(master)

        tk.Label(self, text="用户名（@ 后面的部分）").grid(row=0, column=0, sticky="w", padx=12, pady=(12, 2))
        self.name_var = tk.StringVar(value=(user or {}).get("screen_name", ""))
        entry = ttk.Entry(self, textvariable=self.name_var, width=32)
        entry.grid(row=1, column=0, padx=12, pady=(0, 8))

        self.replies_var = tk.BooleanVar(value=bool((user or {}).get("include_replies", False)))
        self.retweet_var = tk.BooleanVar(value=bool((user or {}).get("skip_retweets", True)))
        ttk.Checkbutton(self, text="包含回复（会改抓 /with_replies，请求量翻倍）",
                        variable=self.replies_var).grid(row=2, column=0, sticky="w", padx=12)
        ttk.Checkbutton(self, text="跳过纯转推（不推送别人转的内容）",
                        variable=self.retweet_var).grid(row=3, column=0, sticky="w", padx=12)

        btns = ttk.Frame(self)
        btns.grid(row=4, column=0, sticky="e", padx=12, pady=12)
        ttk.Button(btns, text="取消", command=self.destroy).pack(side="right", padx=4)
        ttk.Button(btns, text="确定", command=self._ok).pack(side="right")

        entry.focus_set()
        self.bind("<Return>", lambda _e: self._ok())
        self.bind("<Escape>", lambda _e: self.destroy())
        self.grab_set()
        master.wait_window(self)

    def _ok(self):
        name = self.name_var.get().strip().lstrip("@")
        if not NAME_RE.match(name):
            messagebox.showwarning("用户名不合法", "只允许字母、数字、下划线，1~15 位。", parent=self)
            return
        if name.lower() in self.taken and name.lower() != self.edited:
            messagebox.showwarning("重复", f"@{name} 已经在列表里了。", parent=self)
            return
        self.result = {
            "screen_name": name,
            "include_replies": bool(self.replies_var.get()),
            "skip_retweets": bool(self.retweet_var.get()),
        }
        self.destroy()


class NotifyDialog(tk.Toplevel):
    """Telegram / 仅日志 的推送设置。"""

    def __init__(self, master, cfg: dict):
        super().__init__(master)
        self.cfg = cfg
        self.title("通知设置")
        self.resizable(False, False)
        self.transient(master)
        tg = cfg.get("telegram") or {}
        self.mode = tk.StringVar(value="telegram" if tg.get("enabled") else "log")
        self.token_var = tk.StringVar(value=tg.get("token", ""))
        self.chat_var = tk.StringVar(value=str(tg.get("chat_id", "") or ""))
        self.proxy_var = tk.StringVar(value=tg.get("proxy", ""))
        self.show_token = tk.BooleanVar(value=False)

        frm = ttk.Frame(self, padding=12)
        frm.pack(fill="both", expand=True)
        frm.columnconfigure(1, weight=1)

        ttk.Label(frm, text="通道").grid(row=0, column=0, sticky="w")
        box = ttk.Frame(frm)
        box.grid(row=0, column=1, columnspan=2, sticky="w")
        ttk.Radiobutton(box, text="Telegram", value="telegram",
                        variable=self.mode).pack(side="left")
        ttk.Radiobutton(box, text="仅写日志", value="log",
                        variable=self.mode).pack(side="left", padx=12)

        ttk.Label(frm, text="Bot Token").grid(row=1, column=0, sticky="w", pady=4)
        self.token_entry = ttk.Entry(frm, textvariable=self.token_var, width=54, show="*")
        self.token_entry.grid(row=1, column=1, sticky="we", pady=4)
        ttk.Checkbutton(frm, text="显示", variable=self.show_token,
                        command=self._toggle_token).grid(row=1, column=2, padx=6)

        ttk.Label(frm, text="Chat ID（多个用逗号）").grid(row=2, column=0, sticky="w", pady=4)
        ttk.Entry(frm, textvariable=self.chat_var, width=54).grid(row=2, column=1, sticky="we", pady=4)
        ttk.Button(frm, text="获取", width=6, command=self.on_fetch_chat).grid(row=2, column=2, padx=6, pady=4)

        ttk.Label(frm, text="代理").grid(row=3, column=0, sticky="w", pady=4)
        ttk.Entry(frm, textvariable=self.proxy_var, width=54).grid(row=3, column=1, sticky="we", pady=4)
        ttk.Label(frm, text="留空 = 跟随系统代理；需要代理时填 http://127.0.0.1:7890",
                  foreground="#888").grid(row=4, column=1, sticky="w")

        hint = ("创建：Telegram 搜 @BotFather → /newbot → 起名 → 拿到 token；再打开自己的 bot 发 /start。\n"
                "用群聊：把 bot 拉进群 → 在群里发 /start@你的bot → 点「获取」，填的是群的 chat_id。")
        tk.Label(frm, text=hint, wraplength=520, justify="left", fg="#555").grid(
            row=5, column=0, columnspan=3, sticky="w", pady=(8, 4))

        self.status = tk.Label(frm, text="", anchor="w", wraplength=520, justify="left")
        self.status.grid(row=6, column=0, columnspan=3, sticky="w", pady=4)

        btns = ttk.Frame(frm)
        btns.grid(row=7, column=0, columnspan=3, sticky="e", pady=(8, 0))
        ttk.Button(btns, text="关闭", command=self.destroy).pack(side="right", padx=4)
        ttk.Button(btns, text="保存", command=self.on_save).pack(side="right", padx=4)
        ttk.Button(btns, text="发送测试", command=self.on_test).pack(side="right", padx=4)
        ttk.Button(btns, text="打开 BotFather", command=self.on_open_botfather).pack(side="left", padx=4)

        self.grab_set()
        master.wait_window(self)

    def _toggle_token(self):
        self.token_entry.configure(show="" if self.show_token.get() else "*")

    def _set_status(self, text, ok=None):
        color = "#1a7f37" if ok else ("#c0392b" if ok is False else "#333")
        self.status.config(text=text, fg=color)

    def _async(self, fn, done):
        def work():
            try:
                res = fn()
            except Exception as e:  # noqa: BLE001
                msg = str(e)
                self.after(0, lambda: done(None, msg))
                return
            self.after(0, lambda: done(res, None))
        threading.Thread(target=work, daemon=True).start()

    def _notifier(self) -> TelegramNotifier:
        return TelegramNotifier(self.token_var.get().strip(), self.chat_var.get().strip(),
                                self.proxy_var.get().strip())

    def on_open_botfather(self):
        try:
            os.startfile("tg://resolve?domain=BotFather")  # noqa: S606
            self._set_status("已在 Telegram 里打开 @BotFather，发送 /newbot 开始创建", ok=True)
        except Exception as e:  # noqa: BLE001
            self._set_status("打开 Telegram 失败：" + str(e), ok=False)

    def on_fetch_chat(self):
        if not self.token_var.get().strip():
            self._set_status("先填 Bot Token，再点获取", ok=False)
            return
        self._set_status("正在获取 chat_id …（需要你已经给 bot 发过 /start）")
        n = TelegramNotifier(self.token_var.get().strip(), "", self.proxy_var.get().strip())

        def done(res, err):
            if err:
                self._set_status("获取失败：" + err, ok=False)
                return
            cid, who = res
            self.chat_var.set(cid)
            suffix = f"（{who}）" if who else ""
            self._set_status(f"已获取 chat_id={cid}{suffix}", ok=True)

        self._async(n.find_chat_id, done)

    def on_test(self):
        n = self._notifier()
        if not n.token or not n.chat_id:
            self._set_status("Token 和 Chat ID 都要填", ok=False)
            return
        self._set_status("正在发送测试消息 …")

        def done(res, err):
            if err:
                self._set_status("发送失败：" + err, ok=False)
                return
            self._set_status(f"发送成功（@{res}），去 Telegram 看看", ok=True)

        self._async(n.test, done)

    def on_save(self):
        mode = self.mode.get()
        tg = self.cfg.setdefault("telegram", {})
        tg.update({
            "enabled": mode == "telegram",
            "token": self.token_var.get().strip(),
            "chat_id": self.chat_var.get().strip(),
            "proxy": self.proxy_var.get().strip(),
        })
        if mode == "telegram" and not (tg["token"] and tg["chat_id"]):
            self._set_status("选 Telegram 的话，Token 和 Chat ID 都要填好", ok=False)
            return
        if mode == "log":
            self.cfg.setdefault("bark", {})["enabled"] = False
        try:
            CFG_FILE.write_text(json.dumps(self.cfg, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception as e:  # noqa: BLE001
            self._set_status("保存失败：" + str(e), ok=False)
            return
        self._set_status("已保存，下一轮生效", ok=True)
        self.after(800, self.destroy)


class App:
    def __init__(self, root: tk.Tk):
        self.root = root
        x_monitor.ensure_config()
        self.cfg = x_monitor.load_config()
        self.state = {}
        self.q: queue.Queue = queue.Queue()
        self.worker_running = True
        self.last_status: dict = {}
        self.update_link = ""
        self.update_var = tk.StringVar(value="")

        root.title("X 监听管理")
        root.geometry("1000x720")
        root.minsize(820, 560)
        self._build_ui()
        self._load_state()
        self.refresh_notify_label()
        self.refresh_table()
        self.refresh_log()

        threading.Thread(target=self._status_worker, daemon=True).start()
        self.root.after(300, self._drain_queue)
        self.root.after(2000, self._tick_log)
        self.root.after(5000, self._tick_table)
        self.root.after(1500, lambda: self._check_updates(force=False))
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

    # ---------------- UI ----------------
    def _build_ui(self):
        pad = {"padx": 8, "pady": 6}

        status = ttk.LabelFrame(self.root, text="运行状态")
        status.pack(fill="x", padx=10, pady=(10, 4))

        self.task_var = tk.StringVar(value="任务: 检查中…")
        self.login_var = tk.StringVar(value="登录: 检查中…")
        self.browser_var = tk.StringVar(value="浏览器: -")
        self.task_label = tk.Label(status, textvariable=self.task_var, anchor="w")
        self.login_label = tk.Label(status, textvariable=self.login_var, anchor="w")
        tk.Label(status, textvariable=self.browser_var, anchor="w").grid(row=0, column=2, sticky="w", padx=12, pady=6)
        self.task_label.grid(row=0, column=0, sticky="w", padx=12, pady=6)
        self.login_label.grid(row=0, column=1, sticky="w", padx=12, pady=6)
        self.notify_var = tk.StringVar(value="推送: -")
        tk.Label(status, textvariable=self.notify_var, anchor="w").grid(
            row=0, column=3, sticky="w", padx=12, pady=6)

        btns = ttk.Frame(status)
        btns.grid(row=1, column=0, columnspan=3, sticky="w", padx=8, pady=(0, 8))
        ttk.Button(btns, text="启动监听", command=self.on_start).pack(side="left", padx=3)
        ttk.Button(btns, text="停止监听", command=self.on_stop).pack(side="left", padx=3)
        ttk.Button(btns, text="立即检查", command=self.on_check_now).pack(side="left", padx=3)
        ttk.Button(btns, text="打开登录页", command=self.on_login).pack(side="left", padx=3)
        ttk.Button(btns, text="安装/修复自启", command=self.on_install).pack(side="left", padx=3)
        ttk.Button(btns, text="通知设置", command=self.on_notify).pack(side="left", padx=12)
        ttk.Button(btns, text="清理缓存", command=self.on_clean_cache).pack(side="left", padx=3)

        users = ttk.LabelFrame(self.root, text="监控用户")
        users.pack(fill="both", expand=True, padx=10, pady=4)

        cols = ("name", "replies", "retweets", "last_id", "updated")
        self.tree = ttk.Treeview(users, columns=cols, show="headings", height=8)
        for cid, text, width in (
            ("name", "用户名", 200), ("replies", "含回复", 70),
            ("retweets", "跳过转推", 80), ("last_id", "最后推文 ID", 200),
            ("updated", "最后检查时间", 180),
        ):
            self.tree.heading(cid, text=text)
            self.tree.column(cid, width=width, anchor="w" if cid != "replies" else "center")
        self.tree.pack(side="left", fill="both", expand=True, padx=(8, 0), pady=8)
        sb = ttk.Scrollbar(users, orient="vertical", command=self.tree.yview)
        sb.pack(side="left", fill="y", pady=8)
        self.tree.configure(yscrollcommand=sb.set)
        self.tree.bind("<Double-1>", lambda _e: self.on_edit())

        side = ttk.Frame(users)
        side.pack(side="left", fill="y", padx=8, pady=8)
        for text, cmd in (("添加", self.on_add), ("编辑", self.on_edit), ("删除", self.on_delete)):
            ttk.Button(side, text=text, command=cmd, width=12).pack(pady=3)

        opts = ttk.LabelFrame(self.root, text="设置")
        opts.pack(fill="x", padx=10, pady=4)
        ttk.Label(opts, text="轮询间隔（秒）").pack(side="left", padx=(12, 4), pady=8)
        self.interval_var = tk.StringVar(value=str(self.cfg.get("interval_seconds", 90)))
        ttk.Spinbox(opts, from_=20, to=3600, increment=10, width=8,
                    textvariable=self.interval_var).pack(side="left", pady=8)
        ttk.Button(opts, text="保存", command=self.on_save_interval).pack(side="left", padx=8)
        ttk.Label(opts, text="（用户越多间隔要越大，建议 用户数 × 30 秒起）",
                  foreground="#888").pack(side="left", padx=4)

        logf = ttk.LabelFrame(self.root, text="日志（最近 200 行）")
        logf.pack(fill="both", expand=True, padx=10, pady=(4, 10))
        self.log_text = tk.Text(logf, height=10, wrap="none", state="disabled",
                                font=("Consolas", 9) if sys.platform == "win32" else ("Menlo", 9))
        self.log_text.pack(side="left", fill="both", expand=True, padx=(8, 0), pady=8)
        lsb = ttk.Scrollbar(logf, orient="vertical", command=self.log_text.yview)
        lsb.pack(side="left", fill="y", pady=8)
        self.log_text.configure(yscrollcommand=lsb.set)

        bottom = ttk.Frame(self.root)
        bottom.pack(fill="x", padx=10, pady=(0, 8))
        ttk.Label(bottom, text=f"配置: {CFG_FILE}", foreground="#888").pack(side="left")
        ttk.Button(bottom, text="打开目录", command=self.on_open_dir).pack(side="right")
        self.update_label = tk.Label(bottom, textvariable=self.update_var, fg="#b26a00",
                                     cursor="hand2")
        self.update_label.pack(side="right", padx=10)
        self.update_label.bind("<Button-1>", lambda _e: self._open_update())
        self.version_label = tk.Label(bottom, text=f"v{version.__version__}", fg="#666",
                                      cursor="hand2")
        self.version_label.pack(side="right", padx=(0, 12))
        self.version_label.bind("<Button-1>", lambda _e: self._check_updates(force=True))

    # ---------------- 数据 ----------------
    def _load_state(self):
        try:
            self.state = json.loads(STATE_FILE.read_text(encoding="utf-8")).get("users", {})
        except Exception:
            self.state = {}

    def refresh_table(self):
        sel = self.tree.selection()
        keep = self.tree.item(sel[0], "values")[0] if sel else None
        for row in self.tree.get_children():
            self.tree.delete(row)
        for u in self.cfg.get("users", []):
            name = u["screen_name"]
            st = self.state.get(name) or {}
            self.tree.insert("", "end", values=(
                name,
                "是" if u.get("include_replies") else "否",
                "是" if u.get("skip_retweets") else "否",
                st.get("last_id") or "-",
                (st.get("updated") or "-")[:19].replace("T", " "),
            ))
        if keep:
            for row in self.tree.get_children():
                if self.tree.item(row, "values")[0] == keep:
                    self.tree.selection_set(row)
                    break

    def selected_name(self) -> str | None:
        sel = self.tree.selection()
        if not sel:
            return None
        return self.tree.item(sel[0], "values")[0]

    def save_config(self):
        try:
            CFG_FILE.write_text(json.dumps(self.cfg, ensure_ascii=False, indent=2), encoding="utf-8")
            return True
        except Exception as e:  # noqa: BLE001
            messagebox.showerror("保存失败", str(e))
            return False

    # ---------------- 用户操作 ----------------
    def on_add(self):
        taken = {u["screen_name"].lower() for u in self.cfg.get("users", [])}
        dlg = UserDialog(self.root, None, taken)
        if not dlg.result:
            return
        self.cfg.setdefault("users", []).append(dlg.result)
        if self.save_config():
            self.refresh_table()
            self._note(f"已添加 @{dlg.result['screen_name']}，下一轮生效")

    def on_edit(self):
        name = self.selected_name()
        if not name:
            messagebox.showinfo("提示", "先在列表里选一个用户。")
            return
        users = self.cfg.get("users", [])
        idx = next((i for i, u in enumerate(users) if u["screen_name"] == name), None)
        if idx is None:
            return
        taken = {u["screen_name"].lower() for u in users}
        dlg = UserDialog(self.root, users[idx], taken, edited=name)
        if not dlg.result:
            return
        users[idx] = dlg.result
        if self.save_config():
            self.refresh_table()
            self._note(f"已更新 @{dlg.result['screen_name']}，下一轮生效")

    def on_delete(self):
        name = self.selected_name()
        if not name:
            messagebox.showinfo("提示", "先在列表里选一个用户。")
            return
        if not messagebox.askyesno("删除", f"不再监控 @{name}？"):
            return
        self.cfg["users"] = [u for u in self.cfg.get("users", [])
                             if u["screen_name"] != name]
        if self.save_config():
            self.refresh_table()
            self._note(f"已移除 @{name}")

    def on_save_interval(self):
        try:
            v = int(self.interval_var.get())
            if not 10 <= v <= 3600:
                raise ValueError
        except ValueError:
            messagebox.showwarning("间隔不合法", "请输入 10~3600 之间的整数秒。")
            return
        self.cfg["interval_seconds"] = v
        if self.save_config():
            self._note(f"轮询间隔已改为 {v} 秒，下一轮生效")

    # ---------------- 动作 ----------------
    def on_start(self):
        if not task_exists():
            r = install_task()
            if r.returncode != 0:
                messagebox.showerror("安装失败", (r.stderr or r.stdout or "")[:500])
                return
        r = start_task()
        if r.returncode != 0:
            messagebox.showerror("启动失败", (r.stderr or r.stdout or "")[:500])
        else:
            self._note("已启动后台监听")

    def on_stop(self):
        r = stop_task()
        if r.returncode != 0:
            messagebox.showerror("停止失败", (r.stderr or r.stdout or "")[:500])
        else:
            self._note("已停止后台监听")

    def refresh_notify_label(self):
        tg = self.cfg.get("telegram") or {}
        if tg.get("enabled") and tg.get("token"):
            self.notify_var.set("推送: Telegram" + ("" if tg.get("chat_id") else "（缺 chat_id）"))
        else:
            self.notify_var.set("推送: 仅日志")

    CACHE_DIRS = ("Default/Cache", "Default/Code Cache", "Default/GPUCache",
                  "Default/Service Worker", "Default/Media Cache",
                  "Default/DawnGraphiteCache", "Default/DawnWebGPUCache",
                  "GPUPersistentCache", "component_crx_cache")

    def on_clean_cache(self):
        if not messagebox.askyesno(
            "清理缓存",
            "会短暂停止监听（约 10 秒）并清掉监控浏览器的缓存，不影响 X 登录。继续？",
        ):
            return
        self._note("正在清理监控浏览器缓存 …")
        stop_task()
        run_cmd(["powershell", "-NoProfile", "-Command",
                 "Get-CimInstance Win32_Process -Filter \"name='msedge.exe'\" | "
                 "Where-Object { $_.CommandLine -like '*edge-monitor*' } | "
                 "ForEach-Object { Stop-Process -Id $_.ProcessId -Force }"])
        time.sleep(3)
        profile = Path(browser_util.resolve_settings(self.cfg)["profile"])
        freed = 0
        for rel in self.CACHE_DIRS:
            target = profile / rel
            if target.exists():
                freed += dir_size(target)
                shutil.rmtree(target, ignore_errors=True)
        start_task()
        time.sleep(2)
        self._note(f"缓存已清理，释放约 {freed / 1024 / 1024:.0f} MB，监听已重启")

    def on_notify(self):
        NotifyDialog(self.root, self.cfg)
        self.refresh_notify_label()

    def on_install(self):
        r = install_task()
        if r.returncode != 0:
            messagebox.showerror("安装失败", (r.stderr or r.stdout or "")[:800])
        else:
            self._note("自启任务已安装/修复，并已启动")
    
    def on_check_now(self):
        try:
            TRIGGER_FILE.write_text(str(time.time()), encoding="utf-8")
            self._note("已通知监听器立即检查（若在运行，约 1 秒内生效）")
        except Exception as e:  # noqa: BLE001
            messagebox.showerror("失败", str(e))

    def on_login(self):
        port = self._current_port()
        if not browser_util.devtools_alive(port):
            messagebox.showinfo("提示", "监控浏览器没在运行。点「▶ 启动监听」后它会自动打开登录页。")
            return
        if browser_util.open_login_tab(port):
            self._note("已在监控浏览器里打开 X 登录页")
        else:
            messagebox.showerror("失败", "打开登录页失败，请查看日志。")

    def on_open_dir(self):
        try:
            os.startfile(str(BASE))  # noqa: S606
        except Exception:
            pass

    # ---------------- 状态轮询 ----------------
    def _current_port(self) -> int:
        rt = browser_util.read_runtime()
        if rt.get("port"):
            return int(rt["port"])
        return browser_util.resolve_settings(self.cfg)["port"]

    def _status_worker(self):
        cached_task = "未知"
        last_task_check = 0.0
        while self.worker_running:
            now = time.time()
            if now - last_task_check > 15:
                cached_task = task_state()
                last_task_check = now
            info = {"task": cached_task, "logged": None, "browser": "-", "port": self._current_port()}
            try:
                if browser_util.devtools_alive(info["port"]):
                    b = browser_util.Browser(info["port"])
                    info["browser"] = b.version
                    info["logged"] = browser_util.is_logged_in(b)
                    b.close()
            except Exception:
                pass
            self.q.put(info)
            for _ in range(40):
                if not self.worker_running:
                    return
                time.sleep(0.1)

    def _drain_queue(self):
        try:
            while True:
                self.last_status = self.q.get_nowait()
                self._apply_status(self.last_status)
        except queue.Empty:
            pass
        self.root.after(400, self._drain_queue)

    def _apply_status(self, s: dict):
        state = s.get("task", "未知")
        self.task_var.set(f"后台任务: {STATE_TEXT.get(state, state)}")
        logged = s.get("logged")
        if logged is None:
            self.login_var.set("登录: 浏览器未运行")
            self.login_label.config(fg="#c0392b")
        elif logged:
            self.login_var.set("登录: 已登录")
            self.login_label.config(fg="#1a7f37")
        else:
            self.login_var.set("登录: 未登录（请在监控浏览器登录）")
            self.login_label.config(fg="#c0392b")
        browser = s.get("browser", "-")
        self.browser_var.set(f"浏览器: {browser}  (端口 {s.get('port')})")

    # ---------------- 日志 ----------------
    def refresh_log(self):
        try:
            with open(LOG_FILE, encoding="utf-8", errors="replace") as f:
                lines = f.readlines()[-200:]
        except FileNotFoundError:
            lines = []
        text = "".join(lines)
        self.log_text.configure(state="normal")
        if self.log_text.get("1.0", "end").strip() != text.strip():
            self.log_text.delete("1.0", "end")
            self.log_text.insert("end", text)
            self.log_text.see("end")
        self.log_text.configure(state="disabled")

    def _tick_log(self):
        self.refresh_log()
        self.root.after(2000, self._tick_log)

    def _tick_table(self):
        self._load_state()
        self.refresh_notify_label()
        self.refresh_table()
        self.root.after(5000, self._tick_table)

    def _note(self, msg: str):
        self.log_text.configure(state="normal")
        self.log_text.insert("end", f"\n[GUI] {time.strftime('%H:%M:%S')} {msg}\n")
        self.log_text.see("end")
        self.log_text.configure(state="disabled")

    # ---------------- 版本更新 ----------------
    def _check_updates(self, force: bool = False):
        threading.Thread(target=self._check_updates_worker, args=(force,),
                         daemon=True).start()

    def _check_updates_worker(self, force: bool):
        try:
            info = updater.check_update(force=force)
        except Exception:  # noqa: BLE001
            info = None
        self.root.after(0, lambda: self._apply_update(info))

    def _apply_update(self, info):
        if not info:
            return
        if info.get("newer"):
            self.update_link = info.get("url", "")
            self.update_var.set(f"发现新版本 v{info['latest']}，点击查看")
            self._note(f"发现新版本 v{info['latest']}（当前 v{info['current']}）")
        else:
            self.update_var.set("已是最新")

    def _open_update(self):
        if self.update_link:
            webbrowser.open(self.update_link)

    def _on_close(self):
        self.worker_running = False
        self.root.destroy()


def main():
    root = tk.Tk()
    try:
        ttk.Style().theme_use("vista" if sys.platform == "win32" else "clam")
    except Exception:
        pass
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()
