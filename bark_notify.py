"""Bark 推送（沿用旧项目的 device key/服务器，可在 config.json 里覆盖）。"""
from __future__ import annotations

import logging
from urllib.parse import quote

import requests

log = logging.getLogger(__name__)


class BarkNotifier:
    def __init__(self, server: str, device_key: str, enabled: bool = True,
                 group: str = "x-monitor", level: str = "timeSensitive"):
        self.server = (server or "").rstrip("/")
        self.device_key = device_key or ""
        self.enabled = bool(enabled and self.server and self.device_key)
        self.group = group
        self.level = level

    def send(self, title: str, body: str, url: str | None = None) -> bool:
        if not self.enabled:
            log.info("[notify disabled] %s | %s", title, body[:120])
            return False
        payload = {
            "level": self.level,
            "group": self.group,
            "sound": "alarm",
            "call": 1,
            "isArchive": 1,
        }
        if url:
            payload["url"] = url
        endpoint = f"{self.server}/{quote(self.device_key, safe='')}/{quote(title, safe='')}/{quote(body, safe='')}"
        try:
            session = requests.Session()
            session.trust_env = False  # 别走系统代理（ALL_PROXY 会把这台内网 Bark 服务器带偏）
            r = session.get(endpoint, params=payload, verify=False, timeout=20)
            if r.status_code != 200:
                log.error("Bark 推送失败 %s: %s", r.status_code, r.text[:200])
                return False
            return True
        except Exception as e:  # noqa: BLE001
            log.error("Bark 推送异常: %s", e)
            return False
