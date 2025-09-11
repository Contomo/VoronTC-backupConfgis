#!/usr/bin/env bash
set -euo pipefail

# ===================== FIXED SETTINGS =====================
IFACE="can0"
CAN_SPEED="1000000"                 # firmware CAN bitrate
KLIPPER_DIR="$HOME/klipper"
KATAPULT_DIR="$HOME/katapult"
STOP_KLIPPER=1                      # set 0 if you don't want me to stop the service

# UUID list: "Name:uuid"
TARGETS=(
  "White Anthead:76a9dd982630"
  "White DB:9422cd1b01cb"
  "Blue DB:e9d35af42343"
  "Red DB:7465234f8edb"
  "Yellow Yavoth:70b63482dbf8"
  "Orange Yavoth:868cbf9d7ccf"
  "Purple Anthead:2f4800498a26"
)
# ==========================================================

die(){ echo "ERROR: $*" >&2; exit 1; }
info(){ echo "== $*"; }
ok(){ echo "✓ $*"; }
warn(){ echo "! $*"; }

# Tools
PY="$HOME/klippy-env/bin/python"; [[ -x "$PY" ]] || PY="$(command -v python3 || true)"
[[ -n "$PY" ]] || die "python3 not found"
[[ -d "$KLIPPER_DIR"  ]] || die "No Klipper repo at $KLIPPER_DIR"
[[ -d "$KATAPULT_DIR" ]] || die "No Katapult repo at $KATAPULT_DIR"
FLASHTOOL="$KATAPULT_DIR/scripts/flashtool.py"
[[ -f "$FLASHTOOL" ]] || die "Missing $FLASHTOOL"

stop_klipper(){
  if (( STOP_KLIPPER )); then
    if systemctl is-active --quiet klipper 2>/dev/null; then
      info "Stopping klipper..."
      sudo systemctl stop klipper || true
      sleep 1
    fi
  fi
}
start_klipper(){
  if (( STOP_KLIPPER )); then
    if systemctl list-unit-files | grep -q '^klipper\.service'; then
      info "Starting klipper..."
      sudo systemctl start klipper || true
    fi
  fi
}
trap start_klipper EXIT

write_config(){
  info "Writing Klipper .config for EBB36 (STM32G0B1, 8 KiB bootloader, 8 MHz, CAN PB0/PB1 @ ${CAN_SPEED})"
  cd "$KLIPPER_DIR"
  rm -f .config out/autoconf.h out/klipper.bin || true
  cat > .config <<EOF
CONFIG_LOW_LEVEL_OPTIONS=y
CONFIG_MACH_STM32=y
CONFIG_BOARD_DIRECTORY="stm32"
CONFIG_MCU="stm32g0b1xx"
CONFIG_CLOCK_FREQ=64000000
CONFIG_FLASH_SIZE=0x20000
CONFIG_FLASH_BOOT_ADDRESS=0x8000000
CONFIG_RAM_START=0x20000000
CONFIG_RAM_SIZE=0x24000
CONFIG_STACK_SIZE=512
CONFIG_FLASH_APPLICATION_ADDRESS=0x8002000
CONFIG_STM32_SELECT=y
CONFIG_MACH_STM32G0B1=y
CONFIG_MACH_STM32G0=y
CONFIG_MACH_STM32G0Bx=y
CONFIG_HAVE_STM32_USBFS=y
CONFIG_HAVE_STM32_FDCANBUS=y
CONFIG_HAVE_STM32_USBCANBUS=y
CONFIG_STM32_DFU_ROM_ADDRESS=0
CONFIG_STM32_FLASH_START_2000=y
CONFIG_STM32_CLOCK_REF_8M=y
CONFIG_CLOCK_REF_FREQ=8000000
CONFIG_STM32F0_TRIM=16
CONFIG_STM32_MMENU_CANBUS_PB0_PB1=y
CONFIG_STM32_CANBUS_PB0_PB1=y
CONFIG_USB_VENDOR_ID=0x1d50
CONFIG_USB_DEVICE_ID=0x614e
CONFIG_USB_SERIAL_NUMBER="12345"
CONFIG_WANT_ADC=y
CONFIG_WANT_SPI=y
CONFIG_WANT_SOFTWARE_SPI=y
CONFIG_WANT_I2C=y
CONFIG_WANT_SOFTWARE_I2C=y
CONFIG_WANT_HARD_PWM=y
CONFIG_WANT_BUTTONS=y
CONFIG_WANT_TMCUART=y
CONFIG_WANT_NEOPIXEL=y
CONFIG_WANT_PULSE_COUNTER=y
CONFIG_WANT_ST7920=y
CONFIG_WANT_HD44780=y
CONFIG_WANT_ADXL345=y
CONFIG_WANT_LIS2DW=y
CONFIG_WANT_MPU9250=y
CONFIG_WANT_ICM20948=y
CONFIG_WANT_THERMOCOUPLE=y
CONFIG_WANT_HX71X=y
CONFIG_WANT_ADS1220=y
CONFIG_WANT_LDC1612=y
CONFIG_WANT_SENSOR_ANGLE=y
CONFIG_NEED_SENSOR_BULK=y
CONFIG_WANT_LOAD_CELL_PROBE=y
CONFIG_NEED_SOS_FILTER=y
CONFIG_CANSERIAL=y
CONFIG_CANBUS=y
CONFIG_CANBUS_FREQUENCY=1000000
CONFIG_CANBUS_FILTER=y
CONFIG_INLINE_STEPPER_HACK=y
CONFIG_HAVE_STEPPER_OPTIMIZED_BOTH_EDGE=y
CONFIG_WANT_STEPPER_OPTIMIZED_BOTH_EDGE=y
CONFIG_INITIAL_PINS=""
CONFIG_HAVE_GPIO=y
CONFIG_HAVE_GPIO_ADC=y
CONFIG_HAVE_GPIO_SPI=y
CONFIG_HAVE_GPIO_I2C=y
CONFIG_HAVE_GPIO_HARD_PWM=y
CONFIG_HAVE_STRICT_TIMING=y
CONFIG_HAVE_CHIPID=y
CONFIG_HAVE_BOOTLOADER_REQUEST=y
EOF
}

build_fw(){
  info "Building Klipper..."
  cd "$KLIPPER_DIR"
  make clean >/dev/null 2>&1 || true
  make -j"$(nproc)"

  [[ -f out/klipper.bin ]] || die "Build did not produce out/klipper.bin"

  if ! grep -q '^#define CONFIG_CANBUS 1' out/autoconf.h; then
    die "CANBUS is not enabled in autoconf.h; refusing to flash a SERIAL build."
  fi
  if grep -q '^#define CONFIG_USBSERIAL 1' out/autoconf.h || grep -q '^#define CONFIG_SERIAL 1' out/autoconf.h; then
    die "USB or SERIAL also enabled; only CANBUS must be active."
  fi
}

to_bootloader(){
  local uuid="$1"
  info "Rebooting ${uuid} into Katapult..."
  "$PY" "$FLASHTOOL" -i "$IFACE" -u "$uuid" -r || true
  sleep 1
}

wait_katapult(){
  local uuid="$1" t=0
  until "$PY" "$FLASHTOOL" -i "$IFACE" -u "$uuid" -s >/dev/null 2>&1; do
    ((t++>=25)) && return 1
    sleep 0.2
  done
}

verify_klipper(){
  local uuid="$1"
  "$PY" "$KLIPPER_DIR/scripts/canbus_query.py" "$IFACE" \
    | grep -q "canbus_uuid=${uuid}, Application: Klipper"
}

flash_one(){
  local name="$1" uuid="$2"
  echo; info "==== $name [$uuid] ===="

  to_bootloader "$uuid"
  wait_katapult "$uuid" || warn "Bootloader not seen; trying flash anyway"

  info "Flashing..."
  if ! "$PY" "$FLASHTOOL" -i "$IFACE" -u "$uuid" -f "$KLIPPER_DIR/out/klipper.bin"; then
    warn "Flash command failed for $name"; return 1
  fi

  sleep 0.5
  if verify_klipper "$uuid"; then
    ok "Flashed $name"
  else
    warn "$name flashed but still not reporting as Klipper; power-cycle or reset that board"
    return 1
  fi
}


main(){
  stop_klipper
  write_config
  build_fw

  local failed=()
  for row in "${TARGETS[@]}"; do
    name="${row%%:*}"
    uuid="${row##*:}"
    if ! flash_one "$name" "$uuid"; then
      failed+=("$name:$uuid")
    fi
  done

  if ((${#failed[@]})); then
    echo
    warn "Some targets failed:"
    printf '  - %s\n' "${failed[@]}"
    exit 1
  fi

  echo
  ok "All targets processed."
}

main
