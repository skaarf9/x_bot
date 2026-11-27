"""把 X 的 UserTweets / UserTweetsAndReplies GraphQL 响应解析成统一结构。

X 的响应结构改过多次，这里同时兼容：
  * 老结构: data.user.result.timeline_v2.timeline.instructions[].entries[].content.itemContent.tweet_results.result.legacy
  * 新结构: result.core / result.details（created_at_ms、full_text 等）
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any, Iterable


def _unwrap_tweet(result: dict) -> dict | None:
    """处理 TweetWithVisibilityResults 包装。"""
    if not isinstance(result, dict):
        return None
    typename = result.get("__typename") or result.get("__isTweetResult")
    if typename == "TweetWithVisibilityResults" or "tweet" in result and "rest_id" not in result:
        result = result.get("tweet") or {}
    if not isinstance(result, dict) or not result.get("rest_id"):
        return None
    return result


def tweet_to_dict(result: dict) -> dict | None:
    t = _unwrap_tweet(result)
    if not t:
        return None
    legacy = t.get("legacy") or {}
    details = t.get("details") or {}
    core = t.get("core") or {}

    # 作者
    screen_name = None
    user_res = (((core.get("user_results") or {}).get("result")) or {})
    if user_res:
        screen_name = ((user_res.get("core") or {}).get("screen_name")
                       or (user_res.get("legacy") or {}).get("screen_name"))
    if not screen_name:
        screen_name = ((legacy.get("user") or {}).get("screen_name"))

    # 正文：长推文优先 note_tweet
    note = (((t.get("note_tweet") or {}).get("note_tweet_results") or {}).get("result") or {})
    text = (note.get("text") or details.get("full_text") or legacy.get("full_text")
            or legacy.get("text") or "")

    # 时间
    created_at = legacy.get("created_at")
    ts = None
    if details.get("created_at_ms"):
        ts = datetime.fromtimestamp(details["created_at_ms"] / 1000, tz=timezone.utc)
    elif created_at:
        try:
            ts = datetime.strptime(created_at, "%a %b %d %H:%M:%S %z %Y")
        except ValueError:
            ts = None

    # 转推 / 回复
    retweeted = bool(legacy.get("retweeted_status_result") or details.get("retweeted_status_result"))
    if not retweeted and text.startswith("RT @"):
        retweeted = True
    reply_to = (legacy.get("in_reply_to_status_id_str")
                or (legacy.get("in_reply_to_user_id_str") and "?"))
    is_reply = bool(legacy.get("in_reply_to_status_id_str")
                    or legacy.get("in_reply_to_user_id_str")
                    or details.get("in_reply_to_status_id_str"))

    # 媒体
    media = []
    for m in ((legacy.get("entities") or {}).get("media") or []):
        if m.get("media_url_https"):
            media.append(m["media_url_https"])

    # 原推（转推时用于展示）
    rt = legacy.get("retweeted_status_result") or details.get("retweeted_status_result")
    rt_author = None
    if rt:
        inner = rt.get("result") if isinstance(rt.get("result"), dict) else rt
        rt_legacy = inner.get("legacy") or {}
        rt_core = ((inner.get("core") or {}).get("user_results") or {}).get("result") or {}
        rt_author = ((rt_core.get("core") or {}).get("screen_name")
                     or (rt_legacy.get("user") or {}).get("screen_name")
                     or ((inner.get("details") or {}).get("user") or {}).get("screen_name"))

    return {
        "id": str(t["rest_id"]),
        "screen_name": screen_name,
        "text": text,
        "created_at": ts,
        "retweeted": retweeted,
        "retweeted_author": rt_author,
        "is_reply": is_reply,
        "media": media,
        "url": f"https://x.com/{screen_name}/status/{t['rest_id']}" if screen_name else f"https://x.com/i/status/{t['rest_id']}",
        "favorite_count": (legacy.get("favorite_count")
                           if legacy.get("favorite_count") is not None
                           else (t.get("counts") or {}).get("favorite_count")),
    }


def _walk_instructions(instructions: Iterable[dict]) -> Iterable[dict]:
    for inst in instructions or []:
        entries = inst.get("entries") or []
        if inst.get("entry"):
            entries = [inst["entry"]]
        for entry in entries:
            content = entry.get("content") or {}
            etype = content.get("entryType") or content.get("__typename")
            if etype in ("TimelineTimelineItem", "TimelineTimelineModule") or content.get("items"):
                yield entry
            elif content.get("itemContent"):
                yield entry


def _extract_tweets_from_entry(entry: dict) -> Iterable[dict]:
    content = entry.get("content") or {}
    item_contents = []
    if content.get("itemContent"):
        item_contents.append(content["itemContent"])
    for item in content.get("items") or []:
        ic = (item.get("item") or {}).get("itemContent")
        if ic:
            item_contents.append(ic)
    for ic in item_contents:
        if (ic.get("itemType") or "") != "TimelineTweet":
            continue
        tres = ((ic.get("tweet_results") or {}).get("result"))
        if tres:
            d = tweet_to_dict(tres)
            if d:
                yield d


def parse_timeline(payload: dict) -> list[dict]:
    """从 GraphQL 响应中提取所有推文（保持响应中的顺序）。"""
    user = (((payload.get("data") or {}).get("user")) or {})
    result = (user.get("result") or {})
    timeline = (result.get("timeline_v2") or result.get("timeline") or {}).get("timeline") or {}
    instructions = timeline.get("instructions") or []
    tweets = []
    for entry in _walk_instructions(instructions):
        tweets.extend(_extract_tweets_from_entry(entry))
    return tweets


def newest(tweets: list[dict]) -> dict | None:
    """按雪花 ID（字符串长度可变，先比长度再比字典序）取最新。"""
    if not tweets:
        return None
    return max(tweets, key=lambda t: (len(t["id"]), t["id"]))
