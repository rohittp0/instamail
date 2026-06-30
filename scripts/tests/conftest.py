"""Shared fixtures for the scripts test suite (network-free)."""

import pytest


@pytest.fixture
def public_user():
    """A minimal but realistic web_profile_info `user` object: 2 videos + 1 photo."""
    return {
        "id": "123",
        "username": "natgeo",
        "full_name": "National Geographic",
        "is_verified": True,
        "is_business_account": True,
        "is_private": False,
        "external_url": "http://example.com/",
        "biography": "Adventures #travel #nature contact jane@example.com",
        "edge_followed_by": {"count": 1000},
        "edge_follow": {"count": 50},
        "edge_owner_to_timeline_media": {
            "count": 500,
            "edges": [
                {"node": {
                    "is_video": True, "video_view_count": 100, "taken_at_timestamp": 1_000_000,
                    "edge_media_preview_like": {"count": 10},
                    "edge_media_to_comment": {"count": 2},
                    "edge_media_to_caption": {"edges": [{"node": {"text": "hello #travel #nature"}}]},
                }},
                {"node": {
                    "is_video": True, "video_view_count": 200, "taken_at_timestamp": 1_086_400,
                    "edge_media_preview_like": {"count": 20},
                    "edge_media_to_comment": {"count": 4},
                    "edge_media_to_caption": {"edges": [{"node": {"text": "world #travel"}}]},
                }},
                {"node": {
                    "is_video": False, "taken_at_timestamp": 1_172_800,
                    "edge_media_preview_like": {"count": 30},
                    "edge_media_to_comment": {"count": 6},
                    "edge_media_to_caption": {"edges": []},
                }},
            ],
        },
    }
