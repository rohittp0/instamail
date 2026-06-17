from instagram.metrics import compute_metrics


def _post(*, is_video, views=None, likes=0, comments=0, ts=0, caption=""):
    node = {
        "is_video": is_video,
        "edge_media_preview_like": {"count": likes},
        "edge_media_to_comment": {"count": comments},
        "taken_at_timestamp": ts,
        "edge_media_to_caption": {"edges": [{"node": {"text": caption}}] if caption else []},
    }
    if is_video:
        node["video_view_count"] = views
    return {"node": node}


def _user(followers, posts):
    return {
        "edge_followed_by": {"count": followers},
        "edge_owner_to_timeline_media": {"edges": posts},
    }


def test_views_over_videos_only():
    user = _user(1000, [
        _post(is_video=True, views=500, likes=100, comments=10, ts=1_700_000_000),
        _post(is_video=True, views=1500, likes=200, comments=20, ts=1_700_600_000),
        _post(is_video=False, likes=50, comments=5, ts=1_700_700_000),  # photo, no views
    ])
    m = compute_metrics(user)
    assert m["avg_views"] == 1000.0          # (500+1500)/2, photo excluded
    assert m["max_views"] == 1500
    assert m["posts_analyzed"] == 3
    assert m["reels_ratio"] == round(2 / 3, 3)


def test_engagement_rate_and_averages():
    user = _user(1000, [
        _post(is_video=True, views=500, likes=100, comments=10, ts=1_700_000_000),
        _post(is_video=False, likes=300, comments=30, ts=1_700_600_000),
    ])
    m = compute_metrics(user)
    assert m["avg_likes"] == 200            # (100+300)/2
    assert m["avg_comments"] == 20          # (10+30)/2
    # (200+20)/1000*100 = 22.0
    assert m["engagement_rate"] == 22.0


def test_hashtags_and_last_post_date():
    user = _user(10, [
        _post(is_video=False, likes=1, comments=1, ts=1_704_067_200, caption="Trip to #Paris #paris #Eiffel"),
        _post(is_video=False, likes=1, comments=1, ts=1_704_153_600, caption="no tags here"),
    ])
    m = compute_metrics(user)
    assert m["top_hashtags"][0] == "paris"   # case-folded, most frequent first
    assert "eiffel" in m["top_hashtags"]
    assert m["last_post_date"] == "2024-01-02"  # max ts (1_704_153_600) = 2024-01-02 00:00 UTC


def test_photo_only_account_has_none_view_metrics():
    user = _user(500, [_post(is_video=False, likes=10, comments=2, ts=1_700_000_000)])
    m = compute_metrics(user)
    assert m["avg_views"] is None
    assert m["max_views"] is None
    assert m["reels_ratio"] == 0.0


def test_empty_profile_is_all_none():
    m = compute_metrics(_user(0, []))
    assert m["avg_views"] is None
    assert m["max_views"] is None
    assert m["avg_likes"] is None
    assert m["engagement_rate"] is None
    assert m["posts_analyzed"] == 0
    assert m["reels_ratio"] is None
    assert m["posting_cadence_per_week"] is None
    assert m["last_post_date"] is None
    assert m["top_hashtags"] is None
