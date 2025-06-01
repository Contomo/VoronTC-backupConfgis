#!/usr/bin/env bash

# ─── CONFIGURATION ───────────────────────────────────────────
INTERFACE="wlan0"
SSID="Empl-Router"
PING_IP="1.1.1.1"
LOGDIR="/home/contomo/printer_data/config/gcode-shell/mesh-wifi-fix/logs"
LOGFILE="$LOGDIR/wifi-test.log"
TEST_URL="http://speedtest.tele2.net/1MB.zip"
MIN_SPEED_KB=2500    # minimum acceptable download (KB/s)

# ensure logdir exists
mkdir -p "$LOGDIR"

# ─── 1) Bandwidth check ───────────────────────────────────────
echo "[$(date)] Testing bandwidth from $TEST_URL…" >> "$LOGFILE"
bw_bytes=$(curl -s -w '%{speed_download}' -o /dev/null "$TEST_URL")
bw_kb=$(awk "BEGIN{printf \"%d\", $bw_bytes/1024}")
echo "[$(date)] Download speed: ${bw_kb} KB/s" >> "$LOGFILE"

if [ "$bw_kb" -ge "$MIN_SPEED_KB" ]; then
  # above threshold → all good
  exit 0
fi
echo "[$(date)] BELOW THRESHOLD (${MIN_SPEED_KB} KB/s) - triggering recovery…" >> "$LOGFILE"

# ─── 2) Scan for mesh BSSIDs ──────────────────────────────────
mapfile -t BSSIDS < <(
  iw dev "$INTERFACE" scan 2>/dev/null |
    awk -v ssid="$SSID" '
      /^BSS/   { b=$2 }
      /SSID:/  { if ($2==ssid) print b }
    '
)

# ─── 3) Pick the strongest AP ────────────────────────────────
best_bssid=""; best_sig=-1000
for b in "${BSSIDS[@]}"; do
  sig=$(iw dev "$INTERFACE" scan 2>/dev/null |
        awk -v b="$b" '
          $1=="BSS" && $2==b { f=1; next }
          f && /signal:/       { print $2; exit }
        '
  )
  (( sig > best_sig )) && best_sig=$sig && best_bssid=$b
done

if [[ -n "$best_bssid" ]]; then
  echo "[$(date)] Roaming to $best_bssid (signal=${best_sig}dBm)" >> "$LOGFILE"
  sudo wpa_cli -i "$INTERFACE" roam "$best_bssid" &>/dev/null
  sleep 5
fi

# ─── 4) Flap the radio if still dead ──────────────────────────
if ! ping -c3 -W2 "$PING_IP" &>/dev/null; then
  echo "[$(date)] Roaming failed—flapping $INTERFACE" >> "$LOGFILE"
  sudo ip link set dev "$INTERFACE" down
  sleep 3
  sudo ip link set dev "$INTERFACE" up
  sleep 1
  sudo wpa_cli -i "$INTERFACE" reassociate &>/dev/null
  sleep 5
fi

# ─── 5) Final check ───────────────────────────────────────────
if ping -c3 -W2 "$PING_IP" &>/dev/null; then
  echo "[$(date)] Reconnected successfully" >> "$LOGFILE"
else
  echo "[$(date)] Still no link!" >> "$LOGFILE"
fi
