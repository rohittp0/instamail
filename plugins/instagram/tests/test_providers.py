import pytest

from instagram.config import (ENV_HUNTER, ENV_META_IG_USER, ENV_META_TOKEN, ENV_SESSIONID, Tier)
from instagram.providers.discovery import IgSearchDiscovery
from instagram.providers.email import IgEmail, WebsiteEmail, _rank_emails
from instagram.providers.enrichment import MetaEnrichment
from instagram.providers.registry import build_chains
from instagram.providers.verify import SyntaxVerifier


def test_available_env_gating():
    meta = MetaEnrichment()
    assert meta.available({}) is False
    assert meta.available({ENV_META_TOKEN: "t"}) is False  # needs both
    assert meta.available({ENV_META_TOKEN: "t", ENV_META_IG_USER: "1"}) is True


async def test_ig_email_reads_public_email():
    assert await IgEmail().find({"public_email": "a@x.com"}) == "a@x.com"
    assert await IgEmail().find({"public_email": None}) is None


def test_website_email_ranking_prefers_role_mailboxes():
    ranked = _rank_emails(["random@x.com", "partnerships@x.com", "PARTNERSHIPS@x.com"])
    assert ranked[0] == "partnerships@x.com"   # role mailbox first, case-insensitive dedup
    assert len(ranked) == 2


async def test_syntax_verifier_labels():
    v = SyntaxVerifier(check_deliverability=False)
    assert await v.verify("good@example.com") == "valid"
    assert await v.verify("not-an-email") == "invalid"


def test_registry_free_only_excludes_official_and_paid():
    chains = build_chains(env={}, max_tier=Tier.PAID, session=object(), use_cache=False)
    assert [p.name for p in chains.discovery] == ["ddg", "ig_topsearch"]
    assert [p.name for p in chains.enrichment] == ["public"]
    assert [p.name for p in chains.email] == ["ig_public", "website"]
    assert [p.name for p in chains.verify] == ["syntax_mx"]


def test_registry_includes_official_when_creds_present_and_tier_sorted():
    env = {ENV_META_TOKEN: "t", ENV_META_IG_USER: "1"}
    chains = build_chains(env=env, max_tier=Tier.PAID, session=object(), use_cache=False)
    names = [p.name for p in chains.discovery]
    assert "hashtag_search" in names
    # FREE providers sort ahead of the OFFICIAL one
    assert names.index("ddg") < names.index("hashtag_search")


async def test_igsearch_sends_sessionid_cookie():
    captured = {}

    class FakeResp:
        status_code = 200

        def json(self):
            return {"users": [{"user": {"username": "creator1"}}]}

    class FakeSession:
        async def get(self, url, params=None, headers=None, cookies=None):
            captured["cookies"] = cookies
            return FakeResp()

    handles = await IgSearchDiscovery(FakeSession(), sessionid="SID123").discover("x", 10)
    assert handles == ["creator1"]
    assert captured["cookies"] == {"sessionid": "SID123"}


async def test_igsearch_falls_back_to_longest_token_for_verbose_phrase():
    seen = []

    class FakeResp:
        status_code = 200

        def __init__(self, usernames):
            self._u = usernames

        def json(self):
            return {"users": [{"user": {"username": u}} for u in self._u]}

    class FakeSession:
        async def get(self, url, params=None, headers=None, cookies=None):
            q = params["query"]
            seen.append(q)
            return FakeResp(["brandacct"] if q == "travelanimator" else [])

    handles = await IgSearchDiscovery(FakeSession(), sessionid="s").discover(
        "animated travel map travelanimator", 10)
    assert handles == ["brandacct"]                       # recovered via longest token
    assert seen == ["animated travel map travelanimator", "travelanimator"]


def test_registry_prefers_topsearch_when_sessionid_present():
    chains = build_chains(env={ENV_SESSIONID: "SID"}, session=object(), use_cache=False)
    assert chains.discovery[0].name == "ig_topsearch"   # session => topsearch first
    chains_no = build_chains(env={}, session=object(), use_cache=False)
    assert chains_no.discovery[0].name == "ddg"          # no session => ddg first


def test_registry_max_tier_free_excludes_official_even_with_creds():
    env = {ENV_META_TOKEN: "t", ENV_META_IG_USER: "1", ENV_HUNTER: "h"}
    chains = build_chains(env=env, max_tier=Tier.FREE, session=object(), use_cache=False)
    assert all(p.tier == Tier.FREE for p in chains.discovery + chains.enrichment + chains.email)
    assert "hunter" not in [p.name for p in chains.email]
