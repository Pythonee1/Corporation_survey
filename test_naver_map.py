"""Small live test for the NAVER Maps geocoding API.

Run with::

    python test_naver_map.py

The test uses NAVER_MAP_CLIENT_ID and NAVER_MAP_CLIENT_SECRET from the
environment (or the project's .env file, loaded by naver_route.py).
"""

from __future__ import annotations
import os
from naver_route import geocode


TEST_ADDRESS = os.getenv("NAVER_MAP_TEST_ADDRESS", "서울특별시 중구 세종대로 67")


def test_geocode_address() -> tuple[float, float]:
    """Return latitude/longitude for one address using NAVER geocoding."""
    client_id = os.getenv("NAVER_MAP_CLIENT_ID", "")
    client_secret = os.getenv("NAVER_MAP_CLIENT_SECRET", "")
    if not client_id or not client_secret:
        raise RuntimeError(
            "Set NAVER_MAP_CLIENT_ID and NAVER_MAP_CLIENT_SECRET before running "
            "this test."
        )

    latitude, longitude = geocode(TEST_ADDRESS, client_id, client_secret)
    assert -90 <= latitude <= 90
    assert -180 <= longitude <= 180
    return latitude, longitude


if __name__ == "__main__":
    latitude, longitude = test_geocode_address()
    print(f"{TEST_ADDRESS} -> latitude={latitude}, longitude={longitude}")
