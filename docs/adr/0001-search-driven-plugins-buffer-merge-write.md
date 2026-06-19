# Search-driven plugins; buffer, merge by key, write once

The input is **search terms**, not a known list of keys, so each plugin *discovers* its own rows
rather than enriching a supplied identifier. Because a later plugin can add columns to a row an
earlier plugin already produced (same key value), no row can be written until every selected
plugin finishes — the framework runs all plugins concurrently, **buffers** their results, merges
by key value, then **writes the CSV once**.

This deliberately drops the previous design's per-row streaming and output-CSV-as-resume-journal
(see git history before commit `ec6d628`): with no input key list there is nothing to resume
per-key, and merge inherently needs all results in hand. Re-running simply overwrites the output.

Plugin failures are isolated (logged, run continues with partial output); only a contract
violation — a returned row whose keys don't match the plugin's declared `key`+`fields` — is fatal,
since it signals a plugin bug rather than a data miss.
