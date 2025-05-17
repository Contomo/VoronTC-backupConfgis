

────── First Setup ────────────────────────────
If youre trying to set this up from an already running config,
i suggest you start by changing the homing stuff to suite your needs at first.

Hit restart and see if homing works for you.

next step id suggest is to look over the settings in toolchanger.cfg
in there you can change the most common settings related to your config and toolchanging.




──────🔧 INIT & CONFIG ─────────────────────────────────────────────────────────────────────────────────

    _INIT_AT_STARTUP                # Called after printer startup; initializes toolchanger + SVF keys (based on settings).

──────🛠️ TOOL CALIBRATION FIRST SETUP ──────────────────────────────────────────────────────────────────

    TC_FIND_FIRST_CALIBRATION_PROBE_POSITION  # Jog over the probe and run this command to record the first position. only need to do this once.
    TC_GET_PROBE_TRIGGER_TO_BOTTOM            # Run this after the above (if youre using probe calib.) 
                                              # with a known calibrated tool, to print out the value to use for 'trigger_to_bottom_z'

──────🛠️ TOOL CALIBRATION ──────────────────────────────────────────────────────────────────────────────
   --Intended for manual calls--
   TC_ADJUST_OFFSETS_UI            # most likely to need this when starting out, lets you adjust offets live.

   TC_ADJUST_OFFSET                # Manually adjust a tool’s X/Y/Z gcode offset by a given amount. Updates SVF and live G-code offset.
   TC_FIND_TOOL_OFFSETS            # Main calibration loop; heats tools, moves to probe, runs calibration, and queues offsets for save.
   TC_LOAD_OFFSETS                 # Loads offsets from save_variables for a tool, clamps values, falls back to gcode offsets if needed.
   TC_GET_PROBE_TRIGGER_TO_BOTTOM  # Assumes active tool is hand calibrated 'perfect' and spits out the value you need to use for trigger to bottom z.

   TC_OFFSET_STATS                 # print out the stats of collected probing results. just try it lol

   --Internal stuff done--
   _INIT_SVF_KEYS                  # Ensures required tool offset keys exist in save_variables, can auto-repair missing keys.
   _TC_UPDATE_OFFSETS              # Stages tool offsets from TC_FIND_TOOL_OFFSETS, called with SAVE to save results collected to SVF
   _NUDGE_MOVE_OVER_PROBE          # Moves tool 3mm above saved probe position (in svf). Used before probing begins.
   _TC_LOG_OFFSET_HISTORY          # Logs current tool offset results to save_variables as a rolling buffer for stats later.           (offsets_history_t2 = {"x": [1.23, 1.24, ...], "y": [...], "z": [...], "probe": [...]})
                                   #  → Logging is **rolling**: limited to N entries (set via `max_entries_offsets_history`)

──────🛠️ Extras ────────────────────────────────────────────────────────────────────────────────────────────────

   TC_ENDSTOP_AND_CALIBRATION_PROBE_ACCURACY       # Runs two homing/probing loops to estimate endstop/probe accuracy.
     → _TC_CALIBRATION_PROBE_ACCURACY_LOOP
     → _TC_CALIBRATION_PROBE_DATA_COLLECT_LOOP
     → _TC_NUDGE_ANALYZE_AND_PRINT






 ────────────────────────────────────────────────────────────────────────────────────────────────────────────────
  An example tool macro call
   

   [gcode_macro ⬜️]
   variable_active: 0          # active represent big button color in mainsail.
   variable_color: ""          # color represent the small circle button color in mainsail. provide a hex value.
   #---user variables
   variable_filament_runout: 0
   gcode:
        DETECT_ACTIVE_TOOL_PROBE
        TX TN={printer["tool ⬜️"].tool_number} {rawparams}  # we route to global for ease

──────🛠️ Toolchanging ───────────────────────────────────────

    Your T commands work as regular. but!
    You can supply them with extra XYZ parameters to overwrite the restore position
    and restore to a different position after picking up a new tool.

    after toolchange failure, youll get a popup. youll figure it out from there.
    (if you to edit docking position, remember to manually adjust it in config later, will only work till restart.)

 ─────────────────────────────────────────────────────────────────────────
 general gcode macro tips/rules
 ─────────────────────────────────────────────────────────────────────────
   Most custom macros may be called with any toolnumber as the first
   parameter. so anyhting goes aslong as there is a number inside the first
   if none provided, most use the active tool.

   a silent parameter may be provided to disable responses for that macro call.

   in some sections you can find 🟨 blocks. those mark spaces you are most likely to modify stuff

 ─────────────────────────────────────────────────────────────────────────
 workflow
 ─────────────────────────────────────────────────────────────────────────
   in our toolchanger macros we handle everything in absolute coordinate space.
   every move in there should either account for that, or load the correct offsets.
 
 
 
 🟦─────────────────────────────────────────────────────────────────────────────#
                    INPUT SHAPER CONFIGURATION & AUTO-DETECTION               #
 🟦─────────────────────────────────────────────────────────────────────────────#
 This section defines default shaper values that can be used for all tools
 when no tool-specific overrides are found.
 
 🟦 These values are only used if:
    - No per-tool save_variables ('shapers_t0', 'shapers_t1', etc.) are defined
    - No tool-specific or toolchanger params override them
 
 🟦 The macro LOAD_SHAPERS checks for valid shaper values in the following order:
    (highest priority first — first valid match is used)
 
  1️⃣ 'save_variables.shapers_tX' → e.g., 'shapers_t1 = {"freq_x":..., "damp_y":...}'
  2️⃣ '[tool T⬜️]' section params → e.g., 'params_input_shaper_freq_x = 47.5'
  3️⃣ '[toolchanger]' section params → inherited by all tools unless overridden
  4️⃣ 'save_variables.shapers_default' → fallback shared dictionary
  5️⃣ '[input_shaper]' (this section) → ultimate fallback
 
 🟦 Required keys must include:
     - "freq" (for frequency)
     - "damp" (for damping)
     - "x" / "y" (axis specifiers)

 Example valid names:
     ✅ shaper_freq_x, damping_ratio_y, input_shaper_freq_x, etc.
     ❌ Avoid ambiguous keys like "shaper_x" (not detected) or misinterpreted

 You may customize or comment out values here — they will be ignored if higher
 priority sources are found and valid.
 🟦─────────────────────────────────────────────────────────────────────────────#