"""Build the pinned upstream GPU product, including native HeavyIQ."""
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
COMPONENTS = {
    "immerse": "070acbed6dc966dce8aa7a5cd213b22fdbc900cb",
    "webserver": "54b47c7aee62b122338eae523e6a809b49dd9069",
    "pyheavydb": "673c7433bb0a7afb3b18a2e454ed652b3cd1056e",
}
DEPENDENCY_OVERRIDES = {"chromadb": "1.5.9"}


def patch_iq_requirements(path):
    text = path.read_text()
    old, new = "chromadb==1.5.10.dev239", "chromadb==1.5.9"
    if old not in text and new not in text:
        raise ValueError("HeavyIQ dependency declaration changed; review override before building")
    text = text.replace(old, new)
    # All other declared packages exist on production PyPI; connector is built locally.
    text = text.replace("--extra-index-url https://test.pypi.org/simple/\n", "")
    path.write_text(text)


def run(*args, cwd=None):
    subprocess.run(args, cwd=cwd, check=True)


def main():
    config = json.loads((ROOT / "config.json").read_text())
    image = config["heavyai_image"]
    manifest = Path("/opt/heavyai-source/manifest.json")
    refs = dict(COMPONENTS, heavydb=config["heavydb_ref"], heavyiq=config["heavyiq_ref"])
    prior = json.loads(manifest.read_text()) if manifest.exists() else {}
    if prior.get("refs") == refs and prior.get("dependency_overrides") == DEPENDENCY_OVERRIDES:
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
        print("[firms-progress] heavyai_dependencies", flush=True)
        run("bash", "dev-tools/dev.sh", "build", "deps", "--distro=ubuntu22.04",
            "--cuda-version=12.9.2", "--tag=" + tag, "--nproc=8", cwd=source)
    print("[firms-progress] heavyai_components", flush=True)
    iq = base / "heavyiq"
    if not iq.exists():
        run("git", "clone", "https://github.com/heavyai/heavyiq.git", str(iq))
    run("git", "fetch", "origin", refs["heavyiq"], cwd=iq)
    run("git", "checkout", "--detach", refs["heavyiq"], cwd=iq)
    patch_iq_requirements(iq / "requirements.txt")
    run("bash", "dev-tools/dev.sh", "build", "all", "--docker", "--distro=ubuntu22.04",
        "--deps-image=" + deps, "--nproc=8", "--heavyiq-ref=" + refs["heavyiq"],
        "--pyheavydb-ref=" + refs["pyheavydb"],
        "--immerse-ref=" + refs["immerse"], "--webserver-ref=" + refs["webserver"], cwd=source)
    # Upstream emits a latest alias for this locally built product. Never pull it.
    run("docker", "tag", "heavyai-ubuntu22.04-cuda-render-x86_64:latest", image)
    image_id = subprocess.check_output(["docker", "image", "inspect", image,
                                       "--format", "{{.Id}}"], text=True).strip()
    manifest.write_text(json.dumps({"refs": refs, "image": image, "image_id": image_id,
                                    "dependency_overrides": DEPENDENCY_OVERRIDES}, indent=2))


if __name__ == "__main__":
    main()
