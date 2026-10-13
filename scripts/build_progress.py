"""Safe, read-only build telemetry; never returns raw runtime log contents."""
import json
import math
import os
import re
import shutil
import time
from pathlib import Path

STAGES = {"model_install": "Nemotron installation", "heavyai_dependencies": "HEAVY.AI dependencies",
          "heavyai_components": "HeavyDB / Immerse / HeavyIQ compilation",
          "engine_initialize": "HeavyDB initialization", "collection": "Initial FIRMS collection",
          "render_verification": "GPU rendering verification",
          "iq_evaluation": "Native HeavyIQ SQL evaluation", "ready": "Runtime ready"}


def snapshot(root=Path("/"), now=None):
    now = time.time() if now is None else now
    main = root / "var/log/firms-runtime-install.log"
    logs = [main] + list((root / "opt/heavyai-source/heavydb/build/logs").glob("*.log"))
    logs = [path for path in logs if path.is_file()]
    report = {"stage": "Runtime setup", "component_percent": None,
              "last_output_seconds_ago": None}
    if logs:
        newest = max(logs, key=lambda path: path.stat().st_mtime)
        with newest.open("rb") as handle:
            handle.seek(max(0, newest.stat().st_size - 65536))
            tail = handle.read().decode("utf-8", errors="replace")
        marker_matches = list(re.finditer(r"\[firms-progress\] ([a-z_]+)", tail))
        markers = [match.group(1) for match in marker_matches]
        current_tail = tail[marker_matches[-1].end():] if marker_matches else tail
        if markers:
            report["stage"] = STAGES.get(markers[-1], "Runtime setup")
        if newest != main:
            report["stage"] = "HeavyDB / Immerse / HeavyIQ compilation"
        terminal_stage = newest != main or bool(markers and markers[-1] not in {"heavyai_dependencies"})
        if not terminal_stage and ("LLVM" in current_tail or "/llvm-" in current_tail):
            report["stage"] = "Dependencies: LLVM"
        elif not terminal_stage and ("GDAL" in current_tail or "gdal-" in current_tail):
            report["stage"] = "Dependencies: GDAL"
        elif not terminal_stage and ("boost_" in current_tail or "gcc.compile.c++" in current_tail):
            report["stage"] = "Dependencies: Boost"
        percentages = re.findall(r"^\[\s*(\d{1,3})%\] (?:Building|Built|Linking|Generating)", current_tail, re.M)
        if percentages and not (markers and markers[-1] in {"engine_initialize", "collection", "render_verification", "iq_evaluation", "ready"}):
            report["component_percent"] = min(100, int(percentages[-1]))
        report["last_output_seconds_ago"] = max(0, math.floor(now - newest.stat().st_mtime))
    report["load_average_1m"] = round(os.getloadavg()[0], 1)
    report["disk_free_gb"] = round(shutil.disk_usage(root).free / 1024**3, 1)
    return report


if __name__ == "__main__":
    print(json.dumps(snapshot()))
