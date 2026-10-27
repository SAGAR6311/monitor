#!/bin/sh
# Connects free Telegram alerts to your live Monitor. Run once.
# Before: in Telegram, open @BotFather → send /newbot → pick any name → copy the token it gives you.
set -e
cd "$(dirname "$0")"
printf "Paste your bot token from @BotFather: "
read -r TOKEN
curl -s "https://api.telegram.org/bot$TOKEN/getMe" | grep -q '"ok":true' || { echo "That token doesn't work. Copy it again from @BotFather."; exit 1; }
echo "Now open your new bot in Telegram, press Start (or send it 'hi'). Waiting..."
CHAT=
for i in $(seq 1 60); do
  CHAT=$(curl -s "https://api.telegram.org/bot$TOKEN/getUpdates" | node -e "
    let s = ''; process.stdin.on('data', d => s += d).on('end', () => {
      const m = (JSON.parse(s).result || []).reverse().find(u => u.message);
      console.log(m ? m.message.chat.id : '');
    })")
  [ -n "$CHAT" ] && break
  sleep 3
done
[ -n "$CHAT" ] || { echo "No message received. Run ./telegram.sh again."; exit 1; }
printf %s "$TOKEN" | npx wrangler secret put TELEGRAM_BOT_TOKEN
printf %s "$CHAT" | npx wrangler secret put TELEGRAM_CHAT_ID
curl -s -o /dev/null "https://api.telegram.org/bot$TOKEN/sendMessage" -d chat_id="$CHAT" \
  --data-urlencode "text=✅ Monitor alerts connected. New errors will appear here."
echo "Done. You should see a test message in Telegram."
