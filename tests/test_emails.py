from instamail.emails import clean_emails


def test_trims_and_skips_blanks():
    assert clean_emails(["  a@x.com  ", "", "   ", "b@x.com"]) == ["a@x.com", "b@x.com"]


def test_dedupes_case_insensitively_keeping_first_seen():
    assert clean_emails(["Alice@X.com", "alice@x.com", "c@x.com"]) == ["alice@x.com", "c@x.com"]


def test_normalizes_domain_case():
    # email-validator lowercases the domain
    assert clean_emails(["user@EXAMPLE.COM"]) == ["user@example.com"]


def test_skips_invalid_lines(caplog):
    out = clean_emails(["good@x.com", "not-an-email", "also bad@@x"])
    assert out == ["good@x.com"]
    assert "not-an-email" in caplog.text
