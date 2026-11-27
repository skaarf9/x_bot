"""Telegram Bot 推送。

只需要 bot token + chat_id，不需要任何审核/申请。
Bot 不能在用户没说话前主动私聊，所以创建后要先给 bot 发一次 /start。
chat_id 支持逗号/分号分隔多个（私聊 + 群都能收）。
"""
from __future__ import annotations

import html
import logging
import time

import requests

log = logging.getLogger(__name__)

API = "https://api.telegram.org/bot{token}/{method}"
MAX_TEXT = 3900


class TelegramError(RuntimeError):
    pass


def _chat_of(update: dict) -> dict | None:
    """从 update 里取出 chat 对象（消息、编辑、频道帖、成员变动都算）。"""
    for key in ("message", "edited_message", "channel_post", "my_chat_member", "chat_member"):
        obj = update.get(key)
        if isinstance(obj, dict) and obj.get("chat"):
            return obj["chat"]
    return None


class TelegramNotifier:
    """和 BarkNotifier 同样的接口：.enabled / .send(title, body, url=...)."""

    name = "telegram"

    def __init__(self, token: str, chat_id: str = "", proxy: str = "", timeout: int = 20):
        self.token = (token or "").strip()
        self.chat_id = str(chat_id or "").strip()
        self.proxy = (proxy or "").strip()
        self.timeout = timeout
        self.enabled = bool(self.token and self.chat_id)

    # ---------- 底层 ----------
    def _session(self) -> requests.Session:
        s = requests.Session()
        if self.proxy:
            s.trust_env = False
            s.proxies = {"http": self.proxy, "https": self.proxy}
        return s

    def api(self, method: str, **params):
        url = API.format(token=self.token, method=method)
        last = None
        for attempt in range(2):
            try:
                r = self._session().post(url, json=params, timeout=self.timeout)
            except requests.RequestException as e:
                raise TelegramError(f"连不上 Telegram（检查代理）：{e}") from e
            try:
                data = r.json()
            except ValueError:
                raise TelegramError(f"Telegram 返回非 JSON：{r.status_code} {r.text[:200]}")
            if data.get("ok"):
                return data.get("result")
            code = data.get("error_code")
            desc = data.get("description", "")
            if code == 429 and attempt == 0:
                wait = min(30, int((data.get("parameters") or {}).get("retry_after", 3)))
                log.warning("Telegram 限流，%s 秒后重试", wait)
                time.sleep(wait)
                continue
            if code == 401:
                raise TelegramError("bot token 无效（401 Unauthorized）")
            if code == 400 and "chat not found" in desc.lower():
                raise TelegramError("chat_id 不对，或你还没给 bot 发过 /start")
            if code == 400 and "not enough rights" in desc.lower():
                raise TelegramError("bot 在该群没有发言权限（把它设为管理员，或允许成员发言）")
            if code == 403 and "bot was kicked" in desc.lower():
                raise TelegramError("bot 已被移出该群")
            if code == 409:
                raise TelegramError("这个 bot 设置了 webhook，无法用 getUpdates；先删除 webhook")
            last = TelegramError(f"Telegram 错误 {code}: {desc}")
            break
        raise last or TelegramError("未知错误")

    # ---------- 对外 ----------
    def chat_ids(self) -> list[str]:
        raw = self.chat_id.replace(";", ",").replace("，", ",")
        return [x.strip() for x in raw.split(",") if x.strip()]

    def send(self, title: str, body: str, url: str | None = None) -> bool:
        if not self.enabled:
            log.info("[telegram disabled] %s | %s", title, body[:120])
            return False
        text = f"<b>{html.escape(title)}</b>\n\n{html.escape(body)}"
        if len(text) > MAX_TEXT:
            text = text[:MAX_TEXT] + "…"
        ok = False
        for cid in self.chat_ids():
            try:
                self.api("sendMessage", chat_id=cid, text=text,
                         parse_mode="HTML", disable_notification=False)
                ok = True
            except TelegramError as e:
                log.error("Telegram 推送到 %s 失败: %s", cid, e)
        return ok

    def get_me(self) -> dict:
        return self.api("getMe")

    def find_chat_id(self) -> tuple[str, str]:
        """从最近的 update 里取 chat_id：私聊、群、频道都可以。

        群聊需要先在群里发一条 /start@你的bot（普通消息会被隐私模式过滤）。
        """
        updates = self.api("getUpdates", limit=100, timeout=0)
        for u in reversed(updates or []):
            chat = _chat_of(u)
            if not chat or not chat.get("id"):
                continue
            title = (chat.get("title") or chat.get("username")
                     or chat.get("first_name") or "")
            return str(chat["id"]), title
        raise TelegramError("没找到会话：先给 bot 发一条 /start；群聊里发 /start@你的bot")

    def test(self) -> str:
        me = self.get_me()
        name = me.get("username") or me.get("first_name") or "bot"
        for cid in self.chat_ids():
            self.api("sendMessage", chat_id=cid,
                     text=f"✅ <b>X 监听测试</b>\n\n来自 @{html.escape(name)} 的推送通道验证。",
                     parse_mode="HTML")
        return name
