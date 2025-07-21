active_tool=$(curl -s 'http://localhost:7125/printer/objects/query?tool_probe_endstop' \
  | jq -r '.result.status.tool_probe_endstop.active_tool_number')

tool_name=$(curl -s 'http://localhost:7125/printer/objects/query?toolchanger' \
  | jq -r '.result.status.toolchanger.tool_names['"$active_tool"']')

today=$(date +%F)

~/klipper/scripts/calibrate_shaper.py /tmp/calibration_data_y_*.csv \
  -o ~/printer_data/config/shaper-stuffs/results/${tool_name}_Y_${today}.png
