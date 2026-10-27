#!/bin/sh
set -e
cd "$(dirname "$0")"
python3 -m venv .venv
.venv/bin/pip install -q -r requirements.txt
if [ ! -f .env ]; then
  rnd() { python3 -c "import secrets; print(secrets.token_hex(16))"; }
  cat > .env <<EOF
# Dashboard password. Keep secret.
MONITOR_TOKEN=$(rnd)
# Public key used in the apps' DSN. It only lets apps SEND errors (it ends up in your JS bundle, that's fine).
MONITOR_KEY=$(rnd)
MONITOR_PORT=9000
# 127.0.0.1 = only reachable through Caddy / a tunnel (recommended). 0.0.0.0 = whole network.
MONITOR_HOST=127.0.0.1
# Names for project ids used in DSNs
MONITOR_PROJECTS=1=web,2=mobile,3=backend
# Public URL of this server (used for links in alert emails), e.g. https://monitor.example.com
PUBLIC_URL=

# Alert emails. Gmail: turn on 2-Step Verification, create an App Password at
# https://myaccount.google.com/apppasswords and paste it as SMTP_PASSWORD.
ALERT_EMAIL=
SMTP_HOST=smtp.gmail.com
SMTP_PORT=465
SMTP_USER=
SMTP_PASSWORD=
EOF
  chmod 600 .env
  echo "Created .env"
fi
