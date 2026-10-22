"""Naver Maps geocoding, driving-route, and public-transit-link helpers."""

from __future__ import annotations

import json
import argparse
import base64
from functools import lru_cache
from html import unescape
import math
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from datetime import timedelta
from urllib.parse import quote, urlencode
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from dotenv import load_dotenv

load_dotenv()


START_ADDRESS = "경기도 광명시 오리로 801"
EOCODE_URL = "https://maps.apigw.ntruss.com/map-geocode/v2/geocode"
DIRECTIONS_URL = "https://maps.apigw.ntruss.com/map-direction/v1/driving"


class NaverRouteError(RuntimeError):
    """Raised when Naver geocoding or route lookup fails."""


class NaverRouteParseError(NaverRouteError):
    """Raised when loaded route markup cannot be interpreted safely."""


def _find_edge() -> str:
    candidates = [
        os.getenv("NAVER_EDGE_PATH", ""),
        r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
        r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    ]
    for candidate in candidates:
        if candidate and os.path.isfile(candidate):
            return candidate
    discovered = shutil.which("msedge") or shutil.which("chrome")
    if discovered:
        return discovered
    raise NaverRouteError(
        "A Chromium browser is required for public-transit details. "
        "Install Microsoft Edge or set NAVER_EDGE_PATH."
    )


def _duration_text(text: str) -> str:
    hours = re.search(r"(\d+)\s*시간", text)
    minutes = re.search(r"(\d+)\s*분", text)
    seconds = re.search(r"(\d+)\s*초", text)
    pieces = []
    if hours:
        pieces.append(f"{hours.group(1)}hr")
    if minutes:
        pieces.append(f"{minutes.group(1)}min")
    if seconds and not pieces:
        pieces.append(f"{seconds.group(1)}sec")
    return " ".join(pieces) or text.strip()


def _extract_public_transit_details(html: str) -> dict:
    markers = list(re.finditer(r"StyledPubtransDirectionsSummaryItem-[^\"]+", html))
    if not markers:
        if re.search(r"StyledDirectionsError|검색 결과를 불러오는 중 오류|search results.*error", html, re.I):
            raise NaverRouteError(
                "Naver Maps returned a directions-service error while loading this route. "
                "The address lookup succeeded, but Naver did not return transit data; "
                "retry later or open the full Naver Maps page."
            )
        raise NaverRouteError(
            f"Naver loaded, but no public-transit route was found (HTML bytes: {len(html.encode('utf-8'))})"
        )

    # The first summary item is Naver's recommended (최적) route.
    start = markers[0].start()
    end = markers[1].start() if len(markers) > 1 else len(html)
    block = html[start:end]

    def text_only(value: str) -> str:
        return " ".join(unescape(re.sub(r"<[^>]+>", " ", value)).split())

    total_match = re.search(r'class="[^"]*wrap_time_taken[^"]*"[^>]*>(.*?)</em>', block, re.S)
    total = text_only(total_match.group(1)) if total_match else ""
    timeline = []
    for match in re.finditer(r'<li[^>]*class="([^"]*StyledPubtransDirectionsTimelineItem[^"]*)"[^>]*>(.*?)</li>', block, re.S):
        kind_match = re.search(r"_adjust_visible_target_type_([\w-]+)", match.group(1))
        duration_match = re.search(r'class="[^"]*pubtrans_time_taken[^"]*"[^>]*>(.*?)</span>', match.group(2), re.S)
        duration = _duration_text(text_only(duration_match.group(1)) if duration_match else text_only(match.group(2)))
        number_match = re.search(r"\d+", duration)
        timeline.append({"kind": kind_match.group(1) if kind_match else "", "duration": duration, "minutes": int(number_match.group()) if number_match else 0})

    walking_minutes = sum(item["minutes"] for item in timeline if item["kind"] == "walking")
    transit_times = [item for item in timeline if item["kind"] != "walking"]
    mode_markers = list(re.finditer(r'class="[^"]*summary_icon_label[^"]*"[^>]*>(.*?)</(?:em|span)>', block, re.S))
    steps = []
    for index, mode_match in enumerate(mode_markers):
        segment_end = mode_markers[index + 1].start() if index + 1 < len(mode_markers) else len(block)
        segment = block[mode_match.start():segment_end]
        station_match = re.search(r'class="[^"]*citystep_station[^"]*"[^>]*>(.*?)</(?:em|span)>', segment, re.S)
        if not station_match:
            continue
        mode = text_only(mode_match.group(1))
        station = text_only(station_match.group(1))
        bus_match = re.search(r'class="[^"]*alt_bus_arrival_number[^"]*"[^>]*>(.*?)</(?:em|span)>', segment, re.S)
        bus_number = text_only(bus_match.group(1)) if bus_match else ""
        if not bus_number:
            for candidate in re.finditer(r'class="[^"]*(?:bus|route)[^"]*"[^>]*>(.*?)</(?:em|span)>', segment, re.I | re.S):
                candidate_text = text_only(candidate.group(1))
                if re.fullmatch(r"\d{1,4}[A-Za-z]?|[가-힣]+\d{1,3}", candidate_text):
                    bus_number = candidate_text
                    break
        if mode in {"마을", "간선", "지선", "순환", "직행", "광역", "공항"} and bus_number:
            line = bus_number
        else:
            line = ""
        if mode == "하차":
            steps.append({"mode": mode, "station": station})
            continue
        steps.append({"mode": mode, "line": line, "station": station})

    transit_steps = [step for step in steps if step["mode"] != "하차"]
    for step in transit_steps:
        step["label"] = step["mode"] + (f" {step['line']}" if step.get("line") else "")
        step["duration"] = ""
    for step, timing in zip(transit_steps, transit_times):
        label = step["mode"]
        if step.get("line"):
            label = f"{label} {step['line']}"
        step["label"] = label
        step["duration"] = timing["duration"]

    destination = next((step["station"] for step in steps if step["mode"] == "하차"), "")
    if not total or not transit_steps or len({step["station"] for step in transit_steps}) < min(2, len(transit_steps)):
        raise NaverRouteParseError("Naver route details could not be parsed reliably; open the full Naver Maps page")
    return {
        "timespan": _duration_text(total),
        "walk_total": f"{walking_minutes}min",
        "steps": [{"label": step["label"], "station": step["station"], "duration": step.get("duration", "")} for step in transit_steps],
        "destination": destination,
    }


def _headless_public_transit_details(web_url: str, include_screenshot: bool = False) -> dict:
    edge = _find_edge()
    last_error = None
    for attempt in range(1, 3):
        screenshot_data = ""
        with tempfile.TemporaryDirectory(prefix="naver-route-clean-") as profile, tempfile.NamedTemporaryFile() as output:
            screenshot_path = os.path.join(profile, "route.png")
            command = [
                edge, "--headless=new", "--disable-gpu", "--inprivate",
                "--no-first-run", "--no-default-browser-check",
                f"--user-data-dir={profile}", "--virtual-time-budget=45000",
                "--run-all-compositor-stages-before-draw", "--window-size=1280,900",
            ]
            if include_screenshot:
                command.extend([f"--screenshot={screenshot_path}"])
            command.extend(["--dump-dom", web_url])
            try:
                completed = subprocess.run(
                    command,
                    stdout=output,
                    stderr=subprocess.DEVNULL,
                    timeout=60,
                    check=False,
                )
            except subprocess.TimeoutExpired as exc:
                raise NaverRouteError("Headless Naver browser timed out while loading directions") from exc
            output.flush()
            output.seek(0)
            html = output.read().decode("utf-8", errors="replace")
            if include_screenshot:
                for _ in range(20):
                    if os.path.isfile(screenshot_path) and os.path.getsize(screenshot_path) > 0:
                        break
                    time.sleep(0.25)
                if os.path.isfile(screenshot_path):
                    with open(screenshot_path, "rb") as image_file:
                        screenshot_data = base64.b64encode(image_file.read()).decode("ascii")
        if completed.returncode not in {0, 1} and not html:
            raise NaverRouteError(f"Headless Naver browser failed with exit code {completed.returncode}")
        try:
            details = _extract_public_transit_details(html)
        except NaverRouteError as exc:
            last_error = exc
            if isinstance(exc, NaverRouteParseError):
                raise
            if attempt < 2:
                time.sleep(1)
                continue
            raise last_error from exc
        if include_screenshot and screenshot_data:
            details["screenshot_data"] = screenshot_data
        return details
    raise last_error or NaverRouteError("Naver public-transit route could not be loaded")


@lru_cache(maxsize=256)
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
    except HTTPError as exc:
        if exc.code == 401:
            raise NaverRouteError(
                "Naver rejected the Maps credentials (HTTP 401). Create or open "
                "a Maps application in NAVER Cloud Console, enable Geocoding, "
                "and copy that application's Client ID and Client Secret into "
                "NAVER_MAP_CLIENT_ID and NAVER_MAP_CLIENT_SECRET. Do not use the "
                "general Cloud Access Key/Secret Key here."
            ) from exc
        raise NaverRouteError(
            f"Naver geocoding request failed for '{address}' (HTTP {exc.code})"
        ) from exc
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
    include_details: bool = False,
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
    # Naver's desktop directions URL uses Web Mercator coordinates, while the
    # app URL above uses latitude/longitude.  This is a web link fallback for
    # desktop browsers that cannot handle the nmap:// custom scheme.
    def web_mercator(latitude: float, longitude: float) -> tuple[float, float]:
        x = longitude * 20037508.34 / 180
        y = math.log(math.tan((90 + latitude) * math.pi / 360)) / (math.pi / 180)
        y = y * 20037508.34 / 180
        return x, y

    start_x, start_y = web_mercator(start_lat, start_lng)
    destination_x, destination_y = web_mercator(destination_lat, destination_lng)
    start = f"{start_x:.6f},{start_y:.6f},{quote(start_address)},,ADDRESS_POI"
    goal = f"{destination_x:.6f},{destination_y:.6f},{quote(destination)},,ADDRESS_POI"
    result = {
        "start_address": start_address,
        "destination": destination,
        "url": f"nmap://route/public?{params}",
        "web_url": f"https://map.naver.com/p/directions/{start}/{goal}/-/transit",
        "start": {"lat": start_lat, "lng": start_lng},
        "destination_point": {"lat": destination_lat, "lng": destination_lng},
    }
    if include_details:
        result["details"] = _headless_public_transit_details(result["web_url"])
    return result


def driving_route(
    destination: str,
    client_id: str,
    client_secret: str,
    start_address: str = START_ADDRESS,
    option: str = "traoptimal",
) -> dict:
    """Return a compact driving route summary from Naver Directions 5."""
    if not client_id or not client_secret:
        raise NaverRouteError(
            "Set NAVER_MAP_CLIENT_ID and NAVER_MAP_CLIENT_SECRET to enable routes"
        )

    start_lat, start_lng = geocode(start_address, client_id, client_secret)
    destination_lat, destination_lng = geocode(destination, client_id, client_secret)
    query = urlencode({
        "start": f"{start_lng},{start_lat}",
        "goal": f"{destination_lng},{destination_lat}",
        "option": option,
        "lang": "ko",
    })
    request = Request(
        f"{DIRECTIONS_URL}?{query}",
        headers={
            "Accept": "application/json",
            "x-ncp-apigw-api-key-id": client_id,
            "x-ncp-apigw-api-key": client_secret,
        },
    )
    try:
        with urlopen(request, timeout=30) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        raise NaverRouteError(
            f"Naver directions request failed (HTTP {exc.code}). "
            "Enable Directions 5 in the Maps application."
        ) from exc
    except Exception as exc:
        raise NaverRouteError(f"Naver directions request failed: {exc}") from exc

    if payload.get("code") != 0:
        raise NaverRouteError(
            f"Naver directions failed: {payload.get('message', 'unknown error')}"
        )
    routes = payload.get("route", {}).get(option, [])
    if not routes:
        raise NaverRouteError(f"Naver returned no route for option '{option}'")

    summary = routes[0]["summary"]
    duration = timedelta(milliseconds=summary["duration"])
    return {
        "start_address": start_address,
        "destination": destination,
        "option": option,
        "distance_km": round(summary["distance"] / 1000, 1),
        "duration": str(duration).split(".", 1)[0],
        "duration_minutes": round(summary["duration"] / 60000),
        "toll_fare": summary.get("tollFare", 0),
        "taxi_fare": summary.get("taxiFare", 0),
        "fuel_price": summary.get("fuelPrice", 0),
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
    parser.add_argument(
        "--public-transit-url",
        action="store_true",
        help="Print a Naver Maps public-transit app URL instead of route details",
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
        if args.public_transit_url:
            result = public_transit_route(
                args.address,
                client_id,
                client_secret,
                start_address=args.start,
            )
        else:
            result = driving_route(
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
