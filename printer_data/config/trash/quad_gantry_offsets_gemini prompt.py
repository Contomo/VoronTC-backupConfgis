import logging
from typing import List, Tuple, Optional
import random, numpy as np
import math

def _dbg(gcmd, label, **vals):
    """Pretty-print any number of name=value pairs via RESPOND_INFO."""
    gcmd.respond_info(f"[{label}] " + "  ".join(f"{k}={v:+.6f}" for k,v in vals.items()))

PROBE_PATTERNS = {
    1: [(0, 0)],
    2: [(-1, 0), (1, 0)],
    3: [(0, -1), (0, 0), (0, 1)],
    4: [(-1, 0), (1, 0), (0, -1), (0, 1)],
    5: [(-1, 0), (1, 0), (0, -1), (0, 0), (0, 1)],
}

_param_help = """\noptionally to overwrite config defined:\n[POINTS] [SAFE_Z] [TILT] [JITTER] [F]"""

def fit_plane(points: List[Tuple[float, float, float]]):
    if not points: return 0.0, 0.0, 0.0
    A = np.array([[x, y, 1] for x, y, _ in points])
    b = np.array([z for _, _, z in points])
    mx, my, c = np.linalg.lstsq(A, b, rcond=None)[0]
    return float(mx), float(my), float(c)

class QuadGantryOffsets:
    def __init__(self, config):
        self.config   = config
        self.printer  = config.get_printer()
        self.gcode    = self.printer.lookup_object('gcode')

        # User‑configurable knobs
        self.jitter_mm  = config.getfloat('jitter', 0.1, minval=0.0)
        self.def_pts    = config.getint('points', 1, minval=1, maxval=5)
        self.cfg_tilt   = config.getfloat('tilt', 5.0, above=0.0)
        self.length_mm  = config.getfloat('length', None, above=0.0)
        self.safe_z_mm  = config.getfloat('safe_z', None, above=0.0)
        self.speed_xy   = config.getfloat('speed', None, above=0.0)
        self.center_x   = config.getfloat("center_x", None)
        self.center_y   = config.getfloat("center_y", None)
        self.tilt_mm    = self.cfg_tilt
        # Runtime state
        self.current_delta = [0.0] * 4
        self._baseline = None
        self._last_off = (0.0, 0.0, 0.0)
        self._last_planes = {}
        
        # Event + command registration
        self.printer.register_event_handler('klippy:connect', self._on_connect)
        self.gcode.register_command('QGO_SLOPED_G0', self.cmd_QGO_SLOPED_G0, desc=self.cmd_QGO_SLOPED_G0_help)
        self.gcode.register_command('QGO_TILT_GANTRY', self.cmd_QGO_TILT_GANTRY, desc=self.cmd_QGO_TILT_GANTRY_help)
        self.gcode.register_command('QGO_CALIBRATE_BASELINE', self.cmd_QGO_CALIBRATE_BASELINE, desc=self.cmd_QGO_CALIBRATE_BASELINE_help)
        self.gcode.register_command('QGO_CALIBRATE_OFFSET', self.cmd_QGO_CALIBRATE_OFFSET, desc=self.cmd_QGO_CALIBRATE_OFFSET_help)

    def _on_connect(self):
        self.toolhead = self.printer.lookup_object('toolhead')
        self.gcode_move = self.printer.lookup_object('gcode_move')
        self.qgl = self.printer.lookup_object('quad_gantry_level', default=None)
        if self.qgl is None:
            raise self.config.error("[quad_gantry_offsets] requires [quad_gantry_level] to be set up")
        self.probe_h = self.qgl.probe_helper
        self.zhelper = self.qgl.z_helper
        xs, ys = zip(*self.qgl.probe_helper.probe_points)
        mid_x, mid_y = (min(xs) + max(xs)) / 2.0, (min(ys) + max(ys)) / 2.0
        self.sign_x = [-1 if x < mid_x else +1 for x in xs]
        self.sign_y = [-1 if y < mid_y else +1 for y in ys]
        (x0, y0), (x2, y2) = self.qgl.gantry_corners
        self._gantry_xy = [(x0, y0), (x0, y2), (x2, y2), (x2, y0)]
        self.span_x = abs(x2 - x0)
        self.span_y = abs(y2 - y0)
        self._update_slopes()
        if self.center_x is None: self.center_x = mid_x
        if self.center_y is None: self.center_y = mid_y
        if self.length_mm is None: self.length_mm = min(self.span_x, self.span_y) / 4.0
        if self.speed_xy is None: self.speed_xy = self.probe_h.speed
        if self.safe_z_mm is None: self.safe_z_mm = self.qgl.horizontal_move_z
        self.lift_speed = self.probe_h.get_lift_speed()

    def _update_slopes(self):
        # Slope in X direction, from tilting around Y axis
        self.slope_x = self.tilt_mm / self.span_x if self.span_x > 1e-6 else 0
        # Slope in Y direction, from tilting around X axis
        self.slope_y = self.tilt_mm / self.span_y if self.span_y > 1e-6 else 0

    def _params(self, gcmd, baseline:bool=False):
        pts = gcmd.get_int('POINTS', self.def_pts, minval=1, maxval=5)
        safe_z = gcmd.get_float('SAFE_Z', self.safe_z_mm, above=0.0)
        tilt = gcmd.get_float('TILT', self.cfg_tilt, above=0.0)
        jitter = gcmd.get_float('JITTER', self.jitter_mm, minval=0.0)
        speed = gcmd.get_float('F', self.speed_xy * 60, above=0.0)
        self.speed_xy = speed / 60.0
        if baseline and self.tilt_mm != tilt:
            self.tilt_mm = tilt
            self._update_slopes()
        elif not baseline and self.tilt_mm != tilt:
            raise gcmd.error(f"TILT mismatch. Baseline used {self.tilt_mm}mm.")
        return pts, safe_z, jitter

    def _jit(self, x, y, r):
        return (x + random.uniform(-r, r), y + random.uniform(-r, r)) if r > 0 else (x, y)

    def _probe_at(self, x, y, safe_z, jitter):
        xj, yj = self._jit(x, y, jitter)
        self.tilted_move(x=xj, y=yj, z=safe_z, speed=self.speed_xy)
        self.gcode.run_script_from_command("PROBE")
        self.toolhead.wait_moves()
        probe_obj = self.printer.lookup_object('probe', None)
        measured_z = (probe_obj.get_status(self.printer.get_reactor().monotonic())['last_z_result'])
        self.tilted_move(z=safe_z, speed=self.lift_speed)
        return measured_z

    def _probe_points(self, n, safe_z, jitter):
        pattern = PROBE_PATTERNS.get(n, PROBE_PATTERNS[1])
        length = self.length_mm
        return [(p[0], p[1], self._probe_at(self.center_x + p[0]*length/2.0, self.center_y + p[1]*length/2.0, safe_z, jitter)) for p in pattern]

    def _measure_z_at_center(self, pts, safe_z, jitter):
        probed_points = self._probe_points(pts, safe_z, jitter)
        return float(np.mean([p[2] for p in probed_points]))

    def _current_plane_offset(self, x, y):
        ho = self.gcode_move.get_status()['homing_origin']
        phys_x, phys_y = x - ho.x, y - ho.y
        pts = [(gx, gy, h) for (gx, gy), h in zip(self._gantry_xy, self.current_delta)]
        mx, my, c = fit_plane(pts)
        return (mx * phys_x + my * phys_y + c)

    def _tilt(self, X=0.0, Y=0.0, r:bool=False):
        start_z = self.toolhead.get_position()[2]
        raw     = [0.5*(sy*X + sx*Y) for sx, sy in zip(self.sign_x, self.sign_y)]
        delta   = [raw_i - c for raw_i, c in zip(raw, self.current_delta)]
        if not any(abs(v) > 1e-6 for v in delta): return
        self.zhelper.adjust_steppers(delta, self.lift_speed)
        self.current_delta = raw
        if r: self.toolhead.manual_move([None, None, start_z], self.lift_speed)

    def tilted_move(self, *, x=None, y=None, z=None, speed=None):
        cx, cy, cz = self.toolhead.get_position()[:3]
        tx = x if x is not None else cx
        ty = y if y is not None else cy
        tz = z if z is not None else cz
        off_cur = self._current_plane_offset(cx, cy)
        off_tgt = self._current_plane_offset(tx, ty)
        final_tz = tz + (off_tgt - off_cur)
        final_speed = speed if speed is not None else self.speed_xy
        self.toolhead.manual_move([tx, ty, final_tz], final_speed)

    cmd_QGO_SLOPED_G0_help = ("Move with gantry-tilt compensation.")
    def cmd_QGO_SLOPED_G0(self, gcmd):
        speed = gcmd.get_float('F', None, above=0.0)
        if speed is not None: speed /= 60.0
        self.tilted_move(x=gcmd.get_float('X', None), y=gcmd.get_float('Y', None), z=gcmd.get_float('Z', None), speed=speed)

    cmd_QGO_TILT_GANTRY_help = ("Tilt the gantry: [X=<mm>] [Y=<mm>] [RESTORE=0/1]/nRotates around provided axis, RESTORE to restore the Z position")
    def cmd_QGO_TILT_GANTRY(self, gcmd):
        self._tilt(X=gcmd.get_float('X', 0.0), Y=gcmd.get_float('Y', 0.0), r=bool(gcmd.get_int('R', 0)))

    cmd_QGO_CALIBRATE_BASELINE_help = ("Generate a baseline for tool offsets." + _param_help)
    def cmd_QGO_CALIBRATE_BASELINE(self, gcmd):
        pts, safe_z, jitter = self._params(gcmd, baseline=True)
        gcmd.respond_info(f"--- STARTING BASELINE CALIBRATION ---")
        self._tilt() # Ensure gantry is flat
        z_f_base = self._measure_z_at_center(pts, safe_z, jitter)
        # Tilt around Y-axis to induce a slope in the X-direction (slope_x)
        self._tilt(Y=self.tilt_mm)
        z_x_base = self._measure_z_at_center(pts, safe_z, jitter)
        # Tilt around X-axis to induce a slope in the Y-direction (slope_y)
        self._tilt(X=self.tilt_mm)
        z_y_base = self._measure_z_at_center(pts, safe_z, jitter)
        self._tilt(r=True) # Restore flat gantry and Z position
        self._baseline = {'f': z_f_base, 'x': z_x_base, 'y': z_y_base}
        _dbg(gcmd, "[DEBUG] BASELINE RAW Z", z_flat=z_f_base, z_x_slope=z_x_base, z_y_slope=z_y_base)
        gcmd.respond_info("--- BASELINE CALIBRATION COMPLETE ---")

    cmd_QGO_CALIBRATE_OFFSET_help = ("Calibrate tool offsets against the baseline." + _param_help)
    def cmd_QGO_CALIBRATE_OFFSET(self, gcmd):
        if not self._baseline:
            raise gcmd.error("No baseline found. Run QGO_CALIBRATE_BASELINE first.")
        pts, safe_z, jitter = self._params(gcmd)
        gcmd.respond_info(f"--- STARTING OFFSET CALIBRATION ---")
        self._tilt()
        z_f_new = self._measure_z_at_center(pts, safe_z, jitter)
        # Tilt around Y-axis (induces slope_x)
        self._tilt(Y=self.tilt_mm)
        z_x_new = self._measure_z_at_center(pts, safe_z, jitter)
        # Tilt around X-axis (induces slope_y)
        self._tilt(X=self.tilt_mm)
        z_y_new = self._measure_z_at_center(pts, safe_z, jitter)
        self._tilt(r=True)

        # --- Diagnostic Calculations ---
        gcode = self.printer.lookup_object('gcode')
        gcode.respond_info("--- DIAGNOSTIC RESULTS ---")
        
        # Calculate the change in Z height due to each tilt, for baseline and new tool
        dZ_x_base = self._baseline['x'] - self._baseline['f']
        dZ_x_new  = z_x_new - z_f_new
        dZ_y_base = self.baseline['y'] - self._baseline['f']
        dZ_y_new  = z_y_new - z_f_new
        
        # The 'error' is the difference in deflection between the two tools
        err_x = dZ_x_new - dZ_x_base  # Error measured from Y-axis tilt (slope_x)
        err_y = dZ_y_new - dZ_y_base  # Error measured from X-axis tilt (slope_y)
        
        _dbg(gcode, "[DEBUG] SLOPE_VARS", slope_x=self.slope_x, slope_y=self.slope_y)
        _dbg(gcode, "[DEBUG] ERR_VARS", err_x=err_x, err_y=err_y)
        
        # The Z-offset is the simple difference in the flat-gantry probe height
        dz = z_f_new - self._baseline['f']
        
        # --- Final offset calculation based on the physics ---
        # A tool offset in X (dx) causes a Z change (err_x) when tilted around Y (slope_x).
        # err_x = -dx * slope_x  =>  dx = -err_x / slope_x
        #
        # A tool offset in Y (dy) causes a Z change (err_y) when tilted around X (slope_y).
        # err_y = -dy * slope_y  =>  dy = -err_y / slope_y
        
        dx_correct = -err_x / self.slope_x if self.slope_x != 0 else 0
        dy_correct = -err_y / self.slope_y if self.slope_y != 0 else 0
        
        gcode.respond_info(
            "The 'Correct' combination should match your applied SET_GCODE_OFFSET.\n"
            "If another combination matches, your steppers may be reversed/swapped.")

        results = [
            ("Correct",           dx_correct,   dy_correct),
            ("Signs-Reversed",   -dx_correct,  -dy_correct),
            ("XY-Swapped",        dy_correct,   dx_correct),
            ("XY-Swapped-Reversed", -dy_correct, -dx_correct)
        ]
        
        for label, dx, dy in results:
            gcode.respond_info(f"--> {label}: X={dx:+.4f} Y={dy:+.4f} Z={dz:+.4f}")
        
        # Store the theoretically correct result
        self._last_off = (dx_correct, dy_correct, dz)
        gcode.respond_info(f"--- DIAGNOSTICS COMPLETE ---")

    def get_status(self, eventtime):
        dx, dy, dz = self._last_off
        return {'baseline_set': bool(self._baseline),
                'last_x_result': float(dx), 'last_y_result': float(dy), 'last_z_result': float(dz)}

def load_config(config):
    return QuadGantryOffsets(config)