import configparser
import hashlib
import json
import os
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from app.collector import HeavyDB, literal, settings


def docker(*args):
    # Captured output can contain config/credentials; never print on failure.
    return subprocess.run(["docker", *args], check=True, capture_output=True, text=True).stdout


def main():
    config, secret = settings()
    storage = Path("/var/lib/heavyai")
    (storage / "import").mkdir(parents=True, exist_ok=True)
    image = config["heavyai_image"]
    archive = config.get("heavyai_image_archive")
    if archive:
        target = Path("/opt/firms-image.tar")
        if archive.startswith("s3://"):
            # S3 URI must be accessible to instance role. Prefer upload into this
            # deployment's bucket; arbitrary private cross-account buckets need a grant.
            subprocess.run(["aws", "s3", "cp", archive, str(target), "--only-show-errors"], check=True)
        else:
            subprocess.run(["curl", "--fail", "--location", "--proto", "=https", "--tlsv1.2",
                            archive, "-o", str(target)], check=True, capture_output=True)
        checksum = hashlib.sha256()
        with target.open("rb") as handle:
            for block in iter(lambda: handle.read(8 * 1024 * 1024), b""):
                checksum.update(block)
        digest = checksum.hexdigest()
        if digest != config["heavyai_image_sha256"]:
            raise ValueError("Image archive checksum mismatch")
        docker("load", "--input", str(target))
        target.unlink()
        docker("image", "inspect", image)
    else:
        # Authenticated registries require a preconfigured credential helper/login.
        docker("pull", image)
    if not (storage / "storage" / "catalogs").exists():
        docker("run", "--rm", "--gpus", "all", "--entrypoint", "/opt/heavyai/bin/initheavy",
               "-v", f"{storage}:{storage}", image, str(storage / "storage"))
    if secret.get("heavyai_license"):
        license_file = storage / "storage" / "heavyai.license"
        license_file.write_text(secret["heavyai_license"])
        license_file.chmod(0o600)
    iq_config = secret.get("heavyiq_config", "")
    if config["heavyiq_required"]:
        parser = configparser.ConfigParser(interpolation=None, strict=False)
        parser.read_string(iq_config)
        if parser.sections() != ["iq"] or parser.getboolean("iq", "disabled", fallback=True):
            raise ValueError("Native IQ config must contain exactly [iq] with disabled=false and release-supported settings")
    else:
        iq_config = "[iq]\ndisabled = true\n"
    text = '\n'.join([
        'port = 6274', 'http-port = 6278', 'calcite-port = 6279',
        'data = "/var/lib/heavyai/storage"',
        'allowed-import-paths = ["/var/lib/heavyai/import"]',
        '[web]', 'port = 6273', 'frontend = "/opt/heavyai/frontend"',
        'servers-json = "/var/lib/heavyai/servers.json"', iq_config])
    (storage / "heavy.conf").write_text(text)
    (storage / "heavy.conf").chmod(0o600)
    (storage / "servers.json").write_text(json.dumps([{
        "host": "localhost", "port": "6273", "protocol": "http", "database": "heavyai",
        "offline": True, "feature_flags": {"ui/enable_auto_dashboard_refresh": True}}]))
    existing = docker("ps", "-a", "--filter", "name=^heavyai$", "--format", "{{.Names}}")
    if existing.strip():
        docker("rm", "-f", "heavyai")
    docker("run", "-d", "--name", "heavyai", "--restart", "unless-stopped", "--gpus", "all",
           "--ipc=host", "-v", f"{storage}:{storage}",
           "-p", "127.0.0.1:6273:6273", "-p", "127.0.0.1:6274:6274",
           "-p", "127.0.0.1:6278:6278", image)
    db = None
    for _ in range(90):
        for password in (secret["heavyai_password"], "HyperInteractive"):
            try:
                db = HeavyDB(secret, password=password)
                db.execute("SELECT 1")
                if password == "HyperInteractive":
                    db.execute("ALTER USER admin (password=" + literal(secret["heavyai_password"]) + ")")
                break
            except Exception:
                if db is not None:
                    db.close()
                db = None
        if db:
            break
        time.sleep(10)
    if db is None:
        raise RuntimeError("HeavyDB startup failed; check container logs and image/license compatibility")
    try:
        tables = {str(row[0]) for row in db.execute("SHOW TABLES").fetchall()}
        for statement in (ROOT / "sql/schema.sql").read_text().split(";"):
            if statement.strip():
                if statement.strip().startswith("CREATE VIEW firms_observations") and "firms_observations" in tables:
                    continue
                db.execute(statement)
        users = [str(row[0]) for row in db.execute("SHOW ALL USER DETAILS").fetchall()]
        if "firms_reader" not in users:
            db.execute("CREATE USER firms_reader (password=" + literal(secret["reader_password"])
                       + ", is_super='false')")
        else:
            db.execute("ALTER USER firms_reader (password=" + literal(secret["reader_password"]) + ")")
        db.execute("GRANT ACCESS ON DATABASE heavyai TO firms_reader")
        db.execute("GRANT SELECT ON VIEW firms_observations TO firms_reader")
        db.execute("GRANT SELECT ON TABLE firms_collection_runs TO firms_reader")
    finally:
        db.close()
    print("HeavyDB initialized with a read-only demo user.")


if __name__ == "__main__":
    try:
        main()
    except Exception:
        # Vendor errors can echo configuration; log a safe summary only.
        print("Runtime setup failed; inspect HeavyDB container and systemd logs locally.", file=sys.stderr)
        sys.exit(1)
