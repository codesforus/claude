"""ARES (Czech business register) lookups: free, no API key.

Used to verify IČO given by the AI researcher and to drop dissolved companies.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

import httpx

log = logging.getLogger(__name__)

ARES_BASE_URL = "https://ares.gov.cz/ekonomicke-subjekty-v-be/rest"


@dataclass
class AresRecord:
    ico: str
    name: str
    address: str
    active: bool


def lookup(ico: str, http: httpx.Client | None = None) -> AresRecord | None:
    """Return the register record, or None if the IČO doesn't exist or ARES is unreachable."""
    client = http or httpx.Client(timeout=15)
    try:
        response = client.get(f"{ARES_BASE_URL}/ekonomicke-subjekty/{ico}")
    except httpx.HTTPError as e:
        log.warning("ARES nedostupný (%s): %s", ico, e)
        return None
    finally:
        if http is None:
            client.close()
    if response.status_code != 200:
        return None
    data = response.json()
    return AresRecord(
        ico=data.get("ico", ico),
        name=data.get("obchodniJmeno", ""),
        address=(data.get("sidlo") or {}).get("textovaAdresa", ""),
        active=not data.get("datumZaniku"),
    )
