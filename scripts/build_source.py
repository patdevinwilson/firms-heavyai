"""Build the pinned upstream GPU product, including native HeavyIQ."""
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
COMPONENTS = {
    "immerse": "070acbed6dc966dce8aa7a5cd213b22fdbc900cb",
    "webserver": "54b47c7aee62b122338eae523e6a809b49dd9069",
}


def run(*args, cwd=None):
    subprocess.run(args, cwd=cwd, check=True)


def main():
    config = json.loads((ROOT / "config.json").read_text())
    image = config["heavyai_image"]
    manifest = Path("/opt/heavyai-source/manifest.json")
    refs = dict(COMPONENTS, heavydb=config["heavydb_ref"], heavyiq=config["heavyiq_ref"])
    if manifest.exists() and json.loads(manifest.read_text()).get("refs") == refs:
        if subprocess.run(["docker", "image", "inspect", image], capture_output=True).returncode == 0:
            return
    base = manifest.parent
    base.mkdir(parents=True, exist_ok=True)
    source = base / "heavydb"
    if not source.exists():
        run("git", "clone", "https://github.com/heavyai/heavydb.git", str(source))
    run("git", "fetch", "origin", refs["heavydb"], cwd=source)
    run("git", "checkout", "--detach", refs["heavydb"], cwd=source)
    run("git", "submodule", "update", "--init", "--recursive", cwd=source)
    tag = "firms-" + refs["heavydb"][:12]
    deps = "ghcr.io/heavyai/heavydb/core-build-ubuntu22.04-static-cuda12.9.2-x86_64:" + tag
    if subprocess.run(["docker", "image", "inspect", deps], capture_output=True).returncode:
        run("bash", "dev-tools/dev.sh", "build", "deps", "--distro=ubuntu22.04",
            "--cuda-version=12.9.2", "--tag=" + tag, "--nproc=8", cwd=source)
    run("bash", "dev-tools/dev.sh", "build", "all", "--docker", "--distro=ubuntu22.04",
        "--deps-image=" + deps, "--nproc=8", "--heavyiq-ref=" + refs["heavyiq"],
        "--immerse-ref=" + refs["immerse"], "--webserver-ref=" + refs["webserver"], cwd=source)
    # Upstream emits a latest alias for this locally built product. Never pull it.
    run("docker", "tag", "heavyai-ubuntu22.04-cuda-render-x86_64:latest", image)
    image_id = subprocess.check_output(["docker", "image", "inspect", image,
                                       "--format", "{{.Id}}"], text=True).strip()
    manifest.write_text(json.dumps({"refs": refs, "image": image, "image_id": image_id}, indent=2))


if __name__ == "__main__":
    main()
