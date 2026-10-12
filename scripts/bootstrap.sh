#!/bin/bash
set -euo pipefail
umask 077
exec >> /var/log/firms-runtime-install.log 2>&1
cd /opt/firms-heavyai
printf 'AWS_REGION=%s\nSECRET_ARN=%s\nARCHIVE_BUCKET=%s\n' "$AWS_REGION" "$SECRET_ARN" "$ARCHIVE_BUCKET" > /etc/firms-heavyai.env
python3 -m venv .venv
.venv/bin/pip install --disable-pip-version-check -r requirements.txt
if .venv/bin/python -c 'import json; c=json.load(open("config.json")); raise SystemExit(0 if c.get("build_from_source") and c.get("heavyiq_required") else 1)'; then
  bash scripts/install_model.sh
fi
.venv/bin/python scripts/bootstrap.py
cat > /etc/systemd/system/firms-collect.service <<'UNIT'
[Unit]
Description=Global NASA FIRMS collection
After=docker.service network-online.target
Wants=network-online.target
[Service]
Type=oneshot
EnvironmentFile=/etc/firms-heavyai.env
WorkingDirectory=/opt/firms-heavyai
ExecStart=/opt/firms-heavyai/.venv/bin/python -m app.collector
TimeoutStartSec=1800
UMask=0077
UNIT
cat > /etc/systemd/system/firms-collect.timer <<'UNIT'
[Unit]
Description=Collect FIRMS hourly
[Timer]
OnCalendar=hourly
RandomizedDelaySec=120
Persistent=true
[Install]
WantedBy=timers.target
UNIT
cat > /etc/systemd/system/firms-demo.service <<'UNIT'
[Unit]
Description=FIRMS activity demo
After=docker.service network-online.target
[Service]
EnvironmentFile=/etc/firms-heavyai.env
WorkingDirectory=/opt/firms-heavyai
ExecStart=/opt/firms-heavyai/.venv/bin/python -m app.server
Restart=on-failure
UMask=0077
[Install]
WantedBy=multi-user.target
UNIT
cat > /etc/systemd/system/firms-brief.service <<'UNIT'
[Unit]
Description=Fable evidence-linked FIRMS briefing
After=firms-collect.service
[Service]
Type=oneshot
EnvironmentFile=/etc/firms-heavyai.env
WorkingDirectory=/opt/firms-heavyai
ExecStart=/opt/firms-heavyai/.venv/bin/python -m app.brief
TimeoutStartSec=900
UMask=0077
UNIT
cat > /etc/systemd/system/firms-brief.timer <<'UNIT'
[Unit]
Description=Daily FIRMS briefing
[Timer]
OnCalendar=*-*-* 09:15:00 UTC
Persistent=true
[Install]
WantedBy=timers.target
UNIT
systemctl daemon-reload
systemctl start firms-collect.service
systemctl enable --now firms-collect.timer firms-demo.service
if .venv/bin/python -c 'import json; c=json.load(open("config.json")); raise SystemExit(0 if c.get("fable_model") else 1)'; then
  systemctl enable --now firms-brief.timer
fi
if .venv/bin/python -c 'import json; c=json.load(open("config.json")); raise SystemExit(0 if c.get("build_from_source") and c.get("heavyiq_required") else 1)'; then
  # Native IQ may install its own Python environment after HeavyDB is available.
  for attempt in {1..120}; do
    if curl --fail --silent http://127.0.0.1:6275/openapi.json >/dev/null; then break; fi
    sleep 5
  done
  .venv/bin/python scripts/evaluate_iq.py
fi
.venv/bin/python scripts/health.py
echo 'Runtime installed; collection and dashboard are ready. See evaluation report for model scope.'
