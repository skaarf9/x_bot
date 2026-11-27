"""版本检查离线自测（不联网）。"""
import sys

sys.path.insert(0, "..")
import updater  # noqa: E402


def test_parse():
    assert updater.parse_version("v1.2.3") == (1, 2, 3)
    assert updater.parse_version("1.2") == (1, 2)
    assert updater.parse_version("release-2.0.0-beta") == (2, 0, 0)
    assert updater.parse_version("") == (0,)


def test_newer():
    assert updater.is_newer("v1.1.0", "1.0.0")
    assert updater.is_newer("v2", "1.9.9")
    assert not updater.is_newer("v1.0.0", "1.0.0")
    assert not updater.is_newer("v0.9.0", "1.0.0")


def test_prefers_release():
    def fake(url):
        if url.endswith("/releases/latest"):
            return {"tag_name": "v1.3.0", "html_url": "https://example/rel"}
        return None

    updater._get = fake
    tag, link = updater.latest_remote()
    assert tag == "v1.3.0", tag
    assert link == "https://example/rel"


def test_falls_back_to_tags():
    def fake(url):
        if "/releases/" in url:
            return None
        if "/tags" in url:
            return [{"name": "v1.0.0"}, {"name": "v1.2.0"}, {"name": "v0.9.0"}]
        return None

    updater._get = fake
    tag, _ = updater.latest_remote()
    assert tag == "v1.2.0", tag


if __name__ == "__main__":
    test_parse()
    test_newer()
    test_prefers_release()
    test_falls_back_to_tags()
    print("updater tests: ALL PASSED")
