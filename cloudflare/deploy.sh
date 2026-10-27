#!/bin/sh
# Puts Monitor live on Cloudflare (free): https://monitor.<you>.workers.dev
# Safe to run again after changes: it reuses the database and keys it created the first time.
set -e
cd "$(dirname "$0")"

[ -f wrangler.toml ] || cp wrangler.example.toml wrangler.toml   # your settings; not committed
npm install --silent
npx wrangler whoami 2>/dev/null | grep -q "You are logged in" || npx wrangler login

# 1. Database (once)
if grep -q 'REPLACE_WITH_DATABASE_ID' wrangler.toml; then
  ID=$(npx wrangler d1 create monitor 2>&1 | grep -oE '[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}' | head -1)
  [ -n "$ID" ] || { echo "Could not create the D1 database. Run: npx wrangler d1 create monitor"; exit 1; }
  sed -i.bak "s/REPLACE_WITH_DATABASE_ID/$ID/" wrangler.toml && rm wrangler.toml.bak
fi
npx wrangler d1 execute monitor --remote --file schema.sql -y

# 2. Keys (once). Kept in .secrets so you can look them up later.
NEW_KEYS=
if [ ! -f .secrets ]; then
  rnd() { node -e "console.log(require('crypto').randomBytes(16).toString('hex'))"; }
  printf 'MONITOR_TOKEN=%s\nMONITOR_KEY=%s\n' "$(rnd)" "$(rnd)" > .secrets
  chmod 600 .secrets
  NEW_KEYS=1
fi
val() { grep "^$1=" .secrets | cut -d= -f2; }

# 3. Upload
mkdir -p public && cp ../dashboard.html public/index.html
npx wrangler deploy | tee .deploy.log
URL=$(grep -oE 'https://[a-z0-9.-]+\.workers\.dev' .deploy.log | head -1); rm -f .deploy.log
if [ -n "$NEW_KEYS" ]; then
  printf %s "$(val MONITOR_TOKEN)" | npx wrangler secret put MONITOR_TOKEN
  printf %s "$(val MONITOR_KEY)" | npx wrangler secret put MONITOR_KEY
fi

cat <<EOF

✅ Live.

  Dashboard : $URL/?token=$(val MONITOR_TOKEN)
  DSN       : $(echo "$URL" | sed "s#https://#https://$(val MONITOR_KEY)@#")/<project id>
  Phone push: run ./telegram.sh once

Your keys are saved in $(pwd)/.secrets
EOF
