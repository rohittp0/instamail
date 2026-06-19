"""The plugin contract.

A plugin subclasses :class:`BasePlugin`, declares ``name`` / ``key`` / ``fields``, optionally
registers CLI args in :meth:`add_arguments`, and implements :meth:`search`. See ``CONTEXT.md``
for the canonical definitions of these terms.
"""

from __future__ import annotations

from argparse import Namespace


class ContractViolation(Exception):
    """A plugin returned a row whose keys don't match its declared ``key`` + ``fields``.

    Distinct from any exception a plugin raises internally: a contract violation is a plugin
    *bug* (it would recur every run) and is therefore fatal, whereas a runtime error is isolated.
    Raised only by the framework's own validation, so it can never be confused with a plugin's
    own ``ValueError``/``RuntimeError``.
    """


class BasePlugin:
    """Base class every plugin extends.

    Subclasses set the three class attributes and implement :meth:`search`. The attributes fix
    the plugin's CSV columns up front, independent of which rows a given run finds.
    """

    #: Unique identifier — CLI selector, CSV column prefix, and flag prefix. ``[a-z0-9_]+``.
    name: str = ""
    #: The identity column rows are merged on (e.g. ``"email"`` or ``"phone"``).
    key: str = ""
    #: Output column names, excluding ``key``. CSV column for field ``f`` is ``{name}_{f}``.
    fields: list[str] = []

    @classmethod
    def add_arguments(cls, group) -> None:
        """Register this plugin's filter/sort/limit args on ``group`` (an argparse group).

        Write unprefixed flags (e.g. ``--min-followers``); the framework namespaces them with the
        plugin ``name`` on the CLI and hands them back unprefixed in ``opts``. Default: no args.
        """

    @property
    def columns(self) -> list[str]:
        """The plugin's expected row keys: ``key`` followed by ``fields``."""
        return [self.key, *self.fields]

    async def search(self, terms: str, opts: Namespace) -> list[dict]:
        """Search the platform for ``terms`` and return rows.

        Each returned dict's keys must equal ``{key} ∪ set(fields)`` exactly; a field with no
        value must be present as ``None``. The plugin applies its own filtering/sorting/limiting
        from ``opts``, and returns already-normalized ``key`` values (the framework joins on the
        exact string). Return an empty list for a clean no-results run; raising any exception
        marks the whole plugin as failed for this run (logged to stderr, non-fatal).
        """
        raise NotImplementedError
