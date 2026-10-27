#!/bin/sh
# Adds Monitor error tracking to a Next.js App Router project (Next 13.4+, TypeScript or JavaScript).
# Run it from the project folder (where package.json is). Safe to run twice: existing files are never overwritten.
set -e
SDK="$(cd "$(dirname "$0")" && pwd)"

[ -f package.json ] || { echo "❌ Run this inside your Next.js project folder (where package.json is)."; exit 1; }
if [ -d src/app ]; then BASE=src; elif [ -d app ]; then BASE=.; else
  echo "❌ No app/ or src/app/ folder found. This script is for the App Router."; exit 1; fi
LAYOUT=$(ls "$BASE"/app/layout.tsx "$BASE"/app/layout.jsx "$BASE"/app/layout.js 2>/dev/null | head -1)
[ -n "$LAYOUT" ] || { echo "❌ $BASE/app/layout file not found."; exit 1; }
if [ -f tsconfig.json ]; then TS=1; X=ts; XX=tsx; else TS=; X=js; XX=jsx; fi
NEXT=$(node -p "require('./node_modules/next/package.json').version" 2>/dev/null || echo 15)
NEXT_MAJOR=${NEXT%%.*}
echo "Next.js $NEXT, $([ -n "$TS" ] && echo TypeScript || echo JavaScript), app folder: $BASE/app"
echo

# write_new FILE: write stdin to FILE unless FILE (in any js/ts flavour) already exists
write_new() {
  stem=${1%.*}
  for f in "$stem.ts" "$stem.tsx" "$stem.js" "$stem.jsx"; do
    if [ -f "$f" ]; then
      cat > /dev/null
      if grep -q "lib/monitor" "$f"; then echo "✅ $f (already set up)"; else echo "⚠️  $f already exists: $2"; fi
      return
    fi
  done
  cat > "$1"
  echo "✅ $1"
}

# 1. library
mkdir -p "$BASE/lib" "$BASE/components"
cp "$SDK/index.js" "$BASE/lib/monitor.js"
if [ -n "$TS" ]; then cp "$SDK/index.d.ts" "$BASE/lib/monitor.d.ts"; fi
echo "✅ $BASE/lib/monitor.js"

# 2. server errors
if [ -n "$TS" ]; then NN='!'; else NN=; fi  # TypeScript non-null assertion
if [ "$NEXT_MAJOR" -ge 15 ]; then
  write_new "$BASE/instrumentation.$X" "add init() and  export { onRequestError } from \"./lib/monitor\";  to it (see README)." <<EOF
import { init } from "./lib/monitor";

export function register() {
  init({ dsn: process.env.MONITOR_DSN$NN, environment: process.env.NODE_ENV });
}

export { onRequestError } from "./lib/monitor";
EOF
else
  write_new "$BASE/instrumentation.$X" "add the Monitor register() code to it (see README)." <<EOF
export async function register() {
  if (process.env.NEXT_RUNTIME !== "nodejs") return;
  const { init, captureException } = await import("./lib/monitor");
  init({ dsn: process.env.MONITOR_DSN$NN, environment: process.env.NODE_ENV });

  // Next.js 14 has no onRequestError hook, but it logs every server error
  // (pages, API routes, server actions) with console.error, so report from there.
  const log = console.error;
  console.error = (...args) => {
    const err = args.find((a) => a instanceof Error);
    if (err) captureException(err);
    log(...args);
  };
}
EOF
  # Next 14 only runs instrumentation with this flag
  CONF=$(ls next.config.mjs next.config.js next.config.ts 2>/dev/null | head -1)
  if [ -n "$CONF" ] && grep -q "instrumentationHook" "$CONF"; then
    echo "✅ $CONF (already set up)"
  elif [ -n "$CONF" ] && ! grep -q "experimental" "$CONF" && grep -qE "(const nextConfig|module\.exports) *(: *[A-Za-z]+ *)?= *\{" "$CONF"; then
    perl -0777 -i -pe 's/((?:const nextConfig|module\.exports)\s*(?::\s*\w+\s*)?=\s*\{)/$1\n  experimental: { instrumentationHook: true },/' "$CONF"
    echo "✅ $CONF (turned on instrumentationHook)"
  else
    echo "⚠️  Add  experimental: { instrumentationHook: true }  to your next.config by hand."
  fi
fi

# 3. browser errors
write_new "$BASE/components/MonitorInit.$XX" "" <<EOF
"use client";
import { init } from "../lib/monitor";

if (typeof window !== "undefined") {
  init({ dsn: process.env.NEXT_PUBLIC_MONITOR_DSN$NN, environment: process.env.NODE_ENV });
}

export default function MonitorInit() {
  return null;
}
EOF

# 4. layout: import + <MonitorInit /> right after <body ...>
if grep -q "MonitorInit" "$LAYOUT"; then
  echo "✅ $LAYOUT (already set up)"
else
  cp "$LAYOUT" "$LAYOUT.bak"
  if perl -0777 -i -pe '
      s/(<body\b[^>]*>)/$1\n        <MonitorInit \/>/ or die "no <body> tag\n";
      s/^((?:\s*["\x27]use [a-z]+["\x27];?\s*\n)?)/$1import MonitorInit from "..\/components\/MonitorInit";\n/;
    ' "$LAYOUT"; then
    rm "$LAYOUT.bak"; echo "✅ $LAYOUT (added <MonitorInit />)"
  else
    mv "$LAYOUT.bak" "$LAYOUT"
    echo "⚠️  Couldn't edit $LAYOUT: add  import MonitorInit from \"../components/MonitorInit\";  and  <MonitorInit />  inside <body> by hand."
  fi
fi

# 5 + 6. page / layout crashes. Errors with a digest came from the server and were already reported there.
if [ -n "$TS" ]; then ERRT=': { error: Error & { digest?: string }; reset: () => void }'; GERRT=': { error: Error & { digest?: string } }'; else ERRT=; GERRT=; fi
REPORT='if (!error.digest) captureException(error);'
HINT="add  useEffect(() => { $REPORT }, [error]);  to it (import captureException from \"../lib/monitor\")."
write_new "$BASE/app/error.$XX" "$HINT" <<EOF
"use client";
import { useEffect } from "react";
import { captureException } from "../lib/monitor";

export default function Error({ error, reset }$ERRT) {
  useEffect(() => { $REPORT }, [error]);
  return (
    <div style={{ padding: 24 }}>
      <h2>Something went wrong.</h2>
      <button onClick={reset}>Try again</button>
    </div>
  );
}
EOF
write_new "$BASE/app/global-error.$XX" "$HINT" <<EOF
"use client";
import { useEffect } from "react";
import { captureException } from "../lib/monitor";

export default function GlobalError({ error }$GERRT) {
  useEffect(() => { $REPORT }, [error]);
  return (
    <html>
      <body>
        <h2>Something went wrong.</h2>
      </body>
    </html>
  );
}
EOF

# 7. test page
mkdir -p "$BASE/app/test-monitor"
cat > "$BASE/app/test-monitor/page.$XX" <<'EOF'
"use client";
// Delete this folder after testing.
export default function TestMonitor() {
  return (
    <button style={{ padding: 16, margin: 24 }} onClick={() => { throw new Error("Test error from Next.js"); }}>
      Break it
    </button>
  );
}
EOF
echo "✅ $BASE/app/test-monitor/page.$XX (test page)"

if ! cat .env .env.local 2>/dev/null | grep -q "^NEXT_PUBLIC_MONITOR_DSN=" || ! cat .env .env.local 2>/dev/null | grep -q "^MONITOR_DSN="; then
  echo "⚠️  Add MONITOR_DSN and NEXT_PUBLIC_MONITOR_DSN to .env (your DSN ending in /1)."
fi
if [ "$BASE" = src ] && [ -f lib/monitor.js ]; then
  echo "ℹ️  Old copy found at lib/monitor.js: you can delete it (the new one is in src/lib/)."
fi

cat <<EOF

Done. Now:
  1. Restart:  npm run dev
  2. Open:     http://localhost:3000/test-monitor  → click "Break it"
  3. Within 5 seconds it shows on your Monitor dashboard. Then delete $BASE/app/test-monitor
EOF
