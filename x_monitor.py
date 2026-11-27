#!/usr/bin/env python
"""X 新推文监听器（CDP 版）

原理：用远程调试端口接上登录过 X 的 Chromium 系浏览器（Edge/Chrome/Brave 均可），
打开用户主页，截获页面自身发出的时间线响应（UserOriginalsTimeline /
UserRepliesTimeline）。不伪造任何 header / query-id / transaction-id。

用法：
  python x_monitor.py              # 常驻监听（配置文件改动会自动生效）
  python x_monitor.py --once       # 跑一轮就退出
  python x_monitor.py --check      # 只检查浏览器和登录态
"""
from __future__ import annotations

import argparse
import json
import logging
import logging.handlers
import os
import shutil
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import browser_util
import updater
from bark_notify import BarkNotifier
from cdp_client import Browser, CDPError, Page
from telegram_notify import TelegramNotifier
from version import __version__
from x_parser import newest, parse_timeline

BASE = Path(__file__).resolve().parent
CFG_FILE = BASE / "config.json"
STATE_FILE = BASE / "state.json"
TRIGGER_FILE = BASE / "trigger.flag"
EAST8 = timezone(timedelta(hours=8))

# X 会不定期改 GraphQL operation 名，这里把历史名字都放进来
TIMELINE_OPS = ("UserOriginalsTimeline", "UserRepliesTimeline",
                "UserTweets", "UserTweetsAndReplies")

DEFAULTS = {
    "browser": {"exe": "", "profile_dir": "edge-monitor", "debug_port": 9222},
    "interval_seconds": 90,
    "login_timeout_seconds": 600,
    "include_replies": False,
    "skip_retweets": True,
    "notify_on_first_seen": True,
    "dedup_days": 7,
    "dedup_max_per_user": 500,
    "cache_cleanup_hours": 0,   # 0=关闭；>0 表示每 N 小时清一次 x.com 站点缓存
    "check_updates": True,      # 启动时检查 GitHub 上的新版本
    "users": [],
    "telegram": {"enabled": False, "token": "", "chat_id": "", "proxy": ""},
    "bark": {"enabled": False, "server": "", "device_key": ""},
}


def build_notifier(cfg: dict):
    """优先 Telegram，其次 Bark，都没有就是只写日志。"""
    tg = cfg.get("telegram") or {}
    if tg.get("enabled") and tg.get("token"):
        n = TelegramNotifier(tg["token"], tg.get("chat_id", ""), tg.get("proxy", ""))
        if not n.enabled:
            log.warning("Telegram 已启用但缺少 chat_id，通知不会发送")
        return n
    bark = cfg.get("bark") or {}
    return BarkNotifier(bark.get("server", ""), bark.get("device_key", ""),
                        enabled=bark.get("enabled", False))

log = logging.getLogger("x-monitor")


def deep_merge(base: dict, extra: dict) -> dict:
    out = dict(base)
    for k, v in (extra or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def ensure_config():
    """首次运行时用 config.example.json 生成 config.json。"""
    if CFG_FILE.exists():
        return
    example = BASE / "config.example.json"
    if example.exists():
        shutil.copyfile(example, CFG_FILE)


def load_config() -> dict:
    try:
        raw = json.loads(CFG_FILE.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raw = {}
    return deep_merge(DEFAULTS, raw)


def setup_logging(logfile: Path):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    log.setLevel(logging.INFO)
    fh = logging.handlers.RotatingFileHandler(logfile, maxBytes=2 * 1024 * 1024,
                                              backupCount=3, encoding="utf-8")
    fh.setFormatter(logging.Formatter("%(asctime)s - %(levelname)s - %(message)s"))
    log.handlers[:] = [fh]
    if sys.stdout is not None:
        ch = logging.StreamHandler(sys.stdout)
        ch.setFormatter(logging.Formatter("%(asctime)s - %(levelname)s - %(message)s"))
        log.handlers.append(ch)


def save_json(path: Path, data):
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def _parse_iso(text) -> datetime | None:
    try:
        return datetime.fromisoformat(str(text))
    except (TypeError, ValueError):
        return None


_LOCK_HANDLE = None


def acquire_instance_lock() -> bool:
    """同一时间只允许一个监听进程，避免重复推送。"""
    global _LOCK_HANDLE
    try:
        f = open(BASE / "monitor.lock", "a+")
        if os.name == "nt":
            import msvcrt
            f.seek(0)
            msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        _LOCK_HANDLE = f
        return True
    except OSError:
        return False


def _account_missing(text: str) -> bool:
    markers = ("doesn’t exist", "doesn't exist", "账号不存在", "此账号不存在",
               "Try searching for another")
    return any(m in text for m in markers)


class TimelineFetcher:
    """每个用户复用同一个标签页，避免每轮新开 tab。"""

    def __init__(self, browser: Browser):
        self.browser = browser
        self.pages: dict[str, Page] = {}

    def close(self):
        for page in self.pages.values():
            try:
                page.close()
            except Exception:
                pass
        self.pages.clear()

    def drop(self, screen_name: str):
        page = self.pages.pop(screen_name, None)
        if page:
            try:
                page.close()
            except Exception:
                pass

    def _page(self, screen_name: str) -> Page:
        page = self.pages.get(screen_name)
        if page is None or page.conn is not self.browser.conn:
            page = self.browser.new_page("about:blank")
            page.enable_network()
            self.pages[screen_name] = page
        return page

    def fetch(self, screen_name: str, include_replies: bool, timeout: float = 40.0) -> list[dict]:
        page = self._page(screen_name)
        page.drain()
        url = f"https://x.com/{screen_name}" + ("/with_replies" if include_replies else "")
        if page.current_url().startswith(url.rstrip("/")):
            page.reload()
        else:
            page.navigate(url)

        bodies = page.wait_bodies(TIMELINE_OPS, timeout=min(timeout, 22), stop_after=1)
        if not bodies:
            page_text = (page.eval("document.body.innerText") or "")
            if _account_missing(page_text):
                raise LookupError(f"账号不存在或已注销: @{screen_name}")
            log.warning("%s: 22s 内没抓到时间线，硬刷新重试", screen_name)
            page.drain()
            page.reload(ignore_cache=True)
            bodies = page.wait_bodies(TIMELINE_OPS, timeout=timeout, stop_after=1)
        if not bodies:
            page_text = (page.eval("document.body.innerText") or "")
            if _account_missing(page_text):
                raise LookupError(f"账号不存在或已注销: @{screen_name}")
            raise TimeoutError("没有捕获到时间线响应")

        payload = json.loads(bodies[0])
        if "errors" in payload and not payload.get("data"):
            raise RuntimeError(f"接口返回错误: {str(payload['errors'])[:300]}")
        user = ((payload.get("data") or {}).get("user") or {}).get("result") or {}
        if user.get("__typename") in ("UserUnavailable", "UserWithVisibilityResults"):
            raise LookupError(f"账号不可用: {user.get('__typename')}")
        return parse_timeline(payload)


def format_time(dt: datetime | None) -> str:
    if not dt:
        return "时间未知"
    return dt.astimezone(EAST8).strftime("%Y-%m-%d %H:%M:%S")


def render_tweet(t: dict) -> str:
    text = t["text"]
    if len(text) > 1200:
        text = text[:1200] + "…"
    extra = ""
    if t.get("media"):
        extra = f"\n[图片/视频 × {len(t['media'])}]"
    if t.get("retweeted"):
        extra += f"\n[转推自 @{t.get('retweeted_author') or '?'}]"
    if t.get("is_reply"):
        extra += "\n[回复]"
    return f"{text}{extra}\n\n{format_time(t.get('created_at'))}\n{t['url']}"


class Monitor:
    def __init__(self, cfg: dict, state: dict, notifier: BarkNotifier):
        self.cfg = cfg
        self.state = state
        self.notifier = notifier
        self.browser: Browser | None = None
        self.fetcher: TimelineFetcher | None = None
        self.login_page: Page | None = None
        self.port: int = 0
        self._cleaned = False

    # ---------- 浏览器 ----------
    def ensure(self) -> Browser:
        if self.browser is not None and not self.browser.ping():
            log.warning("CDP 连接已失效，重新连接浏览器（登录态不受影响）")
            self.reset()
        if self.browser is None:
            settings = browser_util.resolve_settings(self.cfg)
            self.browser, self.port = browser_util.ensure_browser(
                settings["port"], settings["profile"], settings["exe"])
            self.fetcher = TimelineFetcher(self.browser)
            if not self._cleaned:
                self._cleaned = True
                self._cleanup_stale_tabs()
        return self.browser

    def _cleanup_stale_tabs(self):
        try:
            stale = [t for t in self.browser.list_pages()
                     if (t.get("url") or "").startswith("https://x.com/")]
        except Exception:
            return
        for t in stale:
            self.browser.close_target(t["targetId"])
        if stale:
            log.info("清理了 %d 个上次残留的 x.com 标签页", len(stale))

    def reset(self):
        if self.browser is not None:
            try:
                self.browser.close()
            except Exception:
                pass
        self.browser = None
        self.fetcher = None
        self.login_page = None

    # ---------- 登录 ----------
    def ensure_login(self) -> bool:
        browser = self.ensure()
        if browser_util.is_logged_in(browser):
            return True

        log.info("监控浏览器未登录，打开 X 登录页，请在浏览器窗口里完成登录 ...")
        self.notify("X 监听器未登录", "已打开登录页，请在监控浏览器窗口登录 x.com")
        if self.login_page is None:
            try:
                self.login_page = browser.new_page("https://x.com/login")
            except Exception as e:  # noqa: BLE001
                log.warning("打开登录页失败，重连浏览器后重试: %s", e)
                self.reset()
                browser = self.ensure()
                try:
                    self.login_page = browser.new_page("https://x.com/login")
                except Exception as e2:  # noqa: BLE001
                    log.error("重连后仍无法打开登录页: %s", e2)
                    return False

        timeout = int(self.cfg.get("login_timeout_seconds", 600))
        deadline = time.time() + timeout
        last_note = 0.0
        while time.time() < deadline:
            time.sleep(3)
            if browser_util.is_logged_in(browser):
                log.info("检测到登录成功，继续监听")
                try:
                    if self.login_page:
                        self.login_page.close()
                except Exception:
                    pass
                self.login_page = None
                return True
            if time.time() - last_note > 60:
                last_note = time.time()
                log.info("仍在等待登录（剩余 %d 秒）...", int(deadline - time.time()))
        log.error("等待登录超时")
        return False

    # ---------- 通知 ----------
    def notify(self, title: str, body: str, url: str | None = None) -> bool:
        ok = True
        if self.notifier.enabled:
            ok = self.notifier.send(title, body, url=url)
        log.info("[通知] %s | %s%s", title, body.replace("\n", " ")[:160],
                 "" if ok else "  (发送失败，下一轮重试)")
        return ok

    # ---------- 去重记录 ----------
    def _notified_map(self) -> dict:
        return self.state.setdefault("notified", {})

    def was_notified(self, screen_name: str, tweet_id: str) -> bool:
        return tweet_id in (self._notified_map().get(screen_name) or {})

    def mark_notified(self, screen_name: str, tweet_ids: list[str]):
        bucket = self._notified_map().setdefault(screen_name, {})
        now = datetime.now(EAST8).isoformat()
        for tid in tweet_ids:
            bucket[tid] = now
        self.prune_notified()

    def unmark_notified(self, screen_name: str, tweet_ids: list[str]):
        bucket = self._notified_map().get(screen_name) or {}
        for tid in tweet_ids:
            bucket.pop(tid, None)

    def prune_notified(self):
        """只保留最近 dedup_days 天、每人最多 dedup_max_per_user 条。"""
        days = float(self.cfg.get("dedup_days", 7))
        cap = int(self.cfg.get("dedup_max_per_user", 500))
        cutoff = datetime.now(EAST8) - timedelta(days=days)
        for name in list(self._notified_map()):
            bucket = self._notified_map()[name]
            keep = {tid: ts for tid, ts in bucket.items()
                    if _parse_iso(ts) is None or _parse_iso(ts) >= cutoff}
            if len(keep) > cap:
                ordered = sorted(keep.items(), key=lambda kv: _parse_iso(kv[1]) or cutoff)
                keep = dict(ordered[-cap:])
            if keep:
                self._notified_map()[name] = keep
            else:
                self._notified_map().pop(name, None)

    def deliver(self, screen_name: str, tweets: list[dict]):
        """发送新推文并写入去重记录；发送失败的条目回滚，下一轮重试。"""
        todo = [t for t in tweets if not self.was_notified(screen_name, t["id"])]
        if not todo:
            log.info("@%s 的 %d 条推文已通知过，跳过重复推送", screen_name, len(tweets))
            return
        # 先落盘再发送：即使发送途中进程被杀，重启后也不会重发
        self.mark_notified(screen_name, [t["id"] for t in todo])
        save_json(STATE_FILE, self.state)

        failed: list[str] = []
        if len(todo) <= 3:
            for t in todo:
                if not self.notify(f"@{screen_name} 发新推文", render_tweet(t), url=t["url"]):
                    failed.append(t["id"])
        else:
            head = "\n\n".join(f"· {t['text'][:80]}" for t in todo[:5])
            if not self.notify(f"@{screen_name} 有 {len(todo)} 条新推文",
                               f"{head}\n\n……共 {len(todo)} 条\nhttps://x.com/{screen_name}",
                               url=f"https://x.com/{screen_name}"):
                failed = [t["id"] for t in todo]
        if failed:
            self.unmark_notified(screen_name, failed)
            save_json(STATE_FILE, self.state)
            log.warning("@%s 有 %d 条推送失败，已回滚去重记录，下一轮重试", screen_name, len(failed))

    # ---------- 单用户 ----------
    def check_user(self, user: dict) -> bool:
        name = user["screen_name"]
        include_replies = bool(user.get("include_replies", self.cfg.get("include_replies", False)))
        skip_retweets = bool(user.get("skip_retweets", self.cfg.get("skip_retweets", True)))
        log.info("检查 @%s%s ...", name, "（含回复）" if include_replies else "")

        assert self.fetcher is not None
        try:
            tweets = self.fetcher.fetch(name, include_replies)
        except LookupError as e:
            log.error("%s", e)
            return False
        except Exception as e:  # noqa: BLE001
            log.error("@%s 抓取失败: %s", name, e)
            return False

        top = newest(tweets)
        if not top:
            log.warning("@%s 时间线里没有解析到推文", name)
            return False

        entry = self.state["users"].get(name) or {}
        last_id = entry.get("last_id")
        if not last_id:
            log.info("@%s 首次记录，最新推文 %s: %s", name, top["id"],
                     top["text"][:50].replace("\n", " "))
            if self.cfg.get("notify_on_first_seen", True):
                self.notify(f"开始监听 @{name}", f"当前最新推文：\n\n{render_tweet(top)}",
                            url=top["url"])
        else:
            candidates = [t for t in tweets
                          if not (skip_retweets and t["retweeted"])
                          and not (not include_replies and t["is_reply"])]
            fresh = sorted((t for t in candidates
                            if (len(t["id"]), t["id"]) > (len(last_id), last_id)),
                           key=lambda t: (len(t["id"]), t["id"]))
            if fresh:
                log.info("@%s 发现 %d 条新推文", name, len(fresh))
                self.deliver(name, fresh)
            else:
                log.info("@%s 没有新推文（最新 %s）", name, top["id"])

        self.state["users"][name] = {"last_id": top["id"],
                                     "updated": datetime.now(EAST8).isoformat()}
        save_json(STATE_FILE, self.state)
        return True

    # ---------- 缓存 ----------
    def maybe_clear_cache(self):
        """定期清掉 x.com 的 Service Worker / CacheStorage，防止 profile 越滚越大。"""
        hours = float(self.cfg.get("cache_cleanup_hours", 24))
        if hours <= 0:
            return
        last = _parse_iso(self.state.get("cache_cleared_at"))
        now = datetime.now(EAST8)
        if last and (now - last).total_seconds() < hours * 3600:
            return
        assert self.browser is not None
        page = None
        created = False
        if self.fetcher and self.fetcher.pages:
            page = next(iter(self.fetcher.pages.values()))
        else:
            page = self.browser.new_page("about:blank")
            created = True
        try:
            for origin in ("https://x.com", "https://abs.twimg.com"):
                page.call("Storage.clearDataForOrigin",
                          {"origin": origin, "storageTypes": "cache_storage,service_workers"})
            self.state["cache_cleared_at"] = now.isoformat()
            save_json(STATE_FILE, self.state)
            log.info("已清理浏览器站点缓存（每 %s 小时一次）", int(hours))
        except Exception as e:  # noqa: BLE001
            log.warning("清理站点缓存失败（不影响监听）: %s", e)
        finally:
            if created:
                try:
                    page.close()
                except Exception:
                    pass

    # ---------- 一轮 ----------
    def round(self, users: list[dict]) -> None:
        self.ensure()
        self.maybe_clear_cache()
        if not self.ensure_login():
            return
        for user in users:
            try:
                self.check_user(user)
            except CDPError as e:
                raise RuntimeError(f"CDP 连接异常: {e}") from e

    # ---------- 配置热更新 ----------
    def apply_config(self, cfg: dict):
        users = cfg.get("users") or []
        if self.fetcher:
            alive = {u["screen_name"] for u in users}
            for name in list(self.fetcher.pages):
                if name not in alive:
                    log.info("用户 @%s 已从名单移除，关闭其标签页", name)
                    self.fetcher.drop(name)
        self.cfg = cfg
        self.notifier = build_notifier(cfg)


def sleep_with_trigger(seconds: float):
    """可被 trigger.flag / Ctrl+C 打断的等待。"""
    deadline = time.time() + seconds
    while time.time() < deadline:
        if TRIGGER_FILE.exists():
            try:
                TRIGGER_FILE.unlink()
            except Exception:
                pass
            log.info("收到「立即检查」指令，跳过等待")
            return
        time.sleep(0.5)


def _check_updates_async():
    try:
        info = updater.check_update()
    except Exception:  # noqa: BLE001
        return
    if not info:
        return
    if info.get("newer"):
        log.warning("发现新版本 v%s（当前 v%s）: %s",
                    info["latest"], info["current"], info["url"])
    else:
        log.info("版本 v%s 已是最新", info["current"])


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--once", action="store_true", help="只跑一轮")
    ap.add_argument("--user", help="只检查指定用户名")
    ap.add_argument("--check", action="store_true", help="只做浏览器/登录自检")
    args = ap.parse_args()

    setup_logging(BASE / "monitor.log")
    if not (args.once or args.check) and not acquire_instance_lock():
        log.error("已有监听实例在运行（monitor.lock 被占用），本进程退出")
        return
    ensure_config()
    cfg = load_config()
    state = json.loads(STATE_FILE.read_text(encoding="utf-8")) if STATE_FILE.exists() else {"users": {}}
    state.setdefault("users", {})
    notifier = build_notifier(cfg)

    if args.check:
        settings = browser_util.resolve_settings(cfg)
        browser, port = browser_util.ensure_browser(settings["port"], settings["profile"],
                                                    settings["exe"])
        print("浏览器:", browser.version)
        print("调试端口:", port)
        print("已登录:", browser_util.is_logged_in(browser))
        browser.close()
        return

    users = cfg["users"]
    if args.user:
        users = [u for u in users if u["screen_name"].lower() == args.user.lower()]
        if not users:
            users = [{"screen_name": args.user}]

    monitor = Monitor(cfg, state, notifier)
    log.info("启动监听 v%s，用户: %s", __version__,
             ", ".join(u["screen_name"] for u in users) or "(空)")
    if cfg.get("check_updates", True):
        threading.Thread(target=_check_updates_async, daemon=True).start()

    cfg_mtime = CFG_FILE.stat().st_mtime if CFG_FILE.exists() else 0
    while True:
        started = time.time()
        try:
            if CFG_FILE.exists():
                mtime = CFG_FILE.stat().st_mtime
                if mtime != cfg_mtime:
                    cfg_mtime = mtime
                    cfg = load_config()
                    monitor.apply_config(cfg)
                    users = cfg["users"]
                    if args.user:
                        users = [u for u in users if u["screen_name"].lower() == args.user.lower()]
                    log.info("配置已更新，当前用户: %s",
                             ", ".join(u["screen_name"] for u in users) or "(空)")
            monitor.round(users)
        except Exception as e:  # noqa: BLE001
            log.exception("本轮异常: %s", e)
            monitor.reset()
        if args.once:
            break
        elapsed = time.time() - started
        sleep_for = max(10, float(cfg["interval_seconds"]) - elapsed)
        log.info("等待 %.0f 秒进入下一轮", sleep_for)
        sleep_with_trigger(sleep_for)


if __name__ == "__main__":
    main()
