"""解析器的离线自测（不联网）。"""
import sys

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.path.insert(0, "..")
from x_parser import newest, parse_timeline  # noqa: E402


def item(rest_id, **result):
    r = {"rest_id": rest_id}
    r.update(result)
    return {
        "entryId": f"tweet-{rest_id}",
        "content": {
            "entryType": "TimelineTimelineItem",
            "itemContent": {"itemType": "TimelineTweet", "tweet_results": {"result": r}},
        },
    }


def timeline(*entries):
    tl = {"instructions": [{"type": "TimelineAddEntries", "entries": list(entries)}]}
    return {"data": {"user": {"result": {"timeline_v2": {"timeline": tl}}}}}


legacy_payload = timeline(
    item("111", legacy={"full_text": "hello world",
                        "created_at": "Thu Nov 26 03:43:05 +0000 2026",
                        "user": {"screen_name": "someone"},
                        "entities": {"media": []}}),
    item("222", legacy={"full_text": "RT @orig: retweeted body",
                        "created_at": "Thu Nov 26 04:00:00 +0000 2026",
                        "retweeted_status_result": {"result": {"rest_id": "999",
                                                               "legacy": {"user": {"screen_name": "orig"}}}}}),
)

new_payload = timeline(
    item("333", __typename="Tweet",
         core={"user_results": {"result": {"core": {"screen_name": "newbie"}}}},
         details={"created_at_ms": 1795664585000, "full_text": "new schema tweet"},
         counts={"favorite_count": 7}),
)

for name, payload in (("legacy", legacy_payload), ("new", new_payload)):
    tweets = parse_timeline(payload)
    print(f"{name}: {len(tweets)}")
    for t in tweets:
        print("  ", t["id"], t["screen_name"], "rt=", t["retweeted"], "reply=", t["is_reply"],
              repr(t["text"][:32]), t["created_at"])

top = newest(parse_timeline(legacy_payload))
assert top and top["id"] == "222", top
print("newest ok:", top["id"])
