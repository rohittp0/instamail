from instagram.cache import JsonCache


def test_set_get_roundtrip(tmp_path):
    c = JsonCache(tmp_path, ttl=100, now=lambda: 1000.0)
    c.set("user", {"a": 1})
    assert c.get("user") == {"a": 1}


def test_missing_key_returns_default(tmp_path):
    c = JsonCache(tmp_path, ttl=100, now=lambda: 1000.0)
    assert c.get("nope", default="x") == "x"


def test_expired_entry_returns_default(tmp_path):
    clock = {"t": 1000.0}
    c = JsonCache(tmp_path, ttl=10, now=lambda: clock["t"])
    c.set("k", "v")
    clock["t"] = 1011.0  # 11s later, ttl 10 -> expired
    assert c.get("k", default=None) is None
