import pytest

from instamail.base import AccountNotFound
from instagram.harvester import HarvestError, ProfileNotFound
from instagram.plugin import InstagramPlugin
from instagram.resolver import Resolution


class FakeResolver:
    def __init__(self, resolution):
        self._r = resolution

    async def resolve(self, email):
        return self._r


class FakeHarvester:
    def __init__(self, user=None, exc=None):
        self._user = user
        self._exc = exc

    async def fetch_profile(self, username):
        if self._exc is not None:
            raise self._exc
        return self._user


def _full_user():
    return {
        "id": "42",
        "username": "bob",
        "full_name": "Bob Roberts",
        "biography": "hi",
        "external_url": "https://bob.com",
        "is_verified": True,
        "is_private": False,
        "profile_pic_url": "https://pic",
        "is_business_account": True,
        "business_category_name": "Musician/Band",
        "business_email": "bob@band.com",
        "business_phone_number": "+100",
        "edge_followed_by": {"count": 1000},
        "edge_follow": {"count": 50},
        "edge_owner_to_timeline_media": {
            "count": 2,
            "edges": [
                {"node": {"is_video": True, "video_view_count": 900,
                          "edge_media_preview_like": {"count": 100},
                          "edge_media_to_comment": {"count": 10},
                          "taken_at_timestamp": 1_704_067_200,
                          "edge_media_to_caption": {"edges": [{"node": {"text": "#gig"}}]}}},
                {"node": {"is_video": False,
                          "edge_media_preview_like": {"count": 200},
                          "edge_media_to_comment": {"count": 20},
                          "taken_at_timestamp": 1_704_153_600,
                          "edge_media_to_caption": {"edges": []}}},
            ],
        },
    }


def _plugin(resolution, user=None, exc=None):
    return InstagramPlugin(resolver=FakeResolver(resolution), harvester=FakeHarvester(user, exc))


async def test_row_keys_exactly_match_fields():
    p = _plugin(Resolution("bob", "dork", "medium"), user=_full_user())
    row = await p.fetch("e@x.com")
    assert set(row.keys()) == set(p.fields)  # framework aborts on any key mismatch


async def test_row_values_mapped_correctly():
    p = _plugin(Resolution("bob", "dork", "medium"), user=_full_user())
    row = await p.fetch("e@x.com")
    assert row["id"] == "42"
    assert row["followers"] == 1000
    assert row["following"] == 50
    assert row["posts"] == 2
    assert row["max_views"] == 900
    assert row["public_email"] == "bob@band.com"
    assert row["business_category"] == "Musician/Band"
    assert row["resolution_method"] == "dork"
    assert row["resolution_confidence"] == "medium"
    assert row["username"] == "bob"


async def test_recovery_hints_mapped_from_resolution():
    res = Resolution("bob", "lookup_verified", "high",
                     obfuscated_email="b*****b@x.com", obfuscated_phone="+1 ***-**99")
    p = _plugin(res, user=_full_user())
    row = await p.fetch("e@x.com")
    assert row["recovery_email_hint"] == "b*****b@x.com"
    assert row["recovery_phone_hint"] == "+1 ***-**99"
    assert row["resolution_method"] == "lookup_verified"
    assert row["resolution_confidence"] == "high"


async def test_recovery_hints_blank_when_absent():
    p = _plugin(Resolution("bob", "dork", "medium"), user=_full_user())
    row = await p.fetch("e@x.com")
    assert row["recovery_email_hint"] is None
    assert row["recovery_phone_hint"] is None


async def test_unresolved_email_raises_account_not_found():
    p = _plugin(None)
    with pytest.raises(AccountNotFound):
        await p.fetch("nobody@x.com")


async def test_profile_not_found_raises_account_not_found():
    p = _plugin(Resolution("ghost", "permutation", "low"), exc=ProfileNotFound("ghost"))
    with pytest.raises(AccountNotFound):
        await p.fetch("e@x.com")


async def test_private_account_has_none_view_fields():
    user = _full_user()
    user["is_private"] = True
    user["edge_owner_to_timeline_media"] = {"count": 30, "edges": []}
    p = _plugin(Resolution("bob", "dork", "medium"), user=user)
    row = await p.fetch("e@x.com")
    assert row["is_private"] is True
    assert row["followers"] == 1000   # counts still present
    assert row["avg_views"] is None
    assert row["engagement_rate"] is None


async def test_harvest_error_propagates_not_as_account_not_found():
    p = _plugin(Resolution("bob", "dork", "medium"), exc=HarvestError("blocked"))
    with pytest.raises(HarvestError):
        await p.fetch("e@x.com")
