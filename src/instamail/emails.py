import logging
from typing import Iterable

from email_validator import EmailNotValidError, validate_email

log = logging.getLogger(__name__)


def clean_emails(lines: Iterable[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for raw in lines:
        line = raw.strip()
        if not line:
            continue
        try:
            normalized = validate_email(line, check_deliverability=False).normalized.lower()
        except EmailNotValidError as e:
            log.warning("skipping invalid email %r: %s", line, e)
            continue
        if normalized in seen:
            continue
        seen.add(normalized)
        out.append(normalized)
    return out
