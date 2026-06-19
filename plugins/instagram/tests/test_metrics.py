from instagram.metrics import compute_metrics


def _user(views, followers=1000):
    edges = []
    for i, v in enumerate(views):
        edges.append({"node": {
            "is_video": v is not None,
            "video_view_count": v,
            "edge_media_preview_like": {"count": 10},
            "edge_media_to_comment": {"count": 2},
            "taken_at_timestamp": 1_700_000_000 + i * 86400,
            "edge_media_to_caption": {"edges": [{"node": {"text": "#travel adventure"}}]},
        }})
    return {
        "edge_owner_to_timeline_media": {"edges": edges, "count": len(edges)},
        "edge_followed_by": {"count": followers},
        "edge_follow": {"count": 50},
    }


def test_avg_and_max_views_over_reels():
    m = compute_metrics(_user([100, 300, 200]))
    assert m["avg_views"] == 200.0
    assert m["max_views"] == 300


def test_window_caps_recent_reels():
    # window=2 uses only the first two posts (100, 300) -> avg 200, max 300
    m = compute_metrics(_user([100, 300, 50, 50]), window=2)
    assert m["avg_views"] == 200.0
    assert m["max_views"] == 300


def test_photos_have_no_views():
    m = compute_metrics(_user([None, None]))  # no videos
    assert m["avg_views"] is None and m["max_views"] is None


def test_top_hashtags_extracted():
    m = compute_metrics(_user([100]))
    assert "travel" in (m["top_hashtags"] or [])
