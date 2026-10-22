"""Google Places (Text Search, New API) as a source of company phone numbers.

Results have no fit score yet; they go through AI qualification (leadgen.qualify).
"""

from __future__ import annotations

import logging

import httpx

from leadgen.discovery.models import Candidate

log = logging.getLogger(__name__)

PLACES_URL = "https://places.googleapis.com/v1/places:searchText"
FIELD_MASK = ",".join(
    [
        "places.id",
        "places.displayName",
        "places.formattedAddress",
        "places.nationalPhoneNumber",
        "places.internationalPhoneNumber",
        "places.websiteUri",
        "places.businessStatus",
        "nextPageToken",
    ]
)


def search(
    api_key: str,
    query: str,
    region: str,
    segment: str,
    max_pages: int = 2,
    http: httpx.Client | None = None,
) -> list[Candidate]:
    client = http or httpx.Client(timeout=30)
    headers = {"X-Goog-Api-Key": api_key, "X-Goog-FieldMask": FIELD_MASK}
    body: dict = {
        "textQuery": f"{query} {region}",
        "languageCode": "cs",
        "regionCode": "CZ",
        "pageSize": 20,
    }
    candidates: list[Candidate] = []
    try:
        for _ in range(max_pages):
            response = client.post(PLACES_URL, json=body, headers=headers)
            response.raise_for_status()
            data = response.json()
            for place in data.get("places", []):
                if place.get("businessStatus", "OPERATIONAL") != "OPERATIONAL":
                    continue
                phone = place.get("internationalPhoneNumber") or place.get("nationalPhoneNumber")
                if not phone:
                    continue
                candidates.append(
                    Candidate(
                        company=(place.get("displayName") or {}).get("text", ""),
                        source="google_places",
                        source_ref=place.get("id"),
                        phone=phone,
                        website=place.get("websiteUri"),
                        address=place.get("formattedAddress"),
                        region=region,
                        segment=segment,
                    )
                )
            token = data.get("nextPageToken")
            if not token:
                break
            body = {**body, "pageToken": token}
    finally:
        if http is None:
            client.close()
    log.info("Google Places '%s' / %s: %d firem s telefonem", query, region, len(candidates))
    return candidates
