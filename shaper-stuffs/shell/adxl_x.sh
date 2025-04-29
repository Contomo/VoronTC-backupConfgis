active_tool=$(curl -s 'http://localhost:7125/printer/objects/query?tool_probe_endstop' \
  | jq -r '.result.status.tool_probe_endstop.active_tool_number')

tool_name=$(curl -s 'http://localhost:7125/printer/objects/query?toolchanger' \
  | jq -r '.result.status.toolchanger.tool_names['"$active_tool"']')

today=$(date +%F)  # e.g. 2025-04-05

~/klipper/scripts/calibrate_shaper.py /tmp/calibration_data_x_*.csv \
  -o ~/home/contomo/printer_data/config/shaper-stuffs/results/${tool_name}_X_${today}.png

