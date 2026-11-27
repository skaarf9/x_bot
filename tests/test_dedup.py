"""去重逻辑离线自测（不联网、不发消息）。"""
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, "..")
import x_monitor as xm  # noqa: E402
from bark_notify import BarkNotifier  # noqa: E402

EAST8 = timezone(timedelta(hours=8))
tmp = Path("_dedup_state.json")

cfg = xm.deep_merge(xm.DEFAULTS, {"dedup_days": 7, "dedup_max_per_user": 3})


class FakeNotifier:
    enabled = True

    def __init__(self):
        self.sent = []
        self.fail = False

    def send(self, title, body, url=None):
        if self.fail:
            return False
        self.sent.append(url)
        return True


def tweet(i):
    return {"id": str(i), "text": f"tweet {i}", "url": f"https://x.com/t/status/{i}",
            "created_at": datetime.now(EAST8), "media": [], "retweeted": False,
            "is_reply": False, "screen_name": "t"}


def fresh_monitor(state):
    xm.STATE_FILE = tmp
    return xm.Monitor(cfg, state, FakeNotifier())


# --- 1) 同一轮里重复调用只发一次
tmp.write_text(json.dumps({"users": {}}), encoding="utf-8")
state = {"users": {}, "notified": {}}
m = fresh_monitor(state)
m.deliver("t", [tweet(101), tweet(102)])
assert len(m.notifier.sent) == 2, m.notifier.sent
m.deliver("t", [tweet(101), tweet(102)])
assert len(m.notifier.sent) == 2, "重复调用不应再发"

# --- 2) 模拟重启：从磁盘重新加载 state
state2 = json.loads(tmp.read_text(encoding="utf-8"))
m2 = fresh_monitor(state2)
m2.deliver("t", [tweet(101), tweet(102)])
assert len(m2.notifier.sent) == 0, "重启后不应重发"

# --- 3) 新推文照常发
m2.deliver("t", [tweet(103)])
assert len(m2.notifier.sent) == 1 and m2.notifier.sent[0].endswith("/103")

# --- 4) 发送失败要回滚，下一轮重试
m2.notifier.fail = True
m2.deliver("t", [tweet(104)])
assert m2.was_notified("t", "104") is False, "失败后应回滚去重记录"
m2.notifier.fail = False
m2.deliver("t", [tweet(104)])
assert m2.was_notified("t", "104") is True, "重试成功后应记录"

# --- 5) 上限裁剪（dedup_max_per_user=3）
for i in range(201, 210):
    m2.deliver("t", [tweet(i)])
bucket = m2.state["notified"]["t"]
assert len(bucket) <= 3, f"应裁剪到 3 条，实际 {len(bucket)}"

# --- 6) 过期裁剪
m3 = fresh_monitor({"users": {}, "notified": {"t": {
    "1": (datetime.now(EAST8) - timedelta(days=30)).isoformat(),
    "2": datetime.now(EAST8).isoformat(),
}}})
m3.prune_notified()
assert list(m3.state["notified"]["t"].keys()) == ["2"], m3.state["notified"]

tmp.unlink(missing_ok=True)
print("dedup tests: ALL PASSED")
