import asyncio
from dataclasses import dataclass
from typing import Any, AsyncIterator

from instamail.base import AccountNotFound, BasePlugin


class ContractViolation(Exception):
    """A plugin returned a dict whose keys do not match its declared fields. Fatal."""


@dataclass
class FetchResult:
    email: str
    plugin: str
    status: str            # "ok" | "not_found" | "error"
    data: dict[str, Any] | None
    message: str | None


async def _run_one(plugin: BasePlugin, sem: asyncio.Semaphore, email: str) -> FetchResult:
    async with sem:
        try:
            result = await asyncio.wait_for(plugin.fetch(email), timeout=plugin.timeout)
        except AccountNotFound as e:
            return FetchResult(email, plugin.name, "not_found", None, str(e) or "not_found")
        except asyncio.TimeoutError:
            return FetchResult(email, plugin.name, "error", None, f"timeout after {plugin.timeout}s")
        except Exception as e:
            return FetchResult(email, plugin.name, "error", None, f"{type(e).__name__}: {e}")
    if set(result.keys()) != set(plugin.fields):
        extra = sorted(set(result) - set(plugin.fields))
        missing = sorted(set(plugin.fields) - set(result))
        raise ContractViolation(
            f"plugin {plugin.name!r} returned bad keys for {email}: extra={extra} missing={missing}"
        )
    return FetchResult(email, plugin.name, "ok", result, None)


async def iter_results(
    emails: list[str], plugins: list[BasePlugin], lookahead: int = 500
) -> AsyncIterator[tuple[str, dict[str, FetchResult]]]:
    sems = {p.name: asyncio.Semaphore(p.max_concurrency) for p in plugins}
    inflight: dict[str, list[tuple[str, asyncio.Task]]] = {}

    def schedule(email: str) -> None:
        inflight[email] = [
            (p.name, asyncio.create_task(_run_one(p, sems[p.name], email))) for p in plugins
        ]

    n = len(emails)
    nxt = 0
    while nxt < min(lookahead, n):
        schedule(emails[nxt])
        nxt += 1

    try:
        for email in emails:
            row = {name: await task for name, task in inflight.pop(email)}
            yield email, row
            if nxt < n:
                schedule(emails[nxt])
                nxt += 1
    except BaseException:
        for tasks in inflight.values():
            for _, t in tasks:
                t.cancel()
        raise
