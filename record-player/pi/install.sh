#!/usr/bin/env bash
# Sets up the Raspberry Pi: Spotify Connect player (raspotify) + the turntable server.
# Run it on the Pi, from the record-player folder:   bash pi/install.sh
set -euo pipefail
DIR="$(cd "$(dirname "$0")/.." && pwd)"
NAME="Record Player"

echo "== Installing packages"
sudo apt-get update
sudo apt-get install -y python3 python3-pil alsa-utils curl

if ! command -v librespot >/dev/null; then
  echo "== Installing raspotify (librespot)"
  curl -sL https://dtcooper.github.io/raspotify/install.sh | sh
fi

echo "== Naming the speaker '$NAME'"
CONF=/etc/raspotify/conf
sudo sed -i '/^#\?LIBRESPOT_NAME=/d; /^#\?LIBRESPOT_AUTOPLAY=/d; /^#\?LIBRESPOT_BITRATE=/d' "$CONF"
printf 'LIBRESPOT_NAME="%s"\nLIBRESPOT_BITRATE="320"\n' "$NAME" | sudo tee -a "$CONF" >/dev/null
sudo systemctl restart raspotify

echo "== Installing the turntable service"
[ -f "$DIR/server/config.json" ] || cp "$DIR/server/config.example.json" "$DIR/server/config.json"
sed "s|__USER__|$USER|; s|__DIR__|$DIR|g" "$DIR/pi/turntable.service" | sudo tee /etc/systemd/system/turntable.service >/dev/null
sudo systemctl daemon-reload
sudo systemctl enable --now turntable

echo
echo "Done. Next:"
echo "  1. Put your Spotify Client ID in $DIR/server/config.json, then: sudo systemctl restart turntable"
echo "  2. Open http://$(hostname).local:8888/setup in a browser and sign in to Spotify"
echo "  3. In the Spotify app on your phone, pick the speaker '$NAME' once so the Pi remembers your account"
