#!/usr/bin/env python3
"""Persist a batch of resolved emails to the output sheet — stats + confidence + append.

The JS Workflow's per-batch persist agent makes a single near-zero-token Bash call to this script;
all the work is deterministic Python here, not LLM reasoning. Given the agentic resolve rows on
stdin, it:

  1. fetches public Instagram stats for the resolved handles (instagram_stats),
  2. deterministically upgrades match_confidence -> "high" when the email is corroborated by the
     fetched profile (email/local-part in bio, or a personal-domain link) — zero LLM tokens,
  3. stamps resolved_at,
  4. builds the 25-cell output rows and appends them in one atomic call (sheets_io).

stdin : {"rows": [{"email","username","match_confidence","evidence_url"}, ...]}
stdout: {"appended": N}

Usage:
    echo '{"rows":[...]}' | .venv/bin/python scripts/persist.py
"""

from __future__ import annotations

import asyncio
import json
import sys
from datetime import datetime, timezone

from instagram_stats import _STAT_KEYS, fetch_stats
from sheets_io import OUTPUT_HEADER, append_output, open_spreadsheet

# Free webmail providers — a matching email *domain* in a bio/url is not corroborating (everyone
# has a gmail). Only a *personal* domain link counts toward the confidence upgrade.
FREEMAIL_DOMAINS = {
    "gmail.com", "googlemail.com", "yahoo.com", "ymail.com", "hotmail.com", "outlook.com",
    "live.com", "msn.com", "icloud.com", "me.com", "mac.com", "aol.com", "proton.me",
    "protonmail.com", "gmx.com", "zoho.com", "mail.com", "yandex.com",
}


def _canonical_localpart(email: str) -> str:
    """Local-part lowercased with +tags and dots stripped (for a distinctive-token match)."""
    local = email.split("@", 1)[0].lower()
    return local.split("+", 1)[0].replace(".", "")


def upgrade_confidence(email: str, base_confidence: str, stats: dict) -> str:
    """Return "high" when the fetched profile corroborates the email; else the base confidence.

    Corroboration (any one): the full email appears in the bio/external_url; a distinctive
    local-part (>=4 chars) appears there; or the email's personal (non-freemail) domain appears in
    the external_url. Confidence is only ever raised, never lowered."""
    if base_confidence == "high":
        return "high"
    if not email or "@" not in email:
        return base_confidence
    if stats.get("stats_status") not in ("ok", "private"):
        return base_confidence

    haystack = " ".join(
        str(stats.get(k) or "") for k in ("biography", "external_url", "full_name")
    ).lower()
    if not haystack.strip():
        return base_confidence

    email = email.lower()
    if email in haystack:
        return "high"

    localpart = _canonical_localpart(email)
    if len(localpart) >= 4 and localpart in haystack.replace(".", ""):
        return "high"

    domain = email.split("@", 1)[1]
    url = str(stats.get("external_url") or "").lower()
    if domain not in FREEMAIL_DOMAINS and domain in url:
        return "high"

    return base_confidence


def _cell(stats: dict, key: str):
    val = stats.get(key)
    if key == "top_hashtags" and isinstance(val, list):
        return ", ".join(val)
    return val


def build_rows(resolve_rows: list[dict], stats_map: dict[str, dict], resolved_at: str) -> list[list]:
    """Build output rows (OUTPUT_HEADER order) from resolve rows + a {username: stats} map."""
    rows: list[list] = []
    for r in resolve_rows:
        email = (r.get("email") or "").strip().lower()
        username = (r.get("username") or "").strip().lstrip("@").lower()
        confidence = r.get("match_confidence") or "none"
        evidence = r.get("evidence_url") or ""

        if username:
            stats = stats_map.get(username) or {k: None for k in _STAT_KEYS}
            stats.setdefault("stats_status", "error")
            confidence = upgrade_confidence(email, confidence, stats)
        else:
            stats = {k: None for k in _STAT_KEYS}
            stats["stats_status"] = ""  # dead-end: no handle to fetch

        row_map = {
            "email": email,
            "username": username,
            "match_confidence": confidence,
            "stats_status": stats.get("stats_status"),
            "evidence_url": evidence,
            "resolved_at": resolved_at,
        }
        for k in _STAT_KEYS:
            if k != "username":  # username already taken from the resolve row
                row_map[k] = _cell(stats, k)

        rows.append([row_map.get(col) for col in OUTPUT_HEADER])
    return rows


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def run(resolve_rows: list[dict], *, fetch=fetch_stats, appender=None, now=_now_iso) -> int:
    """Resolve stats, build rows, append. Deps are injectable for tests.

    ``fetch`` is an async {usernames}->{username: stats} callable; ``appender`` is a
    rows->int callable (defaults to a real sheets_io append)."""
    usernames = [
        (r.get("username") or "").strip().lstrip("@").lower()
        for r in resolve_rows
        if (r.get("username") or "").strip()
    ]
    stats_map = asyncio.run(fetch(usernames)) if usernames else {}
    rows = build_rows(resolve_rows, stats_map, now())

    if appender is None:
        spreadsheet = open_spreadsheet()
        return append_output(spreadsheet, rows)
    return appender(rows)


def main(argv=None) -> int:
    try:
        payload = json.load(sys.stdin)
    except Exception as exc:  # noqa: BLE001
        print(f"persist: invalid JSON on stdin: {exc}", file=sys.stderr)
        return 2
    rows = payload.get("rows") if isinstance(payload, dict) else None
    if not isinstance(rows, list):
        print('persist: expected {"rows": [{...}]} on stdin', file=sys.stderr)
        return 2

    appended = run(rows)
    json.dump({"appended": appended}, sys.stdout)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
