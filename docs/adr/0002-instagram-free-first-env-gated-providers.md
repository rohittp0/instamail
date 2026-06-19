# Instagram plugin: free-first, env-gated provider chains

The `instagram` plugin resolves each pipeline layer (discovery → enrichment → email →
verification) through a chain of interchangeable providers ordered by tier
(**FREE → OFFICIAL → PAID**). A provider is eligible only when every env var it requires is set,
and a layer takes the first success, so paid providers (Bright Data/Apify vendor, Hunter) run
only as a genuine last resort and never when a free source already answered. `--instagram-max-tier`
caps the tier per run. This was chosen over a single hardcoded data source because the user wants
to support all access models (public, official Meta, paid vendor) but never pay when a free path
suffices, and because credentials differ per deployment.

**Consequence — ToS risk in the default path.** With no credentials, the only eligible providers
are FREE ones that scrape public Instagram / search-engine endpoints (DuckDuckGo dorking, the
`web_profile_info` endpoint via curl_cffi TLS impersonation, IG topsearch). This violates
Instagram's Terms and is the highest enforcement-risk approach per the project's own research; it
is accepted deliberately to stay runnable without paid/official onboarding. The plugin logs a
warning on the free path. The official Meta and vendor adapters carry real request code but are
verified only via mocked tests this iteration — there are no live credentials to exercise them.

**Consequence — "top by reel reach" is a proxy.** No sanctioned API ranks arbitrary public
creators by reach, so ranking uses avg/max Reel view counts from public profile data
(`metrics.py`) over a recent-reels window, not an official reach metric.
