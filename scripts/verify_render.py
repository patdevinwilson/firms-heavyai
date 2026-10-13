"""Exercise GPU-backed FIRMS point rendering and save a PNG on the host."""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from app.collector import HeavyDB, settings


def main():
    _, secret = settings()
    db = HeavyDB(secret, user="firms_reader", password=secret["reader_password"])
    try:
        client, session = db.connection._client, db.connection._session
        status = client.get_server_status(session)
        if not status.rendering_enabled:
            raise RuntimeError("HeavyDB reports GPU rendering disabled")
        spec = {
            "width": 640, "height": 360,
            "data": [{"name": "detections", "sql": "SELECT longitude AS lon, latitude AS lat "
                      "FROM firms_observations WHERE latitude BETWEEN -80 AND 80 LIMIT 20000"}],
            "projections": [{"name": "merc", "type": "mercator",
                             "bounds": {"x": [-180, 180], "y": [-80, 80]}}],
            "marks": [{"type": "symbol", "from": {"data": "detections"},
                       "properties": {"xc": {"field": "lon"}, "yc": {"field": "lat"},
                                      "fillColor": "orange", "shape": "circle", "width": 4, "height": 4},
                       "transform": {"projection": "merc"}}],
        }
        result = client.render_vega(session, 991, json.dumps(spec), 1, "firms-render-check")
        if not result.image.startswith(b"\x89PNG\r\n\x1a\n"):
            raise RuntimeError("Render endpoint did not return a PNG")
        (ROOT / ".render-check.png").write_bytes(result.image)
        print(json.dumps({"rendering_enabled": status.rendering_enabled,
                          "polygon_rendering_enabled": status.poly_rendering_enabled,
                          "png_bytes": len(result.image), "render_time_ms": result.render_time_ms,
                          "total_time_ms": result.total_time_ms}))
    finally:
        db.close()


if __name__ == "__main__":
    main()
