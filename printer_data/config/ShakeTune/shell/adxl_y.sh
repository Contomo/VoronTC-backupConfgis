#!/bin/bash

# Find the most recent calibration data file for the Y axis
INPUT_CSV=$(ls -t /tmp/calibration_data_y_*.csv | head -n 1)

# Check if a file was found
if [ -z "$INPUT_CSV" ]; then
    echo "Error: No Y-axis calibration CSV file found in /tmp/"
    exit 1
fi

# Get active tool name (using full path for curl)
ACTIVE_TOOL=$(/usr/bin/curl -s 'http://localhost:7125/printer/objects/query?tool_probe_endstop' | /usr/bin/jq -r '.result.status.tool_probe_endstop.active_tool_number')
TOOL_NAME=$(/usr/bin/curl -s 'http://localhost:7125/printer/objects/query?toolchanger' | /usr/bin/jq -r ".result.status.toolchanger.tool_names[$ACTIVE_TOOL]")
TIMESTAMP=$(date +%F_%H-%M)

# Define the full output path
OUTPUT_PNG="/home/contomo/printer_data/config/ShakeTune/normal_out/${TOOL_NAME}_Y_${TIMESTAMP}.png"

# Run the official Klipper script using full paths
/home/contomo/klipper/scripts/calibrate_shaper.py "$INPUT_CSV" -o "$OUTPUT_PNG"