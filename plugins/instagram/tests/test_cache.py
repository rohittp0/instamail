from instagram.cache import JsonCache


def test_set_get_roundtrip(tmp_path):
    c = JsonCache(tmp_path, ttl=100, now=lambda: 1000.0)
    c.set("key", {"a": 1})
    assert c.get("key") == {"a": 1}


def test_missing_returns_default(tmp_path):
    c = JsonCache(tmp_path, ttl=100, now=lambda: 1000.0)
    assert c.get("absent") is None
    assert c.get("absent", default="x") == "x"


def test_can_cache_none_distinct_from_missing(tmp_path):
    c = JsonCache(tmp_path, ttl=100, now=lambda: 1000.0)
    c.set("neg", None)
    assert c.get("neg", default="MISS") is None  # stored None, not the default


def test_expired_entry_returns_default(tmp_path):
    clock = {"t": 1000.0}
    c = JsonCache(tmp_path, ttl=50, now=lambda: clock["t"])
    c.set("key", "v")
    clock["t"] = 1040.0  # within ttl
    assert c.get("key") == "v"
    clock["t"] = 1100.0  # past ttl
    assert c.get("key") is None


def test_keys_are_namespaced_safely(tmp_path):
    c = JsonCache(tmp_path, ttl=100, now=lambda: 1000.0)
    c.set("a@b.com", 1)
    c.set("../etc/passwd", 2)  # path-traversal-looking key must not escape the dir
    assert c.get("a@b.com") == 1
    assert c.get("../etc/passwd") == 2
