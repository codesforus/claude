"""Normalization of the identifiers used for deduplication: phone, domain, IČO."""

from __future__ import annotations

import re
from urllib.parse import urlsplit

import phonenumbers
from phonenumbers import NumberParseException, PhoneNumberFormat, PhoneNumberType

# Websites that belong to a directory or social network, not to the company itself.
GENERIC_HOSTS = {
    "facebook.com",
    "instagram.com",
    "linkedin.com",
    "youtube.com",
    "google.com",
    "maps.google.com",
    "goo.gl",
    "seznam.cz",
    "firmy.cz",
    "mapy.cz",
    "mapy.com",
    "zivefirmy.cz",
    "najisto.centrum.cz",
}

_REJECTED_TYPES = {PhoneNumberType.PREMIUM_RATE, PhoneNumberType.SHARED_COST}


def normalize_phone(raw: str | None, region: str = "CZ") -> str | None:
    """Return the number in E.164 (+420...) or None if it is not a valid callable number."""
    if not raw or not raw.strip():
        return None
    try:
        num = phonenumbers.parse(raw, region)
    except NumberParseException:
        return None
    if not phonenumbers.is_valid_number(num):
        return None
    if phonenumbers.number_type(num) in _REJECTED_TYPES:
        return None
    return phonenumbers.format_number(num, PhoneNumberFormat.E164)


def format_phone(e164: str | None) -> str:
    """Human-readable form for emails, e.g. +420 601 234 567."""
    if not e164:
        return ""
    try:
        return phonenumbers.format_number(
            phonenumbers.parse(e164, None), PhoneNumberFormat.INTERNATIONAL
        )
    except NumberParseException:
        return e164


def domain_of(url: str | None) -> str | None:
    if not url or not url.strip():
        return None
    url = url.strip()
    if "://" not in url:
        url = "http://" + url
    host = (urlsplit(url).hostname or "").lower()
    for prefix in ("www.", "m."):
        host = host.removeprefix(prefix)
    if not host or "." not in host or host in GENERIC_HOSTS:
        return None
    return host


def normalize_ico(raw: str | None) -> str | None:
    """Czech company ID: 8 digits (shorter historical IDs are zero-padded)."""
    if not raw:
        return None
    digits = re.sub(r"\D", "", raw)
    if not digits or len(digits) > 8:
        return None
    return digits.zfill(8)


def normalize_email(raw: str | None) -> str | None:
    if not raw:
        return None
    email = raw.strip().lower()
    return email if re.fullmatch(r"[^@\s]+@[^@\s]+\.[a-z]{2,}", email) else None
