"""Telegram 模块离线自测（不联网）。"""
import sys

sys.path.insert(0, "..")
from telegram_notify import TelegramError, TelegramNotifier  # noqa: E402


class Fake(TelegramNotifier):
    def __init__(self, updates):
        super().__init__("token", "", "")
        self._updates = updates

    def api(self, method, **params):
        assert method == "getUpdates", method
        return self._updates


PRIVATE = {"update_id": 1, "message": {
    "chat": {"id": 8085000, "type": "private", "username": "me"}, "text": "/start"}}
GROUP_MSG = {"update_id": 2, "message": {
    "chat": {"id": -1001234567890, "type": "supergroup", "title": "我的群"},
    "text": "/start@my_bot"}}
MY_MEMBER = {"update_id": 3, "my_chat_member": {
    "chat": {"id": -1009999999999, "type": "group", "title": "另一个群"}, "new_chat_member": {}}}


def test_prefers_latest_chat():
    cid, title = Fake([PRIVATE, GROUP_MSG]).find_chat_id()
    assert cid == "-1001234567890", cid
    assert title == "我的群", title


def test_private_only():
    cid, title = Fake([PRIVATE]).find_chat_id()
    assert cid == "8085000", cid


def test_member_update_only():
    cid, title = Fake([MY_MEMBER]).find_chat_id()
    assert cid == "-1009999999999", cid
    assert title == "另一个群", title


def test_nothing_found():
    try:
        Fake([]).find_chat_id()
    except TelegramError as e:
        assert "没找到" in str(e)
    else:
        raise AssertionError("should raise")


if __name__ == "__main__":
    test_prefers_latest_chat()
    test_private_only()
    test_member_update_only()
    test_nothing_found()
    print("telegram tests: ALL PASSED")
