from __future__ import annotations
import logging
import math
import time
from typing import Iterator

import httpx

from .models import BusinessDetails, Review
from .storage import get_cached, set_cached

log = logging.getLogger(__name__)

_BASE = "https://maps.googleapis.com/maps/api/place"
_GEO_BASE = "https://maps.googleapis.com/maps/api/geocode"
_DETAIL_FIELDS = (
    "place_id,name,formatted_address,formatted_phone_number,"
    "website,rating,user_ratings_total,business_status,"
    "opening_hours,reviews,url"
)


def geocode_location(api_key: str, location: str) -> tuple[float, float] | None:
    """Convert a location name to (lat, lng). Cached."""
    cache_key = f"geocode:{location}"
    cached = get_cached(cache_key)
    if cached:
        return tuple(cached)  # type: ignore[return-value]

    try:
        resp = httpx.get(
            f"{_GEO_BASE}/json",
            params={"address": location, "key": api_key},
            timeout=10,
        )
        resp.raise_for_status()
        data = resp.json()
        if data.get("status") != "OK" or not data.get("results"):
            log.warning(f"Geocoding failed for '{location}': {data.get('status')}")
            return None
        loc = data["results"][0]["geometry"]["location"]
        coords = [loc["lat"], loc["lng"]]
        set_cached(cache_key, coords)
        return (coords[0], coords[1])
    except Exception as exc:
        log.error(f"Geocoding error: {exc}")
        return None


def _grid_points(
    lat: float, lng: float, radius_km: float, tile_km: float
) -> Iterator[tuple[float, float]]:
    """Yield grid centre points that tile the search area."""
    lat_deg = tile_km / 111.0
    lng_deg = tile_km / (111.0 * math.cos(math.radians(lat)))

    steps = math.ceil(radius_km / tile_km)
    for di in range(-steps, steps + 1):
        for dj in range(-steps, steps + 1):
            dist_km = math.sqrt((di * tile_km) ** 2 + (dj * tile_km) ** 2)
            if dist_km <= radius_km:
                yield lat + di * lat_deg, lng + dj * lng_deg


def _tile_search(
    api_key: str, trade: str, lat: float, lng: float, radius_m: int
) -> list[str]:
    """
    Fetch all place_ids (up to 60) for one tile. Caches the aggregated list
    so re-runs never hit a stale page token.
    """
    lat_r = round(lat, 4)
    lng_r = round(lng, 4)
    cache_key = f"tile:{trade}:{lat_r}:{lng_r}:{radius_m}"
    cached = get_cached(cache_key)
    if cached is not None:
        return cached  # type: ignore[return-value]

    place_ids: list[str] = []
    page_token: str | None = None

    while len(place_ids) < 60:  # Google hard cap: 3 pages × 20
        params: dict = {"key": api_key}
        if page_token:
            params["pagetoken"] = page_token
            time.sleep(2)  # Google requires a short delay before using a page token
        else:
            params.update({
                "query": trade,
                "location": f"{lat},{lng}",
                "radius": radius_m,
            })

        try:
            resp = httpx.get(f"{_BASE}/textsearch/json", params=params, timeout=10)
            resp.raise_for_status()
            data = resp.json()
        except Exception as exc:
            log.error(f"Tile search request failed: {exc}")
            break

        status = data.get("status")
        if status == "ZERO_RESULTS":
            break
        if status != "OK":
            log.warning(f"Tile search status '{status}' — stopping pagination.")
            break

        for result in data.get("results", []):
            pid = result.get("place_id")
            if pid:
                place_ids.append(pid)

        page_token = data.get("next_page_token")
        if not page_token:
            break

    set_cached(cache_key, place_ids)
    return place_ids


def text_search(
    api_key: str, trade: str, location: str, radius_km: int, limit: int
) -> list[str]:
    """
    Return up to `limit` unique place_ids by tiling the search area into a grid.

    Each tile queries Google Places with a proper lat/lng + radius so the
    radius parameter is actually respected. Tiles overlap slightly so no
    businesses fall through the gaps.
    """
    coords = geocode_location(api_key, location)
    if not coords:
        log.warning("Geocoding failed — results may be poor (radius ignored by API).")
        # Fall back: single text query without lat/lng
        coords_fallback = None
    else:
        coords_fallback = coords

    lat, lng = coords_fallback or (0.0, 0.0)

    # Tile spacing: 5 km for larger areas, smaller for tight searches
    tile_km = min(float(radius_km), 5.0)
    # Tile search radius = tile spacing × 1.1 so adjacent tiles overlap slightly
    tile_radius_m = int(tile_km * 1000 * 1.1)

    seen: set[str] = set()
    place_ids: list[str] = []

    if coords_fallback:
        for tile_lat, tile_lng in _grid_points(lat, lng, radius_km, tile_km):
            if len(place_ids) >= limit:
                break
            for pid in _tile_search(api_key, trade, tile_lat, tile_lng, tile_radius_m):
                if pid not in seen:
                    seen.add(pid)
                    place_ids.append(pid)
                if len(place_ids) >= limit:
                    break
    else:
        # Geocoding failed: single query fallback (radius is ignored by Google here)
        for pid in _tile_search(api_key, f"{trade} near {location}", lat, lng, radius_km * 1000):
            if pid not in seen:
                seen.add(pid)
                place_ids.append(pid)
            if len(place_ids) >= limit:
                break

    return place_ids[:limit]


def fetch_details(api_key: str, place_id: str) -> BusinessDetails | None:
    cache_key = f"details:{place_id}"
    cached = get_cached(cache_key)

    if not cached:
        try:
            resp = httpx.get(
                f"{_BASE}/details/json",
                params={"place_id": place_id, "fields": _DETAIL_FIELDS, "key": api_key},
                timeout=10,
            )
            resp.raise_for_status()
            cached = resp.json()
            set_cached(cache_key, cached)
        except Exception as exc:
            log.error(f"Details fetch failed for {place_id}: {exc}")
            return None

    r = cached.get("result", {})
    if not r:
        return None

    reviews = [
        Review(
            author_name=rv.get("author_name", ""),
            text=rv.get("text", ""),
            rating=rv.get("rating"),
        )
        for rv in r.get("reviews", [])[:5]
    ]

    return BusinessDetails(
        place_id=place_id,
        name=r.get("name", ""),
        address=r.get("formatted_address", ""),
        phone=r.get("formatted_phone_number", ""),
        website=r.get("website", ""),
        rating=r.get("rating"),
        review_count=r.get("user_ratings_total", 0),
        business_status=r.get("business_status", ""),
        opening_hours=r.get("opening_hours", {}).get("weekday_text", []),
        reviews=reviews,
        google_maps_url=r.get("url", ""),
    )
