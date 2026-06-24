#!/usr/bin/env python3
"""Pre-filter an email list before the email->instagram workflow.

Drops addresses that cannot resolve to a person's Instagram, so we don't spend
OSINT tokens on them:

  * invalid-format  - not a syntactically valid address
  * relay-alias     - masked/anonymized aliases (Apple Hide-My-Email, Firefox
                      Relay, DuckDuckGo, SimpleLogin, addy.io/AnonAddy, ...)
  * disposable      - known throwaway / temp-mail domains
  * no-reply        - automated, non-personal mailboxes (noreply@, postmaster@, ...)
  * duplicate       - same address seen earlier (Gmail dots/+tags canonicalized)

Survivors are normalized (lowercased, validated via email-validator) and written
one per line. A categorized summary of what was dropped is printed to stderr.

Usage:
    .venv/bin/python scripts/clean_emails.py [INPUT] [-o OUTPUT] [--report REPORT]

INPUT defaults to emails.txt; OUTPUT defaults to emails.cleaned.txt.
"""
from __future__ import annotations

import argparse
import sys
from collections import Counter
from pathlib import Path

try:
    from email_validator import EmailNotValidError, validate_email
    _HAVE_EV = True
except Exception:  # pragma: no cover - fallback when dep is absent
    import re

    _HAVE_EV = False
    _EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

# Masked / relay / alias providers — anonymized, cannot map back to a real person.
RELAY_DOMAINS = {
    "privaterelay.appleid.com",                         # Apple Hide My Email
    "relay.firefox.com", "mozmail.com",                 # Firefox Relay
    "duck.com",                                         # DuckDuckGo Email Protection
    "simplelogin.com", "simplelogin.co", "slmail.me",   # SimpleLogin
    "aleeas.com", "8alias.com",
    "anonaddy.com", "anonaddy.me", "addy.io",           # addy.io / AnonAddy
}

# Known disposable / temp-mail domains (representative; extend as needed).
DISPOSABLE_DOMAINS = {
    "mailinator.com", "guerrillamail.com", "guerrillamail.info", "10minutemail.com",
    "tempmail.com", "temp-mail.org", "throwawaymail.com", "yopmail.com",
    "trashmail.com", "getnada.com", "dispostable.com", "maildrop.cc",
    "fakeinbox.com", "sharklasers.com", "grr.la", "spam4.me", "mohmal.com",
    "tempr.email", "emailondeck.com",
}

# Automated / non-personal local-parts that won't map to an individual's IG.
NOREPLY_LOCALPARTS = {
    "noreply", "no-reply", "donotreply", "do-not-reply", "mailer-daemon",
    "postmaster", "bounce", "bounces", "notifications", "notification",
}


def normalize(addr: str) -> str | None:
    """Validate and lowercase an address; return None if it is not a valid email."""
    addr = addr.strip().strip(",;").strip("<>").strip()
    if not addr:
        return None
    if _HAVE_EV:
        try:
            return validate_email(addr, check_deliverability=False).normalized.lower()
        except EmailNotValidError:
            return None
    return addr.lower() if _EMAIL_RE.match(addr) else None


def drop_reason(addr: str) -> str | None:
    """Return a drop category for an (already normalized) address, or None to keep."""
    local, _, domain = addr.partition("@")
    if domain in RELAY_DOMAINS:
        return "relay-alias"
    if domain in DISPOSABLE_DOMAINS:
        return "disposable"
    if local.split("+", 1)[0] in NOREPLY_LOCALPARTS:
        return "no-reply"
    return None


def dedup_key(addr: str) -> str:
    """Canonical identity for dedup; collapses Gmail dots/+tags to one inbox."""
    local, _, domain = addr.partition("@")
    if domain in ("gmail.com", "googlemail.com"):
        local = local.split("+", 1)[0].replace(".", "")
        domain = "gmail.com"
    return f"{local}@{domain}"


def clean(lines):
    """Return (kept, dropped, counts) for an iterable of raw lines."""
    kept: list[str] = []
    dropped: list[tuple[str, str]] = []
    counts: Counter = Counter()
    seen: set[str] = set()

    for line in lines:
        s = line.strip()
        if not s:
            continue
        norm = normalize(s)
        if norm is None:
            dropped.append((s, "invalid-format"))
            counts["invalid-format"] += 1
            continue
        reason = drop_reason(norm)
        if reason:
            dropped.append((norm, reason))
            counts[reason] += 1
            continue
        key = dedup_key(norm)
        if key in seen:
            dropped.append((norm, "duplicate"))
            counts["duplicate"] += 1
            continue
        seen.add(key)
        kept.append(norm)

    return kept, dropped, counts


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="Clean an email list for the email->instagram workflow.")
    p.add_argument("input", nargs="?", default="emails.txt", help="input file (default: emails.txt)")
    p.add_argument("-o", "--output", default="emails.cleaned.txt", help="output file (default: emails.cleaned.txt)")
    p.add_argument("--report", help="optional path to write dropped addresses with reasons (TSV)")
    args = p.parse_args(argv)

    in_path = Path(args.input)
    if not in_path.exists():
        p.error(f"input file not found: {in_path}")

    raw = in_path.read_text(encoding="utf-8", errors="replace").splitlines()
    kept, dropped, counts = clean(raw)

    Path(args.output).write_text("\n".join(kept) + ("\n" if kept else ""), encoding="utf-8")
    if args.report:
        Path(args.report).write_text(
            "".join(f"{reason}\t{addr}\n" for addr, reason in dropped), encoding="utf-8"
        )

    nonempty = sum(1 for line in raw if line.strip())
    print(f"Input addresses (non-empty lines): {nonempty}", file=sys.stderr)
    for reason, n in counts.most_common():
        print(f"  dropped {reason:13} {n}", file=sys.stderr)
    print(f"Kept (unique, resolvable): {len(kept)} -> {args.output}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
