#!/usr/bin/env bash
set -euo pipefail
BASE=/opt/kts-vps-bot
SRC="$(cd "$(dirname "$0")" && pwd)"
command -v docker >/dev/null || { echo "Docker is required on the host."; exit 1; }
mkdir -p "$BASE/project" "$BASE/data"
cp -a "$SRC/." "$BASE/project/"
cd "$BASE/project"
if [[ ! -f .env ]]; then cp .env.example .env; echo "Edit $BASE/project/.env then run this installer again."; exit 0; fi
set -a; source .env; set +a
docker build -t "${DOCKER_IMAGE:-kts-ubuntu-systemd:24.04}" ./image
python3 -m venv .venv
. .venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt
cat >/etc/systemd/system/kts-vps-bot.service <<EOF
[Unit]
Description=KTS HOST VPS Discord Bot
After=docker.service network-online.target
Wants=network-online.target
Requires=docker.service

[Service]
Type=simple
WorkingDirectory=$BASE/project
EnvironmentFile=$BASE/project/.env
ExecStart=$BASE/project/.venv/bin/python $BASE/project/bot.py
Restart=always
RestartSec=5
User=root

[Install]
WantedBy=multi-user.target
EOF
systemctl daemon-reload
systemctl enable --now kts-vps-bot
echo "Installed. Logs: journalctl -u kts-vps-bot -f"
