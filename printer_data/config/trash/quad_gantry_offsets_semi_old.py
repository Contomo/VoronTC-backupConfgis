import logging
from typing import List, Tuple, Optional
import random, numpy as np
import math


def _dbg(gcmd, label, **vals):
    """Pretty-print any number of name=value pairs via RESPOND_INFO."""
    gcmd.respond_info(f"[{label}] " + "  ".join(f"{k}={v:+.6f}" for k,v in vals.items()))



PROBE_PATTERNS = {
    1: [(0, 0)],                                   # centre
    2: [(-1, 0), (1, 0)],                          # left / right
    3: [(0, -1), (0, 0), (0, 1)],                  # front / mid / back
    4: [(-1, 0), (1, 0), (0, -1), (0, 1)],         # cross
    5: [(-1, 0), (1, 0), (0, -1), (0, 0), (0, 1)], # cross + centre
}

_param_help = """\noptionally to overwrite config defined:\n[POINTS] [SAFE_Z] [TILT] [JITTER] [F]"""

def fit_plane(points: List[Tuple[float, float, float]]):
    A = np.array([[x, y, 1] for x, y, _ in points])
    b = np.array([z for _, _, z in points])
    mx, my, c = np.linalg.lstsq(A, b, rcond=None)[0]
    return float(mx), float(my), float(c)
def z_at(p, X, Y):   mx,my,c = p;  return mx*X + my*Y + c

class QuadGantryOffsets:
    def __init__(self, config):
        self.config   = config
        self.printer  = config.get_printer()
        self.gcode    = self.printer.lookup_object('gcode')

        # User‑configurable knobs --------------------------------------
        self.jitter_mm  = config.getfloat('jitter'  , 0.1,  minval=0.0)
        self.def_pts    = config.getint  ('points'  , 1,    minval=1, maxval=5)
        self.cfg_tilt   = config.getfloat('tilt'    , 0.25, above=0.0)
        self.length_mm  = config.getfloat('length'  , None, above=0.0)
        self.safe_z_mm  = config.getfloat('safe_z'  , None, above=0.0)
        self.speed_xy   = config.getfloat('speed'   , None, above=0.0)
        self.center_x   = config.getfloat("center_x", None)
        self.center_y   = config.getfloat("center_y", None)
        self.tilt_mm    = self.cfg_tilt
        # Runtime state -------------------------------------------------
        self.sign_x: List[int] = []
        self.sign_y: List[int] = []
        self.current_delta = [0.0] * 4
        self._baseline = None
        self._last_planes = {'f': (0.0, 0.0, 0.0),'x': (0.0, 0.0, 0.0),'y': (0.0, 0.0, 0.0)}
        self._last_off = (0.0, 0.0, 0.0)

        # Event + command registration --------------------------------
        self.printer.register_event_handler('klippy:connect', self._on_connect)

        self.gcode.register_command('QGO_SLOPED_G0',        
                                    self.cmd_QGO_SLOPED_G0,
                                    desc=self.cmd_QGO_SLOPED_G0_help)
        self.gcode.register_command('QGO_TILT_GANTRY',        
                                    self.cmd_QGO_TILT_GANTRY,
                                    desc=self.cmd_QGO_TILT_GANTRY_help)
        self.gcode.register_command('QGO_CALIBRATE_BASELINE', 
                                    self.cmd_QGO_CALIBRATE_BASELINE,
                                    desc=self.cmd_QGO_CALIBRATE_BASELINE_help)
        self.gcode.register_command('QGO_CALIBRATE_OFFSET',   
                                    self.cmd_QGO_CALIBRATE_OFFSET,
                                    desc=self.cmd_QGO_CALIBRATE_OFFSET_help)
        self.gcode.register_command('QGO_CALIBRATE_OFFSET2',   
                            self.cmd_QGO_CALIBRATE_OFFSET2,
                            desc=self.cmd_QGO_CALIBRATE_OFFSET_help)
        self.gcode.register_command('QGO_CALIBRATE_OFFSET3',   
                    self.cmd_QGO_CALIBRATE_OFFSET3,
                    desc=self.cmd_QGO_CALIBRATE_OFFSET_help)

    def _on_connect(self):
        self.toolhead = self.printer.lookup_object('toolhead')
        self.qgl      = self.printer.lookup_object('quad_gantry_level', default=None)
        if(self.qgl == None):
            raise self.config.error("[quad_gantry_offsets] requires [quad_gantry_level] to be set up")
        
        self.probe_h  = self.qgl.probe_helper #its actually ProbePointsHelper
        self.zhelper  = self.qgl.z_helper

        # probe points used in QGL -----------------------------------------
        xs, ys = zip(*self.probe_h.probe_points)
        mid_x, mid_y = (min(xs) + max(xs)) / 2.0, (min(ys) + max(ys)) / 2.0
        self.sign_x = [-1 if x < mid_x else +1 for x in xs]
        self.sign_y = [-1 if y < mid_y else +1 for y in ys]
        
        # gantry corners, True lever-arm -----------------------------------
        (x0, y0), (x2, y2) = self.qgl.gantry_corners
        self._gantry_xy = [(x0, y0), (x0, y2), (x2, y2), (x2, y0)]
        gxs, gys = zip(*self._gantry_xy)
        self.span_x, self.span_y = max(gxs) - min(gxs), max(gys) - min(gys)
        self.slope_x, self.slope_y = self.tilt_mm / self.span_x, self.tilt_mm / self.span_y
        self.rot_mid_x, self.rot_mid_y = (min(gxs) + max(gxs)) / 2.0, (min(gys) + max(gys)) / 2.0

        logging.info(f"rot_x: {self.rot_mid_x}, rot_y: {self.rot_mid_y}\nslope_x: {self.slope_x}, slope_y: {self.slope_y}")
        logging.info(f"_gantry_xy: {self._gantry_xy}\nsign_x: {self.sign_x} sign_y: {self.sign_y}")

        # user config validation/selection ---------------------------------
        if (self.center_x is None) ^ (self.center_y is None):
            missing, req = (("center_x", "center_y") if self.center_x is None
                            else ("center_y", "center_x"))
            raise self.config.error(f"'{missing}' requires '{req}' to be set as well")  
        
        elif (self.center_x is None):
            self.center_x, self.center_y = mid_x, mid_y

        if (self.length_mm is None):
            self.length_mm = min(max(xs) - min(xs), max(ys) - min(ys)) / 2.0

        if (self.speed_xy is None):
            self.speed_xy = self.probe_h.speed
        self.lift_speed = self.probe_h.get_lift_speed()

        if (self.safe_z_mm is None):
            self.safe_z_mm = self.qgl.horizontal_move_z

        if (2 * self.tilt_mm > self.qgl.max_adjust):
            raise self.config.error(
                f"Requested ±{self.tilt_mm} mm per stepper would create a "
                f"{2*self.tilt_mm} mm front-to-back change, exceeding "
                f"QGL max_adjust={self.qgl.max_adjust} mm")   
    # ------------------------------------------------------------------



    def _plane_at_pivot(self, plane):
        return plane[0]*self.rot_mid_x + plane[1]*self.rot_mid_y + plane[2]

    def _params(self, gcmd, baseline:bool=False):
        pts           = gcmd.get_int  ('POINTS', self.def_pts,       minval=1,   maxval=5)
        safe_z        = gcmd.get_float('SAFE_Z', self.safe_z_mm,     above=0.0)
        tilt          = gcmd.get_float('TILT',   self.cfg_tilt,      above=0.0,  below=self.qgl.max_adjust/2)
        jitter        = gcmd.get_float('JITTER', self.jitter_mm,     minval=0.0)
        self.speed_xy = gcmd.get_float('F',      self.speed_xy * 60, above=0.0) / 60 # HACK HACK HACK
        if baseline and self.tilt_mm != tilt:
            gcmd.respond_info(f"Running following calibrations with modified tilt ({self.tilt_mm}mm -> {tilt}mm)")
            self.slope_x, self.slope_y = tilt / self.span_x, tilt / self.span_y
            self.tilt_mm = tilt
        elif self.tilt_mm != tilt:
            gcmd.respond_error(f"Baseline recorded with {self.tilt_mm}mm tilt, cannot run calibration with {tilt}mm tilt")
        return pts, safe_z, jitter
    # ------------------------------------------------------------------
    def _jit(self, x, y, r):
        return (x + random.uniform(-r, r), y + random.uniform(-r, r)) if r > 0 else (x, y)

    def _probe_at(self, x, y, safe_z, jitter):
        xj, yj = self._jit(x, y, jitter)
        self.tilted_move(x=xj, y=yj, z=safe_z, speed=self.speed_xy)
        self.gcode.run_script_from_command("PROBE")
        self.toolhead.wait_moves()
        probe_obj = self.printer.lookup_object('probe', None)
        measured_z = (probe_obj.get_status(self.printer.get_reactor().monotonic())['last_z_result']
                       if probe_obj else
                       self.printer.lookup_object('gcode_move').get_status()['gcode_position'][2])
        self.tilted_move(z=safe_z, speed=self.lift_speed)
        return measured_z

    def _probe_points(self, n, safe_z, jitter, rotate=0):
        """probe multi points in a pattern, returns results"""
        pts = []
        length = self.length_mm / 2.0
        pattern = PROBE_PATTERNS.get(n, PROBE_PATTERNS[5])
        
        for dx, dy in pattern:
            if rotate == 90:
                dx, dy = -dy, dx  # Rotate 90 degrees clockwise      
            x = self.center_x + dx*length
            y = self.center_y + dy*length
            pts.append((x, y, self._probe_at(x, y, safe_z, jitter)))
        return pts

    def _current_plane_offset(self, x, y):
        """return z diff for a given xy at current slope"""
        pts = [(gx, gy, h) for (gx, gy), h
            in zip(self._gantry_xy, self.current_delta)]
        mx, my, c = fit_plane(pts)
        #logging.info("fit_plane  mx=%+.6f  my=%+.6f  c=%+.3f  (at %4.1f,%4.1f ⇒ %+6.3f)",
        #      mx, my, c,  x, y,  mx*x + my*y + c)
        #logging.info(f"pts: {pts}\nfit_plane(pts){fit_plane(pts)}")
        return (mx*x + my*y + c)

    def _tilt(self, X=0.0, Y=0.0, r:bool=False):
        start_z = self.toolhead.get_position()[2]
        raw     = [0.5*(sy*X + sx*Y) for sx, sy in zip(self.sign_x, self.sign_y)]
        logging.info("TILT_CMD   X=%+.2f  Y=%+.2f   raw=%s", X, Y, raw)
        delta   = [raw_i - c for raw_i, c in zip(raw, self.current_delta)]
        if not any(abs(v) > 1e-6 for v in delta):
            return
        self.zhelper.adjust_steppers(delta, self.lift_speed)
        self.current_delta = raw
        if r:
            self.toolhead.manual_move([None, None, start_z], self.lift_speed)
        #if r:
        #    cur = list(self.toolhead.get_position())  # e.g. [X, Y, Z, E]
        #    cur[2] = start_z                          # only replace Z
        #    self.toolhead.set_position(cur)           # all entries are real numbers
        
    def tilted_move(self, *, x=None, y=None, z=None, speed=None):
        gcm       = self.printer.lookup_object('gcode_move')
        cx, cy, cz = self.toolhead.get_position()[:3]
        tx = cx if x is None else x
        ty = cy if y is None else y
        tz = cz if z is None else z
        # the toolhead knows where it is because it knows where it isnt, and by substzracting whre it is from where it isnt.....
        off_cur = self._current_plane_offset(cx, cy)
        off_tgt = self._current_plane_offset(tx, ty)
        logging.info("MOVE_SLOPE from (%.1f,%.1f,%.3f) → (%.1f,%.1f,%.3f)  "
                     "off_cur=%+.4f  off_tgt=%+.4f  Δoff=%+.4f",
                     cx, cy, cz, tx, ty, tz + (off_tgt - off_cur),
                     off_cur, off_tgt, off_tgt - off_cur)
        tz += (off_tgt - off_cur)
        if (x is None and y is None and abs(tz-cz) < 1e-6 and speed is None):
            return    
        speed_mm_s = speed if speed is not None else gcm.get_status()['speed']
        pos = [x, y, tz]
        self.toolhead.manual_move(pos, speed_mm_s)

    cmd_QGO_SLOPED_G0_help = ("Move with gantry-tilt compensation.\nSyntax: QGO_SLOPED_G0 [X=<mm>] [Y=<mm>] [Z=<mm>] [F=<mm/min>]")
    def cmd_QGO_SLOPED_G0(self, gcmd):
        feed = gcmd.get_float('F', None)
        self.tilted_move(
            x = gcmd.get_float('X', None),
            y = gcmd.get_float('Y', None),
            z = gcmd.get_float('Z', None),
            speed = feed / 60 if feed is not None else None
        )
            
    cmd_QGO_TILT_GANTRY_help = ("Tilt the gantry: [X=<mm>] [Y=<mm>] [RESTORE=0/1]/nRotates around provided axis, RESTORE to restore the Z position")
    def cmd_QGO_TILT_GANTRY(self, gcmd):
        X = gcmd.get_float('X', 0.0)
        Y = gcmd.get_float('Y', 0.0)
        R = gcmd.get_int('R', 0)
        self._params(gcmd)  # handles optional SPEED
        self._tilt(X, Y, bool(R))
        gcmd.respond_info(f"Tilt applied  X={X:+.3f}  Y={Y:+.3f}")

    cmd_QGO_CALIBRATE_BASELINE_help = ("Generate a baseline to compare against with other tools" + _param_help)
    def cmd_QGO_CALIBRATE_BASELINE(self, gcmd):
        n, safe_z, jitter = self._params(gcmd, baseline=True)

        self._tilt(0.0, 0.0)
        flat = self._probe_points(n, safe_z, jitter)
        self._tilt(X=self.tilt_mm)
        x_tilt = self._probe_points(n, safe_z, jitter, rotate=90)
        self._tilt(Y=self.tilt_mm)
        y_tilt = self._probe_points(n, safe_z, jitter)
        self._tilt(0.0, 0.0)
        
        # ------------------------------------------------------------------
        # Store a reference “flat” plane and the two “tilted” planes
        # plus the *expected* ΔZ that the reference tool experiences when
        # we pitch (X-tilt) or roll (Y-tilt) the gantry.  These numbers
        # are what let us back-solve ΔX and ΔY later.
        # ------------------------------------------------------------------
        self._baseline = {
            'f': fit_plane(flat),
            'x': fit_plane(x_tilt),
            'y': fit_plane(y_tilt),
        }

        self.pitch_slope = (self._plane_at_pivot(self._baseline['x'])
                   - self._plane_at_pivot(self._baseline['f']))   # reacts to dY
        self.roll_slope  = (self._plane_at_pivot(self._baseline['y'])
                        - self._plane_at_pivot(self._baseline['f']))   # reacts to dX
        
        # ΔZ that *T0* sees when we pitch      (around X → slope along ±Y)
        bx  = self._plane_at_pivot(self._baseline['x']) \
            - self._plane_at_pivot(self._baseline['f'])
        # ΔZ that *T0* sees when we roll       (around Y → slope along ±X)
        by  = self._plane_at_pivot(self._baseline['y']) \
            - self._plane_at_pivot(self._baseline['f'])

        self._baseline['dz_pitch'] = bx    #   will be ~0 if pivot is centre
        self._baseline['dz_roll']  = by

        self._last_planes = dict(self._baseline)      # UI / STATUS cache
        self._last_off   = (0.0, 0.0, 0.0)

        f_p = self._plane_at_pivot(self._baseline['f'])
        x_p = self._plane_at_pivot(self._baseline['x'])
        y_p = self._plane_at_pivot(self._baseline['y'])
        pitch_slope = x_p - f_p    # reaction to a Y-offset
        roll_slope  = y_p - f_p    # reaction to an X-offset
        gcmd.respond_info(
            f"[DBG0] pitch_slope={pitch_slope:+.6f}  roll_slope={roll_slope:+.6f}"
        )
    
    cmd_QGO_CALIBRATE_OFFSET_help = ("compare against baseline, prints dX/dY/dZ and updates status" + _param_help)
    def cmd_QGO_CALIBRATE_OFFSET(self, gcmd):
        if self._baseline is None:
            gcmd.respond_error("Run QGO_CALIBRATE_BASELINE first")
            return
        n, safe_z, jitter = self._params(gcmd)          # honours POINTS=… JITTER=… TILT=…

        # ── 1 ── probe three planes with current tool ───────────────────────
        self._tilt(0, 0)
        flat_p  = fit_plane(self._probe_points(n, safe_z, jitter))

        self._tilt(X=self.tilt_mm)          # pitch – changes slope along Y
        x_p     = fit_plane(self._probe_points(n, safe_z, jitter, rotate=90))

        self._tilt(Y=self.tilt_mm)          # roll  – changes slope along X
        y_p     = fit_plane(self._probe_points(n, safe_z, jitter))

        self._tilt(0, 0)

        # ── 2 ── evaluate planes at gantry pivot ────────────────────────────
        px, py = self.rot_mid_x, self.rot_mid_y
        def Z(p): a,b,c = p; return a*px + b*py + c

        f0, x0, y0 = (self._baseline[k] for k in ('f','x','y'))
        dz_flat   =  Z(flat_p) - Z(f0)
        dz_pitch  = (Z(x_p) - Z(flat_p)) - (Z(x0) - Z(f0))   # reacts to  ΔY
        dz_roll   = (Z(y_p) - Z(flat_p)) - (Z(y0) - Z(f0))   # reacts to  ΔX

        # slopes (already signed) – re-compute in case user overrode TILT=
        slope_x = self._baseline['y'][0] - self._baseline['f'][0]   # roll-tilt gradient
        slope_y = self._baseline['x'][1] - self._baseline['f'][1]

        # ── 3 ── solve offsets  (note the minus signs) ──────────────────────
        dX =  dz_roll  / slope_x               # roll  → X
        dY =  dz_pitch / slope_y               # pitch → Y
        dZ =  dz_flat  - (f0[0]*dX + f0[1]*dY)

        # ── 4 ── cache + big debug dump ─────────────────────────────────────
        self._last_planes = {'f': flat_p, 'x': x_p, 'y': y_p}
        self._last_off    = (dX, dY, dZ)

        # raw numbers
        _dbg(gcmd, "DBG",
            px=px, py=py,
            slope_x=slope_x, slope_y=slope_y,
            dz_flat=dz_flat, dz_pitch=dz_pitch, dz_roll=dz_roll,
            dX=dX, dY=dY, dZ=dZ)

        gcmd.respond_info(f"Offset solved →  dX={dX:+.3f}  dY={dY:+.3f}  dZ={dZ:+.3f}")


    def cmd_QGO_CALIBRATE_OFFSET3(self, gcmd):
        if self._baseline is None:
            gcmd.respond_error("Run QGO_CALIBRATE_BASELINE first")
            return
        
        # Get parameters and pivot point
        n, safe_z, jitter = self._params(gcmd)
        pivot_x, pivot_y = self.rot_mid_x, self.rot_mid_y
        
        # Current measurements
        self._tilt(0.0, 0.0)
        flat_plane = fit_plane(self._probe_points(n, safe_z, jitter))
        Z_flat = z_at(flat_plane, pivot_x, pivot_y)
        
        self._tilt(X=self.tilt_mm)  # X-tilt (pitch)
        x_plane = fit_plane(self._probe_points(n, safe_z, jitter))
        Z_x = z_at(x_plane, pivot_x, pivot_y)
        
        self._tilt(Y=self.tilt_mm)  # Y-tilt (roll)
        y_plane = fit_plane(self._probe_points(n, safe_z, jitter))
        Z_y = z_at(y_plane, pivot_x, pivot_y)
        
        self._tilt(0.0, 0.0)

        # Baseline references at pivot
        base_f = z_at(self._baseline['f'], pivot_x, pivot_y)
        base_x = z_at(self._baseline['x'], pivot_x, pivot_y)
        base_y = z_at(self._baseline['y'], pivot_x, pivot_y)

        # Calculate expected vs actual tilt responses
        expected_pitch_slope = (base_x - base_f) / self.tilt_mm  # Z/mm of X-tilt
        expected_roll_slope = (base_y - base_f) / self.tilt_mm   # Z/mm of Y-tilt
        
        actual_pitch_response = (Z_x - Z_flat)
        actual_roll_response = (Z_y - Z_flat)

        # The key insight: Each mm of X offset should change pitch response by expected_pitch_slope*(Y_offset/span_y)
        # Similarly for Y offset and roll response
        Δy = (actual_pitch_response - (base_x - base_f)) / (expected_pitch_slope * (self.span_y/2))
        Δx = (actual_roll_response - (base_y - base_f)) / (expected_roll_slope * (self.span_x/2))
        Δz = base_f - Z_flat

        _dbg(gcmd, "KINEMATIC", 
                base_f=base_f, base_x=base_x, base_y=base_y,
                Z_flat=Z_flat, Z_x=Z_x, Z_y=Z_y,
                expected_pitch_slope=expected_pitch_slope,
                expected_roll_slope=expected_roll_slope,
                actual_pitch_response=actual_pitch_response,
                actual_roll_response=actual_roll_response)
        
        self._last_off = (Δx, Δy, Δz)
        gcmd.respond_info(
            f"Offset: ΔX={Δx:+.3f}  ΔY={Δy:+.3f}  ΔZ={Δz:+.3f}\n"
            f"Kinematic Solution:\n"
            f"  Pitch: expected ΔZ={base_x-base_f:.3f}  actual ΔZ={actual_pitch_response:.3f}\n"
            f"  Roll:  expected ΔZ={base_y-base_f:.3f}  actual ΔZ={actual_roll_response:.3f}"
        )

    def cmd_QGO_CALIBRATE_OFFSET2(self, gcmd):
        if self._baseline is None:
            gcmd.respond_error("Run QGO_CALIBRATE_BASELINE first")
            return
        
        # Get parameters and pivot point
        n, safe_z, jitter = self._params(gcmd)
        pivot_x, pivot_y = self.rot_mid_x, self.rot_mid_y
        
        # Current measurements
        self._tilt(0.0, 0.0)
        flat_plane = fit_plane(self._probe_points(n, safe_z, jitter))
        Z_flat = z_at(flat_plane, pivot_x, pivot_y)
        
        self._tilt(X=self.tilt_mm)
        x_plane = fit_plane(self._probe_points(n, safe_z, jitter))
        Z_x = z_at(x_plane, pivot_x, pivot_y)
        
        self._tilt(Y=self.tilt_mm)
        y_plane = fit_plane(self._probe_points(n, safe_z, jitter))
        Z_y = z_at(y_plane, pivot_x, pivot_y)
        
        self._tilt(0.0, 0.0)

        # Baseline references at pivot
        base_f = z_at(self._baseline['f'], pivot_x, pivot_y)
        base_x = z_at(self._baseline['x'], pivot_x, pivot_y)
        base_y = z_at(self._baseline['y'], pivot_x, pivot_y)

        # PHYSICS-BASED CALCULATION -------------------------------------------
        expected_dz_dx = (base_x - base_f)  # Total Z change for full X tilt
        expected_dz_dy = (base_y - base_f)  # Total Z change for full Y tilt
        
        # 2. Measure actual behavior
        actual_dz_dx = (Z_x - Z_flat)
        actual_dz_dy = (Z_y - Z_flat)
        
        slope_x = self._baseline['y'][0] - self._baseline['f'][0]
        slope_y = self._baseline['x'][1] - self._baseline['f'][1]

        # 3. Calculate offsets (cross terms)
        Δx = (actual_dz_dy - expected_dz_dy) / slope_x      # roll  → X
        Δy = (actual_dz_dx - expected_dz_dx) / slope_y      # pitch → Y
        Δz = base_f - Z_flat

        _dbg(gcmd, "PHYSICS", 
                base_f=base_f, base_x=base_x, base_y=base_y,
                Z_flat=Z_flat, Z_x=Z_x, Z_y=Z_y,
                expected_dz_dx=expected_dz_dx, actual_dz_dx=actual_dz_dx,
                expected_dz_dy=expected_dz_dy, actual_dz_dy=actual_dz_dy,
                span_x=self.span_x, span_y=self.span_y, tilt_mm=self.tilt_mm)
        
        self._last_off = (Δx, Δy, Δz)
        gcmd.respond_info(
            f"Offset: ΔX={Δx:+.3f}  ΔY={Δy:+.3f}  ΔZ={Δz:+.3f}\n"
            f"Mechanics:\n"
            f"  X Slope: expected={expected_dz_dx:.6f}/mm  actual={actual_dz_dx:.6f}/mm\n"
            f"  Y Slope: expected={expected_dz_dy:.6f}/mm  actual={actual_dz_dy:.6f}/mm\n"
            f"  Span X: {self.span_x}mm  Span Y: {self.span_y}mm"
        )

    def get_status(self, _eventtime):
        dx, dy, dz = self._last_off
        return {'baseline_set': bool(self._baseline),
                'last_x_result': dx, 
                'last_y_result': dy, 
                'last_z_result': dz,
                'planes': self._last_planes
                }

def load_config(config):
    return QuadGantryOffsets(config)
