# mpc_filament_observer.py
#
# Logging-only observer for detecting filament-runout signatures using heater + MPC signals.
# Multiple instances via sections like:
#   [mpc_filament_observer extruder]
#   [mpc_filament_observer extruder1]
#   [mpc_filament_observer extruder2]
#
# GCODE:
#   QUERY_MPC_OBSERVER SENSOR=<name>
#   DUMP_MPC_OBSERVER  SENSOR=<name> [PATH=/tmp/mpc_obs_<name>.csv] [SECONDS=30]
#   SET_MPC_OBSERVER   SENSOR=<name> ENABLE=0|1 MIN_E_DOT=<mm/s> SAMPLE_HZ=<Hz>
#
# No actions yet — this is for data collection and threshold design.

import csv
import logging
from collections import deque

DEFAULT_MIN_E_DOT = 0.20      # mm/s threshold to mark "printing" windows
DEFAULT_SAMPLE_HZ = 10.0      # sampling rate
MAX_BUFFER_SECONDS = 600      # ~10 minutes of history

class MpcObserver:
    def __init__(self, config):
        # section header suffix, e.g. [mpc_filament_observer extruder2] -> "extruder2"
        self.name = config.get_name().split()[-1]
        self.cfg = config
        self.printer = config.get_printer()
        self.reactor = self.printer.get_reactor()
        self.gcode   = self.printer.lookup_object('gcode')

        # ---- hard bind heater at CONFIG TIME (canonical Klipper way) ----
        pheaters = self.printer.load_object(config, "heaters")
        try:
            self.heater = pheaters.lookup_heater(self.name)
        except Exception:
            raise config.error(
                f"mpc_filament_observer: unknown heater '{self.name}'. "
                f"Define [extruder] / [extruderN] before [mpc_filament_observer {self.name}]."
            )

        # try to get MPC control profile's validated fan object (if using MPC)
        self.cooling_fan = None
        try:
            ctrl = self.heater.get_control()
            if ctrl and hasattr(ctrl, "get_profile"):
                prof = ctrl.get_profile()
                if isinstance(prof, dict) and "cooling_fan" in prof:
                    self.cooling_fan = prof["cooling_fan"]  # already a fan object if configured
        except Exception:
            self.cooling_fan = None

        # optional extruder object (for Ė); may not exist yet at config time
        self.extruder_name = self.name
        self.extruder = self.printer.lookup_object(self.extruder_name, None)

        # live knobs
        self.enabled   = True
        self.min_e_dot = DEFAULT_MIN_E_DOT
        self.sample_hz = DEFAULT_SAMPLE_HZ

        # internals
        self.toolhead  = None
        self.timer     = None
        self.dt_probe  = 0.100   # sec window for Ė derivative
        self.armed     = False

        # ring buffer
        self.max_rows = int(MAX_BUFFER_SECONDS * self.sample_hz) + 1
        self.rows     = deque(maxlen=self.max_rows)

        # GCODE
        self.gcode.register_mux_command(
            "QUERY_MPC_OBSERVER", "SENSOR", self.name,
            self.cmd_QUERY, desc="Show current MPC observer snapshot")
        self.gcode.register_mux_command(
            "DUMP_MPC_OBSERVER", "SENSOR", self.name,
            self.cmd_DUMP, desc="Dump recent MPC observer data to CSV")
        self.gcode.register_mux_command(
            "SET_MPC_OBSERVER", "SENSOR", self.name,
            self.cmd_SET, desc="Enable/disable and tune MPC observer")

        # start sampling once everything is fully constructed
        self.printer.register_event_handler("klippy:ready", self._handle_ready)

    # ----- lifecycle -----

    def _handle_ready(self):
        self.toolhead = self.printer.lookup_object("toolhead")
        if self.extruder is None:
            self.extruder = self.printer.lookup_object(self.extruder_name, None)
        self._schedule()

    def _schedule(self):
        if self.timer is not None:
            self.reactor.unregister_timer(self.timer)
        self.timer = self.reactor.register_timer(
            self._on_timer, self.reactor.monotonic() + 1.0 / self.sample_hz
        )

    def _on_timer(self, eventtime):
        try:
            self._sample(eventtime)
        except Exception as e:
            logging.exception("mpc_observer[%s] sample error: %s", self.name, e)
        return eventtime + (1.0 / max(1e-3, self.sample_hz))

    # ----- readers -----

    def _read_heater(self, t):
        # heater.get_temp(t) -> (measured, target)
        temp, target = self.heater.get_temp(t)
        # power accessor variants across controls
        power = 0.0
        for attr in ("get_power", "get_pwm", "power", "pwm"):
            try:
                val = getattr(self.heater, attr)
                power = val(t) if callable(val) else float(val)
                break
            except Exception:
                continue
        return float(temp), float(target), float(power)

    def _read_e_dot(self, t):
        try:
            if not self.toolhead or not self.extruder:
                return 0.0
            # only when this observed extruder is active
            active = self.toolhead.get_extruder()
            if active is not self.extruder:
                return 0.0
            if hasattr(active, "find_past_position"):
                now  = active.find_past_position(t)
                prev = active.find_past_position(t - self.dt_probe)
                return (now - prev) / max(1e-3, self.dt_probe)
        except Exception:
            pass
        return 0.0

    def _read_fan(self, t):
        f = self.cooling_fan
        if f is None:
            return None
        # try structured status first
        try:
            if hasattr(f, "get_status"):
                st = f.get_status(t)
                # common keys: "speed", "rpm", "value", "power"
                for k in ("speed", "value", "power"):
                    if k in st and st[k] is not None:
                        return float(st[k])
        except Exception:
            pass
        # fallback: callables
        for attr in ("get_speed", "get_power", "get_pwm", "get_duty_cycle"):
            try:
                val = getattr(f, attr)
                return float(val(t) if callable(val) else val)
            except Exception:
                continue
        return None

    def _read_mpc_status(self, t):
        try:
            ctrl = self.heater.get_control()
            if not ctrl or not hasattr(ctrl, "get_status"):
                return None
            st = ctrl.get_status(t)
            return {
                "mpc_temp_block":    st.get("temp_block"),
                "mpc_temp_sensor":   st.get("temp_sensor"),
                "mpc_temp_ambient":  st.get("temp_ambient"),
                "mpc_power":         st.get("power"),
                "mpc_loss_ambient":  st.get("loss_ambient"),
                "mpc_loss_filament": st.get("loss_filament"),
            }
        except Exception:
            return None

    # ----- sampling -----

    def _sample(self, t):
        if not self.enabled:
            return

        temp, target, power = self._read_heater(t)
        e_dot = self._read_e_dot(t)
        fan   = self._read_fan(t)
        mpc   = self._read_mpc_status(t)

        self.armed = (abs(e_dot) >= self.min_e_dot) and (target > 0.0)

        row = {
            "t": t,
            "temp": temp,
            "target": target,
            "power": power,
            "fan": fan,
            "e_dot": e_dot,
            "armed": int(self.armed),
        }
        if mpc:
            row.update(mpc)

        if self.rows:
            dt = max(1e-6, t - self.rows[-1]["t"])
            row["dtemp_dt"]  = (temp  - self.rows[-1]["temp"])  / dt
            row["dpower_dt"] = (power - self.rows[-1]["power"]) / dt
        else:
            row["dtemp_dt"]  = 0.0
            row["dpower_dt"] = 0.0

        self.rows.append(row)

    # ----- GCODE -----

    def cmd_QUERY(self, gcmd):
        if not self.rows:
            gcmd.respond_info(f"MPC_OBSERVER {self.name}: no data yet")
            return
        r = self.rows[-1]
        fan_val = r.get("fan", None)
        fan_str = "None" if fan_val is None else f"{fan_val:.3f}"
        parts = [
            f"SENSOR={self.name}",
            f"temp={r['temp']:.2f}",
            f"target={r['target']:.1f}",
            f"power={r['power']:.3f}",
            f"fan={fan_str}",
            f"e_dot={r['e_dot']:.3f} mm/s",
            f"dtemp_dt={r['dtemp_dt']:.3f} K/s",
            f"armed={r['armed']}",
        ]
        if "mpc_loss_filament" in r:
            parts += [
                f"mpc_loss_filament={r['mpc_loss_filament']:.6f}",
                f"mpc_loss_ambient={r['mpc_loss_ambient']:.6f}",
                f"mpc_power={r['mpc_power']:.3f}",
            ]
        gcmd.respond_info(" ".join(parts))

    def cmd_DUMP(self, gcmd):
        path = gcmd.get("PATH", f"/tmp/mpc_obs_{self.name}.csv")
        seconds = gcmd.get_float("SECONDS", 30.0, minval=0.1)
        if not self.rows:
            gcmd.respond_info(f"MPC_OBSERVER {self.name}: no data to dump")
            return
        t_max = self.rows[-1]["t"]
        t_min = t_max - seconds
        rows = [r for r in self.rows if r["t"] >= t_min]

        base_cols = ["t","temp","target","power","fan","e_dot","dtemp_dt","dpower_dt","armed"]
        mpc_cols  = []
        if any("mpc_loss_filament" in r for r in rows):
            mpc_cols = [
                "mpc_temp_block","mpc_temp_sensor","mpc_temp_ambient",
                "mpc_power","mpc_loss_ambient","mpc_loss_filament"
            ]
        cols = base_cols + mpc_cols

        try:
            with open(path, "w", newline="") as f:
                w = csv.DictWriter(f, fieldnames=cols)
                w.writeheader()
                for r in rows:
                    w.writerow({k: r.get(k, "") for k in cols})
            gcmd.respond_info(f"MPC_OBSERVER {self.name}: wrote {len(rows)} rows to {path}")
        except Exception as e:
            gcmd.respond_error(f"MPC_OBSERVER {self.name}: CSV write failed: {e}")

    def cmd_SET(self, gcmd):
        if gcmd.get("ENABLE", None) is not None:
            self.enabled = bool(int(gcmd.get("ENABLE")))
        if gcmd.get("MIN_E_DOT", None) is not None:
            self.min_e_dot = float(gcmd.get("MIN_E_DOT"))
        if gcmd.get("SAMPLE_HZ", None) is not None:
            self.sample_hz = max(1.0, float(gcmd.get("SAMPLE_HZ")))
            self.max_rows = int(MAX_BUFFER_SECONDS * self.sample_hz) + 1
            self.rows = deque(self.rows, maxlen=self.max_rows)
            self._schedule()
        gcmd.respond_info(
            f"MPC_OBSERVER {self.name}: enable={int(self.enabled)} "
            f"min_e_dot={self.min_e_dot:.3f} sample_hz={self.sample_hz:.1f}"
        )

def load_config_prefix(config):
    return MpcObserver(config)
