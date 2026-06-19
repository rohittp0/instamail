from argparse import Namespace

from instagram.config import Tier
from instagram.pipeline import ROW_FIELDS, Pipeline
from instagram.providers.registry import Chains


# ---- mock providers (track calls; no network) -------------------------------

class FakeDiscovery:
    def __init__(self, name, handles, tier=Tier.FREE):
        self.name, self._handles, self.tier, self.calls = name, handles, tier, 0

    async def discover(self, terms, limit):
        self.calls += 1
        return list(self._handles)[:limit]


class FakeEnrich:
    def __init__(self, name, mapping, tier=Tier.FREE):
        self.name, self._m, self.tier, self.calls = name, mapping, tier, []

    async def enrich(self, username):
        self.calls.append(username)
        return dict(self._m[username]) if username in self._m else None


class FakeEmail:
    def __init__(self, name, fn, tier=Tier.FREE):
        self.name, self._fn, self.tier, self.calls = name, fn, tier, 0

    async def find(self, profile):
        self.calls += 1
        return self._fn(profile)


class FakeVerifier:
    name, tier = "fake", Tier.FREE

    async def verify(self, email):
        return "valid"


def opts(**kw):
    base = dict(limit=100, min_followers=None, sort="avg_views",
                window=12, travel_only=False, require_email=False)
    base.update(kw)
    return Namespace(**base)


def profile(iid, username, **kw):
    p = {"instagram_id": iid, "username": username, "followers": 1000,
         "avg_views": 100, "max_views": 200, "public_email": None, "external_url": None}
    p.update(kw)
    return p


def chains(discovery, enrichment, email, verify=None):
    return Chains(discovery, enrichment, email, verify if verify is not None else [FakeVerifier()])


# ---- tests ------------------------------------------------------------------

async def test_discovery_takes_first_nonempty_paid_not_called():
    free = FakeDiscovery("ddg", ["alice"])
    paid = FakeDiscovery("vendor", ["bob"], tier=Tier.PAID)
    enrich = FakeEnrich("public", {"alice": profile("1", "alice")})
    c = chains([free, paid], [enrich], [FakeEmail("ig", lambda p: None)])
    rows = await Pipeline(c).run("travel", opts())
    assert paid.calls == 0                       # free succeeded -> paid never ran
    assert [r["username"] for r in rows] == ["alice"]
    assert rows[0]["discovery_source"] == "ddg"


async def test_enrichment_free_before_paid():
    disc = FakeDiscovery("ddg", ["alice"])
    free = FakeEnrich("public", {"alice": profile("1", "alice")})
    paid = FakeEnrich("vendor", {"alice": profile("1", "alice")}, tier=Tier.PAID)
    c = chains([disc], [free, paid], [FakeEmail("ig", lambda p: None)])
    await Pipeline(c).run("x", opts())
    assert free.calls == ["alice"] and paid.calls == []   # free hit -> paid skipped


async def test_email_provenance_ladder():
    disc = FakeDiscovery("ddg", ["alice"])
    enrich = FakeEnrich("public", {"alice": profile("1", "alice", external_url="http://a.co")})
    ig = FakeEmail("ig_public", lambda p: None)            # no published email
    site = FakeEmail("website", lambda p: "found@a.co")    # website yields one
    hunter = FakeEmail("hunter", lambda p: "paid@a.co", tier=Tier.PAID)
    c = chains([disc], [enrich], [ig, site, hunter])
    rows = await Pipeline(c).run("x", opts())
    assert rows[0]["email"] == "found@a.co"
    assert rows[0]["email_source"] == "website"
    assert rows[0]["email_confidence"] == "valid"
    assert hunter.calls == 0                                # free website hit -> paid skipped


async def test_ranking_and_limit():
    disc = FakeDiscovery("ddg", ["a", "b", "c"])
    enrich = FakeEnrich("public", {
        "a": profile("1", "a", avg_views=50),
        "b": profile("2", "b", avg_views=300),
        "c": profile("3", "c", avg_views=150),
    })
    c = chains([disc], [enrich], [FakeEmail("ig", lambda p: None)])
    rows = await Pipeline(c).run("x", opts(sort="avg_views", limit=2))
    assert [r["username"] for r in rows] == ["b", "c"]      # top-2 by avg_views


async def test_min_followers_filter():
    disc = FakeDiscovery("ddg", ["a", "b"])
    enrich = FakeEnrich("public", {
        "a": profile("1", "a", followers=100),
        "b": profile("2", "b", followers=5000),
    })
    c = chains([disc], [enrich], [FakeEmail("ig", lambda p: None)])
    rows = await Pipeline(c).run("x", opts(min_followers=1000))
    assert [r["username"] for r in rows] == ["b"]


async def test_dedup_by_instagram_id():
    disc = FakeDiscovery("ddg", ["a", "a_alias"])
    enrich = FakeEnrich("public", {
        "a": profile("1", "a"),
        "a_alias": profile("1", "a_alias"),   # same id -> deduped
    })
    c = chains([disc], [enrich], [FakeEmail("ig", lambda p: None)])
    rows = await Pipeline(c).run("x", opts())
    assert len(rows) == 1


async def test_email_as_key_none_when_unresolved():
    disc = FakeDiscovery("ddg", ["a"])
    enrich = FakeEnrich("public", {"a": profile("1", "a")})
    c = chains([disc], [enrich], [FakeEmail("ig", lambda p: None)])
    rows = await Pipeline(c).run("x", opts())
    assert rows[0]["email"] is None
    assert rows[0]["email_source"] is None
    assert rows[0]["email_confidence"] is None


async def test_require_email_drops_unresolved():
    disc = FakeDiscovery("ddg", ["a", "b"])
    enrich = FakeEnrich("public", {
        "a": profile("1", "a", public_email="a@x.com"),
        "b": profile("2", "b"),
    })
    c = chains([disc], [enrich], [FakeEmail("ig", lambda p: p.get("public_email"))])
    rows = await Pipeline(c).run("x", opts(require_email=True))
    assert [r["username"] for r in rows] == ["a"]
    assert rows[0]["email"] == "a@x.com"


async def test_rows_have_exact_contract_keys_even_on_sparse_profile():
    disc = FakeDiscovery("ddg", ["a"])
    enrich = FakeEnrich("public", {"a": {"instagram_id": "1", "username": "a"}})  # sparse
    c = chains([disc], [enrich], [FakeEmail("ig", lambda p: None)])
    rows = await Pipeline(c).run("x", opts())
    assert set(rows[0]) == {"email", *ROW_FIELDS}            # exact key set
    assert rows[0]["followers"] is None                      # missing -> None


async def test_email_lowercased():
    disc = FakeDiscovery("ddg", ["a"])
    enrich = FakeEnrich("public", {"a": profile("1", "a")})
    c = chains([disc], [enrich], [FakeEmail("ig", lambda p: "MixedCase@X.COM")])
    rows = await Pipeline(c).run("x", opts())
    assert rows[0]["email"] == "mixedcase@x.com"
