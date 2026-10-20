"""Naver Maps geocoding and public-transit route helpers."""

from __future__ import annotations

import json
import argparse
import os
import sys
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from dotenv import load_dotenv

load_dotenv()


START_ADDRESS = "서울 금천구 가산디지털2로 61"
GEOCODE_URL = "https://naveropenapi.apigw.ntruss.com/map-geocode/v2/geocode"


class NaverRouteError(RuntimeError):
    """Raised when Naver geocoding or route-link construction fails."""


def geocode(address: str, client_id: str, client_secret: str) -> tuple[float, float]:
    query = urlencode({"query": address, "count": "1"})
    request = Request(
        f"{GEOCODE_URL}?{query}",
        headers={
            "Accept": "application/json",
            "x-ncp-apigw-api-key-id": client_id,
            "x-ncp-apigw-api-key": client_secret,
        },
    )
    try:
        with urlopen(request, timeout=30) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except Exception as exc:
        raise NaverRouteError(f"Naver geocoding request failed for '{address}': {exc}") from exc
    addresses = payload.get("addresses", [])
    if not addresses:
        raise NaverRouteError(f"Naver could not geocode: {address}")
    item = addresses[0]
    return float(item["y"]), float(item["x"])


def public_transit_route(
    destination: str,
    client_id: str,
    client_secret: str,
    start_address: str = START_ADDRESS,
) -> dict:
    if not client_id or not client_secret:
        raise NaverRouteError(
            "Set NAVER_MAP_CLIENT_ID and NAVER_MAP_CLIENT_SECRET to enable subway routes"
        )
    start_lat, start_lng = geocode(start_address, client_id, client_secret)
    destination_lat, destination_lng = geocode(destination, client_id, client_secret)
    params = urlencode({
        "slat": start_lat,
        "slng": start_lng,
        "sname": start_address,
        "dlat": destination_lat,
        "dlng": destination_lng,
        "dname": destination,
        "appname": "corporation-survey",
    })
    return {
        "start_address": start_address,
        "destination": destination,
        "url": f"nmap://route/public?{params}",
        "start": {"lat": start_lat, "lng": start_lng},
        "destination_point": {"lat": destination_lat, "lng": destination_lng},
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Test Naver Maps geocoding and public-transit routing."
    )
    parser.add_argument(
        "address",
        nargs="?",
        help="Destination address; omit to test the default start address only",
    )
    parser.add_argument(
        "--start",
        default=START_ADDRESS,
        help=f"Starting address (default: {START_ADDRESS})",
    )
    args = parser.parse_args()

    client_id = os.getenv("NAVER_MAP_CLIENT_ID", "")
    client_secret = os.getenv("NAVER_MAP_CLIENT_SECRET", "")
    # print(f"Using Naver Maps Client ID: {client_id}")
    # print(f"Using Naver Maps Client Secret: {client_secret}")
    if not client_id or not client_secret:
        print(
            "ERROR: Set NAVER_MAP_CLIENT_ID and NAVER_MAP_CLIENT_SECRET first.",
            file=sys.stderr,
        )
        return 2

    try:
        if not args.address:
            lat, lng = geocode(args.start, client_id, client_secret)
            print(json.dumps({
                "ok": True,
                "mode": "start_address_geocode",
                "address": args.start,
                "lat": lat,
                "lng": lng,
            }, ensure_ascii=False, indent=2))
            return 0
        result = public_transit_route(
            args.address,
            client_id,
            client_secret,
            start_address=args.start,
        )
    except NaverRouteError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
