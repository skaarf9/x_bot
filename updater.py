"""检查 GitHub 上的新版本。

- 优先读 Release（/releases/latest），没有 Release 就退回读 tag（/tags）。
- 不依赖 .git，打包成 zip 也能用。
- 结果缓存 6 小时，网络失败时静默返回 None，不影响主程序。
"""
from __future__ import annotations

import json
import logging
import re
import time
from pathlib import Path

import requests

from version import REPO, __version__

log = logging.getLogger(__name__)
CACHE_FILE = Path(__file__).resolve().parent / "update_cache.json"
API = "https://api.github.com/repos/" + REPO
MAX_AGE = 6 * 3600
TIMEOUT = 8


def parse_version(text: str) -> tuple[int, ...]:
    """'v1.2.3' / 'release-1.2' / '1.2.3-beta' -> (1,2,3)。"""
    m = re.search(r"(\d+(?:\.\d+)*)", text or "")
    if not m:
        return (0,)
    return tuple(int(x) for x in m.group(1).split("."))


def is_newer(remote: str, local: str = __version__) -> bool:
    return parse_version(remote) > parse_version(local)


def _get(url: str):
    try:
        r = requests.get(url, timeout=TIMEOUT,
                         headers={"Accept": "application/vnd.github+json"})
    except requests.RequestException:
        return None
    if r.status_code != 200:
        return None
    try:
        return r.json()
    except ValueError:
        return None


def _raw_version() -> tuple[str | None, str]:
    """兜底：直接读仓库 main 上的 version.py（不走 API，不受 60 次/小时限流）。"""
    url = f"https://raw.githubusercontent.com/{REPO}/main/version.py"
    try:
        r = requests.get(url, timeout=TIMEOUT)
    except requests.RequestException:
        return None, ""
    if r.status_code != 200:
        return None, ""
    for line in r.text.splitlines():
        if line.startswith("__version__"):
            value = line.split("=", 1)[-1]
            value = "".join(ch for ch in value if ch.isalnum() or ch in ".-_")
            if value:
                return value, f"https://github.com/{REPO}"
    return None, ""


def latest_remote() -> tuple[str, str] | tuple[None, str]:
    """返回 (版本/标签名, 链接)。Release → tag → raw version.py 逐级兜底。"""
    release = _get(API + "/releases/latest")
    if isinstance(release, dict) and release.get("tag_name"):
        return release["tag_name"], release.get("html_url") or f"https://github.com/{REPO}/releases"

    tags = _get(API + "/tags?per_page=30")
    if isinstance(tags, list) and tags:
        names = [t.get("name", "") for t in tags if t.get("name")]
        if names:
            best = max(names, key=parse_version)
            return best, f"https://github.com/{REPO}/releases/tag/{best}"

    # GitHub API 对匿名请求限流很严（共享 IP 很容易 403），这里退回读源码里的版本号
    return _raw_version()


def check_update(force: bool = False) -> dict | None:
    """返回 {'current', 'latest', 'url', 'newer'}；检查失败返回 None。"""
    now = time.time()
    if not force:
        try:
            cached = json.loads(CACHE_FILE.read_text(encoding="utf-8"))
            if now - float(cached.get("checked_at", 0)) < MAX_AGE:
                return cached.get("result")
        except Exception:
            pass

    tag, url = latest_remote()
    if not tag:
        return None
    result = {
        "current": __version__,
        "latest": tag.lstrip("vV"),
        "url": url,
        "newer": is_newer(tag),
    }
    try:
        CACHE_FILE.write_text(json.dumps({"checked_at": now, "result": result},
                                         ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:
        pass
    return result
