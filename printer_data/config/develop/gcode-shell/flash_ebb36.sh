# New opts near the top
EXPECT_MCU="${EXPECT_MCU:-STM32G0B1}"   # what we expect before flashing
LIST_ONLY=0
FORCE=0

# Add to arg parser
    --list) LIST_ONLY=1; shift;;
    --expect-mcu) EXPECT_MCU="$2"; shift 2;;
    --force) FORCE=1; shift;;

# New helper to parse App type from canbus_query.py
print_discovery() {
  echo "== canbus_query (${IFACE}) =="
  $PY "$CANQUERY" "$IFACE" 2>/dev/null || true
  echo
  echo "== katapult flashtool -q (${IFACE}) =="
  python3 "$FLASH" -i "$IFACE" -q 2>/dev/null || true
  echo
}

# Handle --list and exit early
if [[ $LIST_ONLY -eq 1 ]]; then
  # Stop klipper temporarily so query sees initialized devices
  if systemctl is-active --quiet "$KLIPPER_SVC"; then
    echo "Stopping $KLIPPER_SVC for discovery..."
    sudo systemctl stop "$KLIPPER_SVC"
    print_discovery
    echo "Starting $KLIPPER_SVC..."
    sudo systemctl start "$KLIPPER_SVC" || true
  else
    print_discovery
  fi
  exit 0
fi

# Before flashing: positively identify the target
probe_uuid() {
  local uuid="$1"
  echo "Requesting bootloader for UUID=$uuid..."
  python3 "$FLASH" -i "$IFACE" -u "$uuid" -r || true
  sleep 1
  echo "Querying bootloader status for UUID=$uuid..."
  local status
  if ! status="$(python3 "$FLASH" -i "$IFACE" -u "$uuid" -s 2>&1)"; then
    echo "Could not query bootloader status."
    return 1
  fi
  echo "$status"
  # Heuristics: must look like Katapult on the expected MCU
  if ! grep -iq "katapult" <<<"$status"; then
    echo "Refusing: target is not in Katapult (no 'Katapult' in status)."
    return 2
  fi
  if [[ -n "$EXPECT_MCU" ]] && ! grep -iq "$EXPECT_MCU" <<<"$status"; then
    if [[ $FORCE -eq 1 ]]; then
      echo "WARNING: Expected MCU '$EXPECT_MCU' not detected, but --force set. Continuing."
    else
      echo "Refusing: Expected MCU '$EXPECT_MCU' not detected. Use --expect-mcu or --force to override."
      return 3
    fi
  fi
  return 0
}

# ... later, just before the actual flash ...
if [[ -z "$UUID" ]]; then
  echo "Discovery mode. Showing devices:"
  print_discovery
  echo "Re-run with: UUID=<uuid> $0"
  exit 0
fi

# PROBE: bail out if the UUID doesn't look like the EBB36/Katapult we expect
if ! probe_uuid "$UUID"; then
  echo "Safety gate blocked the flash. Fix the target or use --force if you’re absolutely sure."
  exit 1
fi

# Now do the real flash
echo "Flashing UUID=$UUID on $IFACE ..."
python3 "$FLASH" -i "$IFACE" -u "$UUID" -f "$FW"
