import configparser
import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from app.collector import HeavyDB, literal, settings
from scripts.iq_guidance import GuidanceStore


def docker(*args):
    # Captured output can contain config/credentials; never print on failure.
    return subprocess.run(["docker", *args], check=True, capture_output=True, text=True).stdout


def native_iq_mounts(storage):
    source = Path("/opt/heavyai-source/heavyiq/heavyiq/langchain/utils.py")
    text = source.read_text()
    anchor = "    model_local_path: str\n"
    if text.count(anchor) != 1:
        raise ValueError("HeavyIQ tokenizer initialization changed; review integration")
    text = text.replace(anchor,
        '    if model_name == "firms-nemotron":\n'
        '        from firms_tokenizer import RemoteTokenizer\n'
        '        return RemoteTokenizer(config.custom_llm_api_base)\n' + anchor)
    context_anchor = "llm.context_window - 306"
    if text.count(context_anchor) != 2:
        raise ValueError("HeavyIQ context budgeting changed; review integration")
    text = text.replace(context_anchor,
        '(getattr(llm, "context_window", None) or config.custom_llm_api_context_window) - 306')
    text = 'from firms_connector import ensure_connection_api\nensure_connection_api()\n' + text
    overrides = storage / "iq-overrides"
    overrides.mkdir(exist_ok=True)
    (overrides / "utils.py").write_text(text)
    shutil.copyfile(ROOT / "scripts/iq_tokenizer.py", overrides / "firms_tokenizer.py")
    shutil.copyfile(ROOT / "scripts/iq_connector.py", overrides / "firms_connector.py")
    mounts = ["-v", f"{overrides / 'utils.py'}:/opt/heavyai/heavyiq/heavyiq/langchain/utils.py:ro",
            "-v", f"{overrides / 'firms_tokenizer.py'}:/opt/heavyai/heavyiq/firms_tokenizer.py:ro",
            "-v", f"{overrides / 'firms_connector.py'}:/opt/heavyai/heavyiq/firms_connector.py:ro"]
    GuidanceStore(storage / 'iq-guidance.sqlite3').initialize('heavyai',
        [line.strip() for line in (ROOT / 'sql/heavyiq-guidance.txt').read_text().splitlines() if line.strip()])
    shutil.copyfile(ROOT / 'scripts/iq_guidance.py', overrides / 'firms_guidance.py')
    mounts += ['-v', f"{overrides / 'firms_guidance.py'}:/opt/heavyai/heavyiq/firms_guidance.py:ro"]
    source_root = source.parents[1]

    def overlay(relative, anchor, replacement, count=1, prefix=''):
        original = (source_root / relative).read_text()
        if original.count(anchor) != count:
            raise ValueError('HeavyIQ Guidance integration changed; review ' + relative)
        target = overrides / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(prefix + original.replace(anchor, replacement))
        mounts.extend(['-v', f'{target}:/opt/heavyai/heavyiq/heavyiq/{relative}:ro'])

    overlay('api/__init__.py', '        include_rag_routers(app)\n',
        '        include_rag_routers(app)\n'
        '    else:\n'
        '        from firms_guidance import register\n'
        '        register(app)\n')
    for chain in ('sql_chain', 'table_chain', 'auto_sql_chain'):
        overlay(f'lcel/chains/heavydb/{chain}.py',
            '(lambda x: not (CONFIG.enable_rag), lambda x: relevant_info_default_values)',
            '(lambda x: not (CONFIG.enable_rag), RunnableLambda(firms_relevant_info))',
            prefix='from firms_guidance import relevant_info as firms_relevant_info\n')
    # Upstream's default OpenAI templates omit relevant_info entirely.
    overlay('lcel/prompts/openai.py', '{table_info}\n', '{table_info}\n{relevant_info}\n', count=3)
    # SQL repair must unpack the guidance dictionary, just like initial SQL.
    sql_path = overrides / 'lcel/chains/heavydb/sql_chain.py'
    sql_text = sql_path.read_text()
    anchor = 'RunnablePassthrough.assign(relevant_info=relevant_info_lambda)'
    if sql_text.count(anchor) != 1:
        raise ValueError('HeavyIQ SQL repair Guidance integration changed')
    sql_path.write_text(sql_text.replace(anchor,
        'RunnablePassthrough.assign(relevant_info_sql_dict=relevant_info_lambda)'
        ' | RunnablePassthrough.assign('
        'snippet_ids=lambda x: x["relevant_info_sql_dict"]["snippet_ids"], '
        'relevant_info=lambda x: x["relevant_info_sql_dict"]["relevant_info"])'))
    overlay('lcel/chains/heavydb/answer_chain.py',
        '    return summarize\n',
        '    return summarize + "\\n" + inputs["guidance_dict"]["relevant_info"]\n',
        prefix='from firms_guidance import relevant_info as firms_relevant_info\n')
    answer_path = overrides / 'lcel/chains/heavydb/answer_chain.py'
    answer_text = answer_path.read_text()
    anchor = '    derive_column_metadata_step | sql_to_answer_prompt_rbl'
    if answer_text.count(anchor) != 1:
        raise ValueError('HeavyIQ answer Guidance integration changed')
    answer_path.write_text(answer_text.replace(anchor,
        '    RunnablePassthrough.assign(guidance_dict=RunnableLambda(firms_relevant_info))\n'
        '    | RunnablePassthrough.assign(snippet_ids=lambda x: x["guidance_dict"]["snippet_ids"])\n'
        '    | derive_column_metadata_step | sql_to_answer_prompt_rbl'))
    return mounts


def main():
    config, secret = settings()
    storage = Path("/var/lib/heavyai")
    (storage / "import").mkdir(parents=True, exist_ok=True)
    (storage / "storage").mkdir(parents=True, exist_ok=True)
    image = config["heavyai_image"]
    archive = config.get("heavyai_image_archive")
    if config.get("build_from_source"):
        subprocess.run([sys.executable, str(ROOT / "scripts/build_source.py")], check=True)
    elif archive:
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
    runtime_mounts = []
    if config.get("build_from_source"):
        if config["heavyiq_required"]:
            runtime_mounts += native_iq_mounts(storage)
        geos = storage / "libgeos"
        geos.mkdir(exist_ok=True)
        artifacts = list(Path("/opt/heavyai-source/heavydb/build/components").glob("heavydb-libgeos-*.tar.xz"))
        if len(artifacts) != 1:
            raise ValueError("Expected one source-built GEOS library artifact")
        subprocess.run(["tar", "-xJf", str(artifacts[0]), "-C", str(geos)], check=True)
        driver_manifests = Path("/usr/share/vulkan/icd.d")
        if driver_manifests.exists():
            headless_manifests = storage / "vulkan"
            headless_manifests.mkdir(exist_ok=True)
            manifest = json.loads((driver_manifests / "nvidia_icd.json").read_text())
            manifest["ICD"]["library_path"] = "libEGL_nvidia.so.0"
            (headless_manifests / "nvidia_icd.json").write_text(json.dumps(manifest))
            runtime_mounts += ["-v", f"{headless_manifests}:{driver_manifests}:ro"]
            runtime_mounts += ["-e", "NVIDIA_DRIVER_CAPABILITIES=compute,utility,graphics,display"]
            for device in ("/dev/dri", "/dev/nvidia-modeset"):
                if Path(device).exists():
                    runtime_mounts += ["--device", device]
    print("[firms-progress] engine_initialize", flush=True)
    if not (storage / "storage" / "catalogs").exists():
        docker("run", "--rm", "--gpus", "all", "--entrypoint", "/opt/heavyai/bin/initheavy",
               "-v", f"{storage}:{storage}", image, str(storage / "storage"))
    if secret.get("heavyai_license"):
        license_file = storage / "storage" / "heavyai.license"
        license_file.write_text(secret["heavyai_license"])
        license_file.chmod(0o600)
    iq_config = secret.get("heavyiq_config", "")
    if config.get("build_from_source"):
        iq_config = ('[iq]\ndisabled = false\ncustom_llm_type = "API_VLLM"\n'
                     'custom_llm_api_base = "http://host.docker.internal:8100/v1"\n'
                     'custom_llm_api_context_window = 6000\n'
                     'custom_llm_api_vllm_beam_width = 1\n'
                     'enable_logprobs = false\nenable_rag = false\n')
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
        'servers-json = "/var/lib/heavyai/servers.json"',
        'mapbox-token = ' + json.dumps(secret.get("mapbox_token", "")),
        'google-api-key = ' + json.dumps(secret.get("google_maps_api_key", "")), iq_config])
    (storage / "heavy.conf").write_text(text)
    (storage / "heavy.conf").chmod(0o600)
    (storage / "servers.json").write_text(json.dumps([{
        "host": config.get("immerse_host", "localhost"), "port": "6273", "protocol": "http", "database": "heavyai",
        "offline": True, "feature_flags": {"ui/enable_auto_dashboard_refresh": True}}]))
    existing = docker("ps", "-a", "--filter", "name=^heavyai$", "--format", "{{.Names}}")
    if existing.strip():
        docker("rm", "-f", "heavyai")
    docker("run", "-d", "--name", "heavyai", "--restart", "unless-stopped", "--gpus", "all",
           "--add-host", "host.docker.internal:host-gateway",
           "--ipc=host", "--ulimit", "memlock=-1:-1", "-v", f"{storage}:{storage}", *runtime_mounts,
           "-p", "127.0.0.1:6273:6273", "-p", "127.0.0.1:6274:6274",
           "-p", "127.0.0.1:6278:6278", "-p", "127.0.0.1:6275:6275", image)
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
        if "firms_published_windows" in tables:
            declaration = str(db.execute("SHOW CREATE TABLE firms_published_windows").fetchone()[0])
            if "publication_order" not in declaration:
                db.execute("ALTER TABLE firms_published_windows ADD COLUMN publication_order BIGINT")
                db.execute("UPDATE firms_published_windows SET publication_order="
                           "CAST(SUBSTRING(batch_id, 1, 19) AS BIGINT)")
        db.execute("DROP VIEW IF EXISTS firms_observations")
        for statement in (ROOT / "sql/schema.sql").read_text().split(";"):
            if statement.strip():
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
