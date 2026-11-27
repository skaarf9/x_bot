"""Minimal, dependency-light Chrome DevTools Protocol client.

Only what this project needs: open a page, navigate, and capture an XHR body.
Uses websocket-client (already installed in the user's venv).
"""
from __future__ import annotations

import json
import queue
import threading
import time

import requests
import websocket


class CDPError(RuntimeError):
    pass


class _Connection:
    def __init__(self, ws_url: str, timeout: float = 30.0):
        self.ws = websocket.create_connection(ws_url, timeout=timeout, max_size=64 * 1024 * 1024,
                                              enable_multithread=True)
        self._next_id = 0
        self._lock = threading.Lock()
        self._pending: dict[int, list] = {}
        self._subscribers = []
        self._closed = False
        threading.Thread(target=self._read_loop, daemon=True).start()

    def _read_loop(self):
        while not self._closed:
            try:
                raw = self.ws.recv()
            except Exception:
                self._closed = True
                break
            if not raw:
                continue
            try:
                msg = json.loads(raw)
            except ValueError:
                continue
            mid = msg.get("id")
            if mid is not None:
                slot = self._pending.pop(mid, None)
                if slot:
                    slot[0] = msg
                    slot[1].set()
                continue
            sid = msg.get("sessionId")
            for cb in list(self._subscribers):
                try:
                    cb(msg.get("method"), msg.get("params") or {}, sid)
                except Exception:
                    pass

    def subscribe(self, cb):
        self._subscribers.append(cb)

    def call(self, method: str, params: dict | None = None, session_id: str | None = None,
             timeout: float = 30.0) -> dict:
        with self._lock:
            self._next_id += 1
            mid = self._next_id
        payload = {"id": mid, "method": method, "params": params or {}}
        if session_id:
            payload["sessionId"] = session_id
        slot = [None, threading.Event()]
        self._pending[mid] = slot
        self.ws.send(json.dumps(payload))
        if not slot[1].wait(timeout):
            self._pending.pop(mid, None)
            raise CDPError(f"{method} timed out after {timeout}s")
        msg = slot[0]
        if "error" in msg:
            raise CDPError(f"{method}: {msg['error']}")
        return msg.get("result", {})

    def close(self):
        self._closed = True
        try:
            self.ws.close()
        except Exception:
            pass


class Page:
    def __init__(self, conn: _Connection, target_id: str, session_id: str):
        self.conn = conn
        self.target_id = target_id
        self.session_id = session_id
        self._queue: queue.Queue = queue.Queue()
        self.conn.subscribe(self._on_event)

    def _on_event(self, method, params, session_id):
        if session_id == self.session_id:
            self._queue.put((method, params))

    def call(self, method: str, params: dict | None = None, timeout: float = 30.0):
        return self.conn.call(method, params, self.session_id, timeout)

    def navigate(self, url: str):
        return self.call("Page.navigate", {"url": url})

    def enable_network(self):
        self.call("Network.enable", {"maxTotalBufferSize": 100 * 1024 * 1024,
                                     "maxResourceBufferSize": 20 * 1024 * 1024})
        self.call("Network.setCacheDisabled", {"cacheDisabled": False})
        try:
            self.call("Network.setBypassServiceWorker", {"bypass": True})
        except CDPError:
            pass
        self.call("Page.enable")

    def drain(self):
        while True:
            try:
                self._queue.get_nowait()
            except queue.Empty:
                return

    def reload(self, ignore_cache: bool = False):
        return self.call("Page.reload", {"ignoreCache": ignore_cache})

    def current_url(self) -> str:
        try:
            return self.eval("location.href") or ""
        except CDPError:
            return ""

    def wait_bodies(self, url_substr, timeout: float = 30.0, stop_after: int = 1) -> list[str]:
        """Wait for responses whose URL contains any of url_substr and return their bodies."""
        needles = (url_substr,) if isinstance(url_substr, str) else tuple(url_substr)
        deadline = time.time() + timeout
        pending: dict[str, str] = {}
        bodies: list[str] = []
        while time.time() < deadline and len(bodies) < stop_after:
            remain = max(0.05, deadline - time.time())
            try:
                method, params = self._queue.get(timeout=min(remain, 1.0))
            except queue.Empty:
                continue
            if method == "Network.responseReceived":
                url = (params.get("response") or {}).get("url", "")
                if any(n in url for n in needles):
                    pending[params["requestId"]] = url
            elif method == "Network.loadingFinished":
                rid = params.get("requestId")
                if rid in pending:
                    pending.pop(rid, None)
                    body = self._try_body(rid)
                    if body:
                        bodies.append(body)
        return bodies

    def _try_body(self, request_id: str) -> str | None:
        for _ in range(6):
            try:
                res = self.call("Network.getResponseBody", {"requestId": request_id}, timeout=10)
                body = res.get("body", "")
                if res.get("base64Encoded"):
                    import base64
                    body = base64.b64decode(body).decode("utf-8", "replace")
                return body
            except CDPError:
                time.sleep(0.25)
        return None

    def eval(self, expression: str, timeout: float = 30.0):
        res = self.call("Runtime.evaluate", {"expression": expression, "returnByValue": True,
                                             "awaitPromise": True}, timeout=timeout)
        return res.get("result", {}).get("value")

    def close(self):
        try:
            self.conn.call("Target.closeTarget", {"targetId": self.target_id}, timeout=10)
        except Exception:
            pass


class Browser:
    def __init__(self, port: int = 9222, host: str = "127.0.0.1"):
        self.base = f"http://{host}:{port}"
        info = requests.get(self.base + "/json/version", timeout=5).json()
        self.version = info.get("Browser", "")
        self.conn = _Connection(info["webSocketDebuggerUrl"])

    def new_page(self, url: str = "about:blank") -> Page:
        res = self.conn.call("Target.createTarget", {"url": url})
        target_id = res["targetId"]
        att = self.conn.call("Target.attachToTarget", {"targetId": target_id, "flatten": True})
        return Page(self.conn, target_id, att["sessionId"])

    def list_pages(self) -> list[dict]:
        infos = self.conn.call("Target.getTargets").get("targetInfos", [])
        return [t for t in infos if t.get("type") == "page"]

    def close_target(self, target_id: str):
        try:
            self.conn.call("Target.closeTarget", {"targetId": target_id}, timeout=10)
        except CDPError:
            pass

    def cookies(self, url: str | None = None) -> list[dict]:
        params = {"urls": [url]} if url else {}
        return self.conn.call("Storage.getCookies", params).get("cookies", [])

    def close(self):
        self.conn.close()
