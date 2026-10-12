#!/usr/bin/env python3
"""AWS CLI + Python standard library deployment; no local SDK or Terraform needed."""
import argparse
import getpass
import hashlib
import json
import os
import re
import secrets
import shlex
import subprocess
import sys
import time
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from infra.template import template


def aws(args, profile, region, json_output=True):
    command = ["aws", "--profile", profile, "--region", region, "--no-cli-pager", *args]
    if json_output:
        command += ["--output", "json"]
    result = subprocess.run(command, check=True, text=True, capture_output=json_output)
    return json.loads(result.stdout) if json_output and result.stdout.strip() else result.stdout


def validate_config(config):
    if config.get("instance_type") != "g5.4xlarge":
        raise ValueError("This deployment is sized for g5.4xlarge")
    if not re.fullmatch(r"[a-zA-Z][a-zA-Z0-9-]{0,80}", config.get("stack_name", "")):
        raise ValueError("Invalid stack name")
    if not re.fullmatch(r"[a-z]{2}(?:-[a-z]+)+-\d", config.get("region", "")):
        raise ValueError("Invalid AWS region")
    if not 1 <= config.get("lookback_days", 0) <= 5:
        raise ValueError("lookback_days must be 1..5")
    if not 3 <= config.get("retain_days", 0) <= 365:
        raise ValueError("retain_days must be 3..365")
    if not 100 <= config.get("disk_gb", 0) <= 2000:
        raise ValueError("disk_gb must be 100..2000")
    if config.get("existing_vpc_id") or config.get("existing_subnet_id"):
        for field, pattern in (("existing_vpc_id", r"vpc-[0-9a-f]{8}(?:[0-9a-f]{9})?"),
                               ("existing_subnet_id", r"subnet-[0-9a-f]{8}(?:[0-9a-f]{9})?")):
            if not re.fullmatch(pattern, config.get(field, "")):
                raise ValueError("Provide both existing VPC and public subnet IDs")
    from app.collector import SOURCES
    sources = config.get("sources", [])
    if not sources or len(sources) != len(set(sources)) or not set(sources) <= set(SOURCES):
        raise ValueError("Select unique supported VIIRS sources")
    image = config.get("heavyai_image", "")
    if not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9._/@:-]+", image) or image.endswith(":latest"):
        raise ValueError("Provide a versioned HEAVY.AI image reference (not latest)")
    if config.get("build_from_source"):
        for field in ("heavydb_ref", "heavyiq_ref"):
            if not re.fullmatch(r"[0-9a-f]{40}", config.get(field, "")):
                raise ValueError("Source builds require full commit hashes: " + field)
    archive = config.get("heavyai_image_archive", "")
    if archive and not archive.startswith(("s3://", "https://")):
        raise ValueError("Image archive must be s3:// or https://")
    if archive and not re.fullmatch(r"[0-9a-f]{64}", config.get("heavyai_image_sha256", "")):
        raise ValueError("Image archives require their SHA-256 checksum")


def outputs(args, config):
    stack = aws(["cloudformation", "describe-stacks", "--stack-name", config["stack_name"]],
                args.profile, config["region"])["Stacks"][0]
    return {x["OutputKey"]: x["OutputValue"] for x in stack["Outputs"]}


def package(config):
    target = ROOT / ".deploy"
    target.mkdir(exist_ok=True)
    path = target / "runtime.zip"
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
        for directory in ("app", "sql", "scripts"):
            for file in sorted((ROOT / directory).rglob("*")):
                if file.is_file() and "__pycache__" not in file.parts and file.suffix != ".pyc":
                    archive.write(file, file.relative_to(ROOT))
        archive.write(ROOT / "requirements.txt", "requirements.txt")
        archive.writestr("config.json", json.dumps(config))
    return path, hashlib.sha256(path.read_bytes()).hexdigest()


def configure(args):
    config_path = ROOT / "config.json"
    config = json.loads((config_path if config_path.exists() else ROOT / "config.example.json").read_text())
    if args.image:
        config["heavyai_image"] = args.image
        config["build_from_source"] = False
    if args.vpc_id or args.subnet_id:
        config["existing_vpc_id"] = args.vpc_id or ""
        config["existing_subnet_id"] = args.subnet_id or ""
    if args.archive:
        config["heavyai_image_archive"] = args.archive
        config["heavyai_image_sha256"] = args.sha256 or ""
    config["heavyiq_required"] = not args.without_heavyiq
    secret_path = ROOT / ".deploy" / "secrets.json"
    secret_path.parent.mkdir(exist_ok=True)
    secret = json.loads(secret_path.read_text()) if secret_path.exists() else {}
    map_key = os.environ.get("FIRMS_MAP_KEY") or secret.get("firms_map_key")
    if not map_key:
        map_key = getpass.getpass("NASA FIRMS MAP_KEY (hidden): ").strip()
    if not re.fullmatch(r"[a-zA-Z0-9]{16,128}", map_key):
        raise ValueError("Invalid MAP_KEY format")
    secret["firms_map_key"] = map_key
    secret.setdefault("heavyai_password", secrets.token_urlsafe(32))
    secret.setdefault("reader_password", secrets.token_urlsafe(32))
    if args.license:
        secret["heavyai_license"] = Path(args.license).read_text()
    if args.iq_config:
        secret["heavyiq_config"] = Path(args.iq_config).read_text()
    if os.environ.get("ANTHROPIC_API_KEY"):
        secret["anthropic_api_key"] = os.environ["ANTHROPIC_API_KEY"]
    if args.fable_model:
        config["fable_model"] = args.fable_model
    validate_config(config)
    if config["heavyiq_required"] and not config.get("build_from_source") and not secret.get("heavyiq_config"):
        raise ValueError("Supply --iq-config with your release-supported native HeavyIQ configuration; "
                         "or explicitly use --without-heavyiq for a collection-only deployment")
    if config.get("fable_model") and not secret.get("anthropic_api_key"):
        raise ValueError("Set ANTHROPIC_API_KEY before enabling a Fable model")
    config_path.write_text(json.dumps(config, indent=2) + "\n")
    descriptor = os.open(secret_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    os.fchmod(descriptor, 0o600)
    with os.fdopen(descriptor, "w") as handle:
        json.dump(secret, handle)
    print("Configuration saved. Credentials stay in ignored .deploy/secrets.json (mode 0600).")


def send(args, config, instance, command):
    return aws(["ssm", "send-command", "--instance-ids", instance,
                "--document-name", "AWS-RunShellScript", "--timeout-seconds", "3600",
                "--parameters", json.dumps({"commands": ["bash -lc " + shlex.quote(command)], "executionTimeout": ["28800"]})],
               args.profile, config["region"])["Command"]["CommandId"]


def wait_command(args, config, instance, command_id):
    for _ in range(2940):
        try:
            result = aws(["ssm", "get-command-invocation", "--command-id", command_id,
                          "--instance-id", instance], args.profile, config["region"])
        except subprocess.CalledProcessError:
            time.sleep(10)
            continue
        state = result["Status"]
        if state == "Success":
            print(result.get("StandardOutputContent", ""))
            return
        if state in {"Failed", "TimedOut", "Cancelled", "Cancelling"}:
            raise RuntimeError("Remote bootstrap failed. Inspect SSM command " + command_id
                               + " and /var/log/firms-runtime-install.log on the instance.")
        print("Bootstrap: " + state, flush=True)
        time.sleep(10)
    raise TimeoutError("Bootstrap still running; inspect SSM command " + command_id)


def deploy(args, config):
    validate_config(config)
    secret_path = ROOT / ".deploy" / "secrets.json"
    if not secret_path.exists():
        raise ValueError("Run configure first")
    secret = json.loads(secret_path.read_text())
    if config["heavyiq_required"] and not config.get("build_from_source") and not secret.get("heavyiq_config"):
        raise ValueError("HeavyIQ configuration is required")
    identity = aws(["sts", "get-caller-identity"], args.profile, config["region"])
    print(f"Account {identity['Account']}; region {config['region']}; g5.4xlarge; {config['disk_gb']} GB gp3.")
    images = aws(["ec2", "describe-images", "--owners", "099720109477", "--filters",
        "Name=name,Values=ubuntu/images/hvm-ssd/ubuntu-jammy-22.04-amd64-server-*",
        "Name=state,Values=available"], args.profile, config["region"])["Images"]
    if not images:
        raise ValueError("No Canonical Ubuntu 22.04 image available in selected region")
    ami = max(images, key=lambda image: image["CreationDate"])["ImageId"]
    path, digest = package(config)
    template_path = ROOT / ".deploy" / "stack.json"
    template_path.write_text(json.dumps(template(existing_network=bool(config.get("existing_vpc_id"))), indent=2))
    aws(["cloudformation", "validate-template", "--template-body", "file://" + str(template_path)],
        args.profile, config["region"])
    if args.plan:
        print("Template validated; package prepared. No AWS resources created.")
        return
    parameters = [f"AmiId={ami}", "InstanceType=g5.4xlarge", f"DiskGb={config['disk_gb']}"]
    if config.get("existing_vpc_id"):
        subnet = aws(["ec2", "describe-subnets", "--subnet-ids", config["existing_subnet_id"]],
                     args.profile, config["region"])["Subnets"][0]
        if subnet["VpcId"] != config["existing_vpc_id"]:
            raise ValueError("Existing subnet does not belong to the selected VPC")
        parameters += ["ExistingVpcId=" + config["existing_vpc_id"],
                       "ExistingSubnetId=" + config["existing_subnet_id"]]
    aws(["cloudformation", "deploy", "--stack-name", config["stack_name"],
         "--template-file", str(template_path), "--capabilities", "CAPABILITY_IAM",
         "--no-fail-on-empty-changeset", "--parameter-overrides", *parameters],
        args.profile, config["region"], json_output=False)
    out = outputs(args, config)
    aws(["secretsmanager", "put-secret-value", "--secret-id", out["SecretArn"],
         "--secret-string", "file://" + str(secret_path)], args.profile, config["region"])
    key = "deployment/" + digest + ".zip"
    aws(["s3", "cp", str(path), f"s3://{out['ArchiveBucket']}/{key}", "--only-show-errors"],
        args.profile, config["region"], json_output=False)
    print("Waiting for the SSM agent and GPU setup (usually 10–20 minutes).", flush=True)
    instance = out["InstanceId"]
    for _ in range(180):
        managed = aws(["ssm", "describe-instance-information", "--filters",
                       f"Key=InstanceIds,Values={instance}"], args.profile, config["region"])
        online = any(x["PingStatus"] == "Online" for x in managed["InstanceInformationList"])
        if online:
            probe = send(args, config, instance, "test -f /var/lib/firms-base-ready && nvidia-smi >/dev/null")
            time.sleep(5)
            try:
                result = aws(["ssm", "get-command-invocation", "--command-id", probe,
                              "--instance-id", instance], args.profile, config["region"])
            except subprocess.CalledProcessError:
                time.sleep(10)
                continue
            if result["Status"] == "Success":
                break
        time.sleep(10)
    else:
        raise TimeoutError("GPU setup incomplete; inspect cloud-init and /var/log/firms-base-install.log")
    command = "\n".join([
        "set -euo pipefail", "install -d -m 700 /opt/firms-heavyai",
        f"aws s3 cp {shlex.quote('s3://' + out['ArchiveBucket'] + '/' + key)} /opt/firms-runtime.zip --region {shlex.quote(config['region'])} --only-show-errors",
        f"echo '{digest}  /opt/firms-runtime.zip' | sha256sum --check --status",
        "unzip -o -q /opt/firms-runtime.zip -d /opt/firms-heavyai",
        "export " + " ".join(k + "=" + shlex.quote(v) for k, v in {
            "AWS_REGION": config["region"], "SECRET_ARN": out["SecretArn"],
            "ARCHIVE_BUCKET": out["ArchiveBucket"]}.items()),
        "bash /opt/firms-heavyai/scripts/bootstrap.sh"])
    command_id = send(args, config, instance, command)
    print("SSM bootstrap command: " + command_id, flush=True)
    wait_command(args, config, instance, command_id)
    print("Bootstrap complete. Run status for evaluation/freshness, connect for the map, or connect --port 6273 for Immerse.")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", default="heavyai")
    commands = parser.add_subparsers(dest="command", required=True)
    setup = commands.add_parser("configure")
    setup.add_argument("--image")
    setup.add_argument("--vpc-id")
    setup.add_argument("--subnet-id")
    setup.add_argument("--archive")
    setup.add_argument("--sha256")
    setup.add_argument("--license")
    setup.add_argument("--iq-config")
    setup.add_argument("--without-heavyiq", action="store_true")
    setup.add_argument("--fable-model")
    launch = commands.add_parser("deploy")
    launch.add_argument("--plan", action="store_true")
    commands.add_parser("status")
    connect = commands.add_parser("connect")
    connect.add_argument("--port", type=int, choices=[8080, 6273], default=8080)
    commands.add_parser("destroy")
    args = parser.parse_args()
    if args.command == "configure":
        configure(args)
        return
    config = json.loads((ROOT / "config.json").read_text())
    if args.command == "deploy":
        deploy(args, config)
    elif args.command == "connect":
        out = outputs(args, config)
        command = ["aws", "--profile", args.profile, "--region", config["region"],
             "ssm", "start-session", "--target", out["InstanceId"],
             "--document-name", "AWS-StartPortForwardingSession", "--parameters",
             json.dumps({"portNumber": [str(args.port)], "localPortNumber": [str(args.port)]})]
        print(f"Open http://localhost:{args.port}; requires the AWS Session Manager plugin.")
        subprocess.run(command, check=True)
    elif args.command == "status":
        out = outputs(args, config)
        print(json.dumps(out, indent=2))
        command_id = send(args, config, out["InstanceId"],
                          "set -a; . /etc/firms-heavyai.env; set +a; "
                          "/opt/firms-heavyai/.venv/bin/python /opt/firms-heavyai/scripts/health.py")
        wait_command(args, config, out["InstanceId"], command_id)
    elif args.command == "destroy":
        aws(["cloudformation", "delete-stack", "--stack-name", config["stack_name"]],
            args.profile, config["region"])
        aws(["cloudformation", "wait", "stack-delete-complete", "--stack-name", config["stack_name"]],
            args.profile, config["region"], json_output=False)
        print("Compute/network removed. S3 archive and secret retained; storage charges continue.")


if __name__ == "__main__":
    try:
        main()
    except subprocess.CalledProcessError as exc:
        # Do not dump commands or captured output: runtime configuration may contain tokens.
        print(f"AWS operation failed (exit {exc.returncode}). Inspect CloudFormation/SSM events.", file=sys.stderr)
        sys.exit(1)
    except (ValueError, RuntimeError, TimeoutError, FileNotFoundError) as exc:
        print(str(exc), file=sys.stderr)
        sys.exit(1)
