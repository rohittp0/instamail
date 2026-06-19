"""Verification provider (free-only): syntax + MX deliverability via email-validator.

Returns a confidence label fed into the row's ``email_confidence`` column. There is no paid
verifier (ZeroBounce was explicitly dropped)."""

from __future__ import annotations

from ..config import Tier
from .base import Verifier


class SyntaxVerifier(Verifier):
    name = "syntax_mx"
    tier = Tier.FREE

    def __init__(self, check_deliverability: bool = True):
        self._check_deliverability = check_deliverability

    async def verify(self, email: str) -> str:
        from email_validator import EmailNotValidError, validate_email

        try:
            validate_email(email, check_deliverability=self._check_deliverability)
            return "valid"
        except EmailNotValidError:
            return "invalid"
        except Exception:
            # DNS/network hiccup during MX check — can't confirm, don't reject.
            return "unknown"
