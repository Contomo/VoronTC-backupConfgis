#!/bin/bash

# Find the most recent calibration data file for the X axis
INPUT_CSV=$(ls -t /tmp/calibration_data_x_*.csv 2>/dev/null | head -n 1)

if [ -z "$INPUT_CSV" ]; then
    echo "Error: No X-axis calibration CSV file found in /tmp/"
    exit 1
fi

TOOL_NAME=$(/usr/bin/curl -s 'http://localhost:7125/printer/objects/query?toolchanger' \
    | /usr/bin/jq -r ".result.status.toolchanger.tool")

TIMESTAMP=$(date +%F_%H-%M)
OUTPUT_PNG="${HOME}/printer_data/config/ShakeTune/normal_out/${TOOL_NAME}_X_${TIMESTAMP}.png"

"${HOME}/klippy-env/bin/python" "${HOME}/klipper/scripts/calibrate_shaper.py" "$INPUT_CSV" -o "$OUTPUT_PNG"
