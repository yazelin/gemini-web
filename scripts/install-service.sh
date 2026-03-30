#!/usr/bin/env bash
set -euo pipefail

SERVICE_NAME="gemini-web-api"
WORK_DIR="$(cd "$(dirname "$0")/.." && pwd)"
USER="${SUDO_USER:-$(whoami)}"
USER_HOME="$(eval echo ~${USER})"

sudo tee /etc/systemd/system/${SERVICE_NAME}.service > /dev/null <<EOF
[Unit]
Description=Gemini Web API
After=network.target

[Service]
Type=simple
User=${USER}
WorkingDirectory=${WORK_DIR}
ExecStart=${WORK_DIR}/.venv/bin/uvicorn src.main:app --host 0.0.0.0 --port 8070
Restart=on-failure
RestartSec=10
Environment=HEADLESS=true
Environment=HOME=${USER_HOME}
Environment=PLAYWRIGHT_BROWSERS_PATH=${USER_HOME}/.cache/ms-playwright

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable ${SERVICE_NAME}
sudo systemctl start ${SERVICE_NAME}
echo "✓ ${SERVICE_NAME} 服務已安裝並啟動"
