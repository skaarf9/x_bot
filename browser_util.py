"""浏览器探测、调试端口管理、登录态检测（monitor 和 GUI 共用）。"""
from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import time
from pathlib import Path

import requests

from cdp_client import Browser

BASE = Path(__file__).resolve().parent
RUNNING_FILE = BASE / "runtime.json"

# 常见 Chromium 系浏览器位置（Edge / Chrome / Brave / Chromium）
CANDIDATES = [
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    os.path.expandvars(r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe"),
    r"C:\Program Files\BraveSoftware\Brave-Browser\Application\brave.exe",
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
    "/usr/bin/google-chrome",
    "/usr/bin/google-chrome-stable",
    "/usr/bin/chromium",
    "/usr/bin/chromium-browser",
    "/usr/bin/microsoft-edge",
]

WHICH_NAMES = ("msedge", "microsoft-edge", "google-chrome", "google-chrome-stable",
               "chromium", "chromium-browser", "brave", "brave-browser")


def detect_browser_exe(configured: str = "") -> str | None:
    if configured:
        p = Path(os.path.expandvars(os.path.expanduser(configured)))
        if p.is_file():
            return str(p)
    for c in CANDIDATES:
        if Path(c).is_file():
            return c
    for name in WHICH_NAMES:
        found = shutil.which(name)
        if found:
            return found
    return None


def port_open(port: int, host: str = "127.0.0.1", timeout: float = 0.3) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def devtools_alive(port: int, host: str = "127.0.0.1") -> bool:
    try:
        r = requests.get(f"http://{host}:{port}/json/version", timeout=2)
        return r.ok and "webSocketDebuggerUrl" in r.json()
    except Exception:
        return False


def choose_port(preferred: int) -> int:
    """优先用配置端口；被非 DevTools 程序占用就往后找空闲端口。"""
    if devtools_alive(preferred):
        return preferred
    if not port_open(preferred):
        return preferred
    for p in range(preferred + 1, preferred + 21):
        if not port_open(p):
            return p
    raise RuntimeError(f"{preferred}~{preferred + 20} 端口都被占用了")


def read_runtime() -> dict:
    try:
        return json.loads(RUNNING_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def write_runtime(data: dict):
    try:
        RUNNING_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:
        pass


def ensure_browser(port: int, profile: Path, exe: str = "", timeout: int = 40) -> tuple[Browser, int]:
    """连上已有的调试端口；没有就用专用 profile 启动浏览器。返回 (Browser, 实际端口)。"""
    if devtools_alive(port):
        write_runtime({"port": port, "profile": str(profile), "exe": exe})
        return Browser(port), port

    real_port = choose_port(port)
    exe = detect_browser_exe(exe)
    if not exe:
        raise RuntimeError("没有找到可用的浏览器（Edge/Chrome/Chromium/Brave 都行），请在 config.json 里填 browser.exe")

    profile = Path(profile).expanduser().resolve()
    profile.mkdir(parents=True, exist_ok=True)
    args = [
        exe,
        f"--user-data-dir={profile}",
        f"--remote-debugging-port={real_port}",
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-extensions",        # 监控 profile 不需要任何扩展
        "--disable-sync",              # 别把用户的扩展/书签同步进来，profile 会越滚越大
        "--disable-component-update",  # 别重新下几百 MB 的组件缓存
        "--disable-background-timer-throttling",
        "--disable-renderer-backgrounding",
        "--disable-backgrounding-occluded-windows",
        "--window-size=1280,900",
        "about:blank",
    ]
    subprocess.Popen(args, close_fds=True, stdout=subprocess.DEVNULL,
                     stderr=subprocess.DEVNULL,
                     creationflags=getattr(subprocess, "DETACHED_PROCESS", 0))
    deadline = time.time() + timeout
    while time.time() < deadline:
        time.sleep(1)
        if devtools_alive(real_port):
            write_runtime({"port": real_port, "profile": str(profile), "exe": exe})
            return Browser(real_port), real_port
    raise RuntimeError(f"浏览器调试端口 {real_port} 没有起来（可能被安全软件拦截）")


def is_logged_in(browser: Browser) -> bool:
    try:
        cookies = browser.cookies()
    except Exception:
        return False
    return any(c.get("name") == "auth_token" and "x.com" in (c.get("domain") or "")
               for c in cookies)


def resolve_settings(cfg: dict) -> dict:
    """把新旧两种配置格式统一成内部结构。"""
    b = cfg.get("browser") or {}
    profile = b.get("profile_dir") or cfg.get("edge_profile") or "edge-monitor"
    p = Path(os.path.expandvars(str(profile)))
    if not p.is_absolute():
        p = BASE / p
    return {
        "port": int(b.get("debug_port") or cfg.get("edge_port") or 9222),
        "profile": p,
        "exe": b.get("exe") or cfg.get("edge_exe") or "",
    }


def open_login_tab(port: int) -> bool:
    """在监控浏览器里打开 X 登录页（不关闭，留给你登录）。"""
    try:
        b = Browser(port)
        b.new_page("https://x.com/login")
        b.close()
        return True
    except Exception:
        return False
