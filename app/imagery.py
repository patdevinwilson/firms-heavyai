"""Satellite-matched NASA GIBS daily imagery, cached by sensor/day/tile.

Every observation gets a descriptor. Fetching is lazy on popup open, so records
in the same tile share one download. These are mosaics, not exact L1B overpasses.
"""
import hashlib
import json
import math
import time
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import urlopen

SATELLITES = {"N": "SNPP", "N20": "NOAA20", "N21": "NOAA21"}
ENDPOINT = "https://gibs.earthdata.nasa.gov/wms/epsg4326/best/wms.cgi"
PRODUCTS = {"N": "VNP02IMG", "N20": "VJ102IMG", "N21": "VJ202IMG"}
CACHE_LOCK = threading.Lock()


def granule_search(satellite, acquired_at, latitude, longitude):
    """Find candidate L1B granules covering the point and observation minute.

    A CMR match is provenance, not proof that a browse image shows the fire pixel.
    Files may require Earthdata authentication. No credentials are needed to search.
    """
    stamp = datetime.fromisoformat(str(acquired_at).replace("Z", "+00:00"))
    stamp = stamp.replace(tzinfo=timezone.utc) if stamp.tzinfo is None else stamp.astimezone(timezone.utc)
    product = PRODUCTS[satellite]
    params = {"short_name[]": [product, product + "_NRT"],
              "point": f"{float(longitude)},{float(latitude)}", "page_size": 30,
              "temporal": ",".join((stamp + timedelta(minutes=m)).strftime("%Y-%m-%dT%H:%M:%SZ") for m in (-6, 1))}
    url = "https://cmr.earthdata.nasa.gov/search/granules.json?" + urlencode(params, doseq=True)
    with urlopen(url, timeout=30) as response:
        entries = json.load(response).get("feed", {}).get("entry", [])
    candidates = []
    for entry in entries:
        start = datetime.fromisoformat(entry["time_start"].replace("Z", "+00:00"))
        end = datetime.fromisoformat(entry["time_end"].replace("Z", "+00:00"))
        if not (start < stamp + timedelta(minutes=1) and end > stamp):
            continue
        links = [{"url": link["href"], "kind": "browse" if "browse" in link.get("rel", "") else "data"}
                 for link in entry.get("links", []) if link.get("href", "").startswith("https://")
                 and any(kind in link.get("rel", "") for kind in ("browse", "data"))]
        candidates.append({"id": entry["id"], "title": entry.get("title"),
                           "start": entry["time_start"], "end": entry["time_end"], "links": links})
    return {"product": product, "search_url": url, "granules": candidates,
            "note": "Candidate matching instrument granules; browse availability and exact pixel match are not guaranteed. Data downloads may require Earthdata login."}


def descriptor(satellite, acquired_at, latitude, longitude, daynight):
    sensor = SATELLITES[satellite]
    date = str(acquired_at)[:10]
    datetime.strptime(date, "%Y-%m-%d")
    lat, lon = float(latitude), float(longitude)
    if not math.isfinite(lat) or not math.isfinite(lon) or not -90 <= lat <= 90 or not -180 <= lon <= 180:
        raise ValueError("Invalid coordinate")
    if daynight not in {"D", "N"}:
        raise ValueError("Invalid observation mode")
    # Non-overlapping quarter-degree tiles give shared cache keys, including edges.
    west = min(179.75, math.floor(lon * 4) / 4)
    south = min(89.75, math.floor(lat * 4) / 4)
    bbox = [west, south, west + .25, south + .25]
    layer = f"VIIRS_{sensor}_" + ("Brightness_Temp_BandI5_Night" if daynight == "N"
                                  else "CorrectedReflectance_TrueColor")
    params = {"SERVICE": "WMS", "VERSION": "1.1.1", "REQUEST": "GetMap",
              "LAYERS": layer, "STYLES": "", "FORMAT": "image/png", "TRANSPARENT": "TRUE",
              "SRS": "EPSG:4326", "BBOX": ",".join(map(str, bbox)),
              "WIDTH": "384", "HEIGHT": "384", "TIME": date}
    url = ENDPOINT + "?" + urlencode(params)
    return {"url": url, "cache_key": hashlib.sha256(url.encode()).hexdigest(),
            "layer": layer, "date": date, "bbox": bbox,
            "marker": [(lon - west) / .25, 1 - (lat - south) / .25],
            "kind": "night I5 brightness temperature" if daynight == "N" else "day true colour",
            "provenance": "NASA GIBS daily mosaic; exact overpass match is not guaranteed",
            "worldview_url": "https://worldview.earthdata.nasa.gov/?" + urlencode({
                "t": date, "l": layer, "v": ",".join(map(str, bbox))})}


def fetch_image(info, cache=Path("/var/cache/firms-imagery")):
    cache.mkdir(parents=True, exist_ok=True)
    target = cache / (info["cache_key"] + ".png")
    # GIBS mosaics can be revised as swaths arrive. Refresh even cached images.
    with CACHE_LOCK:
        if target.exists() and time.time() - target.stat().st_mtime < 3600:
            return target.read_bytes()
    with urlopen(info["url"], timeout=30) as response:
        data = response.read(4 * 1024 * 1024 + 1)
        if not data.startswith(b"\x89PNG\r\n\x1a\n") or len(data) > 4 * 1024 * 1024:
            raise ValueError("NASA did not return a usable PNG")
    # Atomic replace permits concurrent reads; each writer has a unique temporary.
    import tempfile
    with tempfile.NamedTemporaryFile(dir=cache, delete=False) as handle:
        temp = Path(handle.name)
        handle.write(data)
    with CACHE_LOCK:
        temp.replace(target)
        files = sorted(cache.glob("*.png"), key=lambda p: p.stat().st_mtime)
        size = sum(p.stat().st_size for p in files)
        for file in files:
            if size <= 2 * 1024**3:
                break
            size -= file.stat().st_size
            file.unlink(missing_ok=True)
    return data
