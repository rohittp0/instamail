"""Pure functions deriving content metrics from a web_profile_info `user` object.

Side-effect free so they unit-test against fixtures without network. Missing values are
None. Photos carry no view count, so view metrics cover video/reel posts only. The
"top by reel reach" ranking proxy is avg_views / max_views over the embedded recent posts.
"""

import re
from collections import Counter
from datetime import datetime, timezone
from typing import Any

_HASHTAG_RE = re.compile(r"#(\w+)")
_TOP_HASHTAGS = 10


def _post_nodes(user: dict) -> list[dict]:
    edges = user.get("edge_owner_to_timeline_media", {}).get("edges", []) or []
    return [e["node"] for e in edges if isinstance(e, dict) and "node" in e]


def _video_views(node: dict) -> int | None:
    if not node.get("is_video"):
        return None
    views = node.get("video_view_count")
    if views is None:
        views = node.get("video_play_count")
    return views


def _count(node: dict, edge: str) -> int | None:
    val = node.get(edge)
    return val.get("count") if isinstance(val, dict) else None


def _caption(node: dict) -> str:
    edges = node.get("edge_media_to_caption", {}).get("edges", []) or []
    if edges:
        return edges[0].get("node", {}).get("text", "") or ""
    return ""


def _mean(values: list[float]) -> float | None:
    return sum(values) / len(values) if values else None


def compute_metrics(user: dict, window: int | None = None) -> dict[str, Any]:
    """Return derived metrics from the embedded recent posts of a web_profile_info user.

    ``window`` caps how many of the most-recent posts feed the view metrics (the ranking
    proxy); ``None`` uses all embedded posts (~12)."""
    posts = _post_nodes(user)
    followers = (user.get("edge_followed_by") or {}).get("count") or 0

    view_posts = posts[:window] if window else posts
    views = [v for v in (_video_views(n) for n in view_posts) if v is not None]
    likes = [c for c in (_count(n, "edge_media_preview_like") for n in posts) if c is not None]
    comments = [c for c in (_count(n, "edge_media_to_comment") for n in posts) if c is not None]
    timestamps = [n["taken_at_timestamp"] for n in posts if n.get("taken_at_timestamp")]
    num_videos = sum(1 for n in posts if n.get("is_video"))

    avg_likes = _mean(likes)
    avg_comments = _mean(comments)

    engagement_rate = None
    if followers and avg_likes is not None and avg_comments is not None:
        engagement_rate = round((avg_likes + avg_comments) / followers * 100, 3)

    cadence = None
    if len(timestamps) >= 2:
        span_days = (max(timestamps) - min(timestamps)) / 86400
        if span_days > 0:
            cadence = round(len(timestamps) / (span_days / 7), 2)

    last_post_date = None
    if timestamps:
        last_post_date = datetime.fromtimestamp(max(timestamps), tz=timezone.utc).date().isoformat()

    hashtags: Counter = Counter()
    for n in posts:
        for tag in _HASHTAG_RE.findall(_caption(n)):
            hashtags[tag.lower()] += 1
    top_hashtags = [t for t, _ in hashtags.most_common(_TOP_HASHTAGS)] or None

    return {
        "avg_views": round(_mean(views), 1) if views else None,
        "max_views": max(views) if views else None,
        "avg_likes": round(avg_likes) if avg_likes is not None else None,
        "avg_comments": round(avg_comments) if avg_comments is not None else None,
        "engagement_rate": engagement_rate,
        "posts_analyzed": len(posts),
        "reels_ratio": round(num_videos / len(posts), 3) if posts else None,
        "posting_cadence_per_week": cadence,
        "last_post_date": last_post_date,
        "top_hashtags": top_hashtags,
    }
