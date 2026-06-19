"""Env var names, provider tiers, and the travel-classification keyword list.

Tier ordering drives the free-first preference chains: a layer tries eligible providers in
ascending tier and takes the first success, so PAID providers run only as a last resort.
"""

from enum import IntEnum


class Tier(IntEnum):
    FREE = 0
    OFFICIAL = 1
    PAID = 2


# Env vars that gate each non-free provider (presence == consent to use it).
ENV_SESSIONID = "INSTAGRAM_SESSIONID"       # optional booster for the free public path
ENV_META_TOKEN = "META_ACCESS_TOKEN"        # official (Hashtag Search + Business Discovery)
ENV_META_IG_USER = "META_IG_USER_ID"        # official (Business Discovery requires the IG user id)
ENV_BRIGHTDATA = "BRIGHTDATA_API_KEY"       # paid vendor
ENV_APIFY = "APIFY_TOKEN"                   # paid vendor (alternative)
ENV_HUNTER = "HUNTER_API_KEY"               # paid email finder

CACHE_DIR = ".cache/instagram"
PROFILE_TTL = 24 * 3600       # profiles cached 1 day
DISCOVERY_TTL = 6 * 3600      # discovery results cached 6 hours

TRAVEL_KEYWORDS = (
    "travel", "traveler", "traveller", "wanderlust", "explore", "explorer", "adventure",
    "nomad", "backpack", "backpacker", "vanlife", "roadtrip", "globetrotter", "voyage",
    "destination", "tourism", "tourist", "journey", "expedition", "trip", "getaway",
    "passport", "jetset", "wander", "hiking", "trekking", "itinerary", "staycation",
)
