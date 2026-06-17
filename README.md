# InstaMail: Pluggable OSINT Email Enrichment CLI

A lightweight, pluggable Python CLI that enriches a list of email addresses by running concurrent per-platform enrichment plugins and combining results into a single CSV output file.

## What It Does

InstaMail reads email addresses from a file, runs a selected set of **plugins** (one per platform) against each email concurrently, and writes all results to a CSV. Each plugin attempts to resolve an email to account information on a specific platform (e.g., social media, public registries). The framework ships plugin-free — you write and deploy plugins to match your OSINT targets.

**Key features:**
- **Pluggable:** drop `.py` files into `./plugins/` — no reinstall needed
- **Concurrent:** per-plugin rate limiting via configurable `max_concurrency`
- **Resumable:** output CSV is the journal; a rerun skips already-processed emails
- **Email-clean:** validates and deduplicates input using `email-validator`, normalizes to lowercase
- **Fail-fast plugins:** plugin bugs are fatal (contract violation); runtime data misses are not
- **Ordered output:** rows appear in input order despite concurrent processing

## Installation

### Requirements
- Python ≥ 3.14
- `email-validator>=2`

### Install (editable + dev dependencies)

```bash
pip install -e ".[dev]"
```

Or without dev dependencies:
```bash
pip install -e .
```

This installs the `instamail` command and makes the package importable.

## Usage

### Basic Usage

Enrich emails using all discovered plugins:
```bash
instamail -i emails.txt --plugins all -o results.csv
```

### Common Flags

- `-i, --input FILE` — read emails from this file (required); one email per line
- `-o, --output FILE` — write CSV results here (default: `output.csv`)
- `--plugins PLUGIN [PLUGIN ...]` — comma-separated plugin names to run, or `all` for all discovered plugins (default: `all`)
- `--plugins-dir DIR` — directory to scan for plugins (default: `./plugins/`)
- `--list-plugins` — print available plugins and exit
- `-v, --verbose` — log individual fetch successes (by default only failures + summary are logged)

### Examples

```bash
# List all available plugins
instamail --list-plugins

# Run specific plugins
instamail -i emails.txt --plugins instagram,github -o results.csv

# Verbose logging
instamail -i emails.txt --plugins all -v

# Custom plugins directory
instamail -i emails.txt --plugins-dir ./my_plugins/ -o results.csv
```

### Output

The output CSV includes:
- `email` — the normalized (lowercased) email address
- Columns for each plugin's fields, named `{plugin_name}_{field_name}`
- Plugins are ordered alphabetically by name; fields follow their declared order
- Scalar values are written as-is; `None` becomes blank; non-scalars (list/dict) are JSON-encoded

**Example row:**
```
email,github_public_repos,github_followers,instagram_verified,instagram_followers
alice@example.com,42,156,true,8234
bob@example.com,3,,false,
```

### Resuming a Run

If `instamail` is interrupted, rerun with the same input, output file, and plugin selection:
```bash
instamail -i emails.txt --plugins all -o results.csv
```

InstaMail will:
1. Read the existing CSV header
2. Extract already-processed emails from the `email` column
3. Skip them and process only new emails
4. Append new rows in input order

**Important:** the existing CSV header must exactly match the current plugin selection. If you've added/removed plugins or changed fields, you must use a new output file.

## Writing a Plugin

### Plugin Contract

A plugin is a Python class that subclasses `BasePlugin` and lives as a `.py` file in `./plugins/` (or your custom `--plugins-dir`).

**Required attributes:**
- `name: str` — unique identifier for the plugin (e.g., `"instagram"`, `"github"`), used in CSV column names and `--plugins` selection
- `fields: list[str]` — list of output field names (e.g., `["followers", "verified", "bio"]`)
- `max_concurrency: int` (default: 5) — max simultaneous `fetch()` calls for this plugin
- `timeout: float` in seconds (default: 10.0) — hard timeout per email per plugin

**Required method:**
```python
async def fetch(self, email: str) -> dict[str, Any]:
    """Resolve one email to account data.
    
    Returns a dict with keys exactly matching `fields`. A missing field must be None.
    Raises AccountNotFound if the email has no account on this platform.
    Raises any other exception on a runtime failure (network error, timeout, etc).
    """
```

### Plugin Structure

```python
from instamail.base import BasePlugin, AccountNotFound

class MyPlatformPlugin(BasePlugin):
    name = "myplatform"  # used as {name}_{field} in CSV headers
    fields = ["user_id", "username", "followers", "verified"]
    max_concurrency = 10  # up to 10 concurrent fetches
    timeout = 15.0  # 15-second timeout per email
    
    async def fetch(self, email: str) -> dict[str, str | int | bool | None]:
        """Fetch account info for an email from MyPlatform."""
        # Email is normalized (lowercased)
        result = await self.query_myplatform_api(email)
        
        if not result:
            raise AccountNotFound(f"No account for {email}")
        
        return {
            "user_id": result.get("id"),
            "username": result.get("username"),
            "followers": result.get("follower_count", 0),
            "verified": result.get("is_verified", False),
        }
```

### The Exact-Keys Contract

**Your `fetch()` returned dict keys must exactly match `fields`.** This is a contract:
- **No extra keys** — if you return `{"user_id": 1, "username": "alice", "followers": 100, "verified": True, "extra_key": "oops"}`, the run aborts
- **No missing keys** — if you return `{"user_id": 1, "username": "alice"}` (missing `followers` and `verified`), the run aborts
- **Missing data = None** — if the platform didn't return followers, return `"followers": None`, not omit the key

**Why:** a contract violation indicates a plugin bug that would recur on every email, so it's fatal. The run aborts immediately with a message naming the plugin and the offending keys. This is different from data misses (`AccountNotFound`, network errors, timeouts), which are logged but do not abort the run.

### `AccountNotFound` vs Other Exceptions

- **Raise `AccountNotFound()`** when the email genuinely has no account on this platform (e.g., HTTP 404, "user not found" in the API response). This is logged as a "not found" outcome, not counted as an error.
- **Raise any other exception** (or timeout) on a runtime failure: network error, API rate limit, parsing error, etc. These are logged as "error" outcomes and do not abort the run.

### Deploying a Plugin

Drop your `.py` file into `./plugins/`:
```bash
cp my_platform.py ./plugins/
```

Then run:
```bash
instamail --list-plugins
```

Your plugin will appear in the list. Next run:
```bash
instamail -i emails.txt --plugins myplatform -o results.csv
```

No reinstall needed.

## Example: A Fictional "Faceblook" Plugin

Here's a complete worked example for a fictional social platform:

```python
# plugins/faceblook.py

import asyncio
from instamail.base import BasePlugin, AccountNotFound

class FaceblookPlugin(BasePlugin):
    """Example plugin for the fictional Faceblook social network."""
    
    name = "faceblook"
    fields = ["user_id", "display_name", "followers", "verified"]
    max_concurrency = 8
    timeout = 12.0
    
    async def fetch(self, email: str) -> dict:
        """Mock fetch: returns fake data for demonstration.
        
        In a real plugin, this would:
        - Call an HTTP API to look up the email
        - Parse the response
        - Return a dict with field values
        """
        # Simulate network delay
        await asyncio.sleep(0.1)
        
        # Mock data: some emails have accounts, some don't
        accounts = {
            "alice@example.com": {
                "user_id": "fb_12345",
                "display_name": "Alice Wonder",
                "followers": 1523,
                "verified": True,
            },
            "bob@example.com": {
                "user_id": "fb_67890",
                "display_name": "Bob Smith",
                "followers": 342,
                "verified": False,
            },
        }
        
        if email in accounts:
            return accounts[email]
        else:
            # Clean miss: account does not exist
            raise AccountNotFound(f"Email {email} has no Faceblook account")
```

When you run:
```bash
instamail -i emails.txt --plugins faceblook -o results.csv
```

The output CSV will look like:
```
email,faceblook_user_id,faceblook_display_name,faceblook_followers,faceblook_verified
alice@example.com,fb_12345,Alice Wonder,1523,true
bob@example.com,fb_67890,Bob Smith,342,false
charlie@example.com,,,
```

(Charlie has no Faceblook account, so those cells are blank.)

## Included Plugin: Instagram

The bundled `instagram` plugin (the self-contained `plugins/instagram/` package) enriches an
email with a user's Instagram profile: `id`, `followers`, `following`, `posts`, `avg_views`,
`max_views`, plus profile, business-contact, engagement, and content-signal fields (run
`instamail --list-plugins` for the full list).

```bash
instamail -i emails.txt --plugins instagram -o out.csv
```

**How it works.** It first resolves the email to a username (best-effort: DuckDuckGo dork →
username-permutation gated by a profile name match), then harvests the public profile anonymously
via Instagram's `web_profile_info` endpoint (Chrome TLS impersonation via `curl_cffi`). Avg/max
views exist only for video/reel posts; private/photo-only accounts leave those cells blank. Each
row records `resolution_method` and `resolution_confidence` so you can judge a hit's trustworthiness.

> **On breach-data resolution:** a breach-lookup resolver (IntelX) was evaluated and dropped. The
> free tier's selector/phonebook API (which would return `instagram.com/<handle>`) is paid-gated,
> and its search API returns only breach *dataset metadata* (never usernames) — so it could not
> yield Instagram handles. Resolution therefore relies on dorking + permutation.

**Environment variables (optional but recommended):**

| Variable | Effect |
|---|---|
| `INSTAGRAM_SESSIONID` | An `instagram.com` `sessionid` cookie. Sharply reduces 401s and lets the plugin fetch faster (~5s vs ~18s min interval). |

**Expectations.** Email→Instagram resolution is inherently **low-yield** — Instagram severs that
link, so most emails will land in the log as `not_found`. Throughput is intentionally **slow**
(anonymous rate limiting); results are cached under `.cache/instagram/` and the framework's
autoresume makes reruns cheap. Run from a **residential/mobile IP** — datacenter IPs are blocked.

## Concurrency Model

- **Per-plugin concurrency:** each plugin gets its own `asyncio.Semaphore(max_concurrency)`. If a plugin has `max_concurrency=5`, at most 5 of its `fetch()` calls run simultaneously.
- **No global cap:** there is no `--concurrency` flag or global concurrency limit. Concurrency is governed entirely by the plugins, because rate limits are a property of the platform, not the run.
- **Ordered output despite concurrency:** rows are written in input email order via a bounded look-ahead window. A single slow email (near its timeout) holds back rows behind it; this is the intended trade-off for maintaining input order.

## Autoresume and the Output CSV

The output CSV doubles as a resume journal:
- **Per-row flush:** after all selected plugins complete for one email, that row is flushed and fsync'd to disk.
- **"Has a row" = "fully processed":** if an email appears in the output CSV, all its selected plugins have completed.
- **Resume:** on startup, if the output file exists, InstaMail reads the CSV header and the `email` column, skips already-processed emails, and appends new rows.
- **Header must match:** the existing CSV header must exactly equal the expected header for the current plugin selection (alphabetically sorted, `email` first, then `{plugin}_{field}`). A mismatch is fatal — resume continues the *same* run, so a changed plugin set requires a new output file.

See `docs/adr/0001-output-csv-as-resume-journal.md` for the design rationale.

## Email Normalization

Input emails are validated and normalized using `email-validator` (syntax only; DNS/deliverability checks are off):
- **Lowercased:** both local part and domain are converted to lowercase (e.g., `Alice@EXAMPLE.COM` → `alice@example.com`)
- **Deduplicated:** after normalization, only the first occurrence of each email is retained
- **Invalid lines skipped:** syntax errors are logged to stderr and the line is skipped
- **Plugins receive normalized form:** plugins always see fully lowercased emails

## Logging and Diagnostics

- **stderr:** all logs go to stderr (not stdout), so the CSV can be piped cleanly
- **Log levels:** by default, errors and the run summary are logged. Use `-v` to also log successes.
- **Run summary:** at the end, a summary line on stderr shows: `ok: N, not_found: M, error: K` across all email × plugin tasks

## Troubleshooting

**"Contract violation: plugin X returned keys {…} but expected {…}"**
- Your plugin's `fetch()` method is returning the wrong keys. Check that every key in `fields` is present and no extra keys are returned.

**"Header mismatch: existing CSV header differs from expected header"**
- You've changed the plugin selection (added/removed/renamed plugins). Use a new output file, or revert your plugin changes.

**"Duplicate plugin name: X"**
- Two `.py` files in `./plugins/` define plugins with the same `name`. Rename one.

**"Unknown plugin: X"**
- The plugin you named in `--plugins` does not exist. Run `--list-plugins` to see available plugins.

**"Empty plugins directory and --plugins all specified"**
- The `./plugins/` directory (or your custom `--plugins-dir`) has no `.py` files. This is fatal because the run would produce a meaningless empty CSV.

## Reference

- **CONTEXT.md** — canonical glossary of terms (plugin, contract, autoresume, email cleaning)
- **docs/adr/0001-output-csv-as-resume-journal.md** — design decision on why the output CSV is the resume journal
- **CLAUDE.md** — architecture overview and development commands

## License

Unlicensed (example/educational code).
