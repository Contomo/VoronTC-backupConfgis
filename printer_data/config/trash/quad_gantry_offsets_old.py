from __future__ import annotations

class QuadGantryOffsets:
    def __init__(self, cfg):

        self.cfg    = cfg
        self.printer= cfg.get_printer()
        self.gcode  = self.printer.lookup_object('gcode')
        # Defer all Klipper-object lookups to connect
        self.printer.register_event_handler('klippy:connect', self._on_connect)
        # Register command now; internals will be wired up in on_connect

        self.current_delta = [0, 0, 0, 0]


        self.req = self.cfg.getfloat('tilt', 1.0, above=0.0)

        self.gcode.register_command(
            'QGL_PROBE_CYCLE', self.cmd_probe_cycle,
            desc='Probe flat, +X tilt, +Y tilt; prints Zflat/Zx/Zy'
        )
        # placeholders until on_connect
        self.qgl = self.toolhead = self.probe_h = self.zhelper = None

    def _on_connect(self):

        self.qgl = self.printer.lookup_object('quad_gantry_level', None)

        if not self.qgl:
            self.printer.configfile.error('QuadGantryOffsets: missing required [quad_gantry_level] section')

        # Now that QGL exists, grab all needed helpers
        self.toolhead    = self.printer.lookup_object('toolhead')
        self.zhelper     = self.qgl.z_helper
        self.probe_h     = self.qgl.probe_helper

        # Inherit motion & probe settings from QGL & probe helper
        self.safe_z      = self.qgl.horizontal_move_z #todo grab this from QGL config instead....
        self.xy_feed     = self.probe_h.speed * 60           # mm/min
        self.lift_speed  = self.probe_h.get_lift_speed()     # mm/s

        #  four tuples (x, y) – the probe location assigned to each stepper
        qgl_points = self.probe_h.probe_points
        #  centre of those four points
        # centre of the four QGL probe-points
        mid_x = sum(p[0] for p in qgl_points) / 4.0
        mid_y = sum(p[1] for p in qgl_points) / 4.0

        self.sign_x = [+1 if p[0] > mid_x else -1 for p in qgl_points]
        self.sign_y = [+1 if p[1] > mid_y else -1 for p in qgl_points]

        # remember what tilt is currently active (start at flat)
        self.current_delta = [0, 0, 0, 0]



        # Centroid of user-defined QGL probe points
        pts = self.probe_h.probe_points
        self.cx         = sum(x for x,_ in pts) / len(pts)
        self.cy         = sum(y for _,y in pts) / len(pts)

        # Clamp requested tilt to QGL’s max_adjust
        
        self.tilt_mm    = min(self.req, self.qgl.max_adjust)

        # Stepper sign patterns from QGL’s own source


    def _probe_once(self) -> float:
        run  = self.gcode.run_script_from_command
        wait = self.toolhead.wait_moves
        run(f"G0 X{self.cx:.3f} Y{self.cy:.3f} Z{self.safe_z:.2f} F{self.xy_feed:.0f}")
        run("PROBE")      # uses your [probe] settings for speed, lift, etc.
        wait()
        z = self.toolhead.get_position()[2]
        run(f"G0 Z{self.safe_z:.2f} F{self.lift_speed*60:.0f}")
        wait()
        return z

    def _tilt(self, axis: str):
        # First, remove whatever tilt is in effect now
        if any(self.current_delta):
            self.zhelper.adjust_steppers([-d for d in self.current_delta],
                                         self.lift_speed)
            self.current_delta = [0, 0, 0, 0]

        # Then, if axis is x or y, apply the new tilt
        if axis in ('x', 'y'):
            signs  = self.sign_x if axis == 'x' else self.sign_y
            delta  = [s * self.tilt_mm for s in signs]
            self.zhelper.adjust_steppers(delta, self.lift_speed)
            self.current_delta = delta[:]      # remember what we applied

    def cmd_probe_cycle(self, gcmd):
        # If user explicitly asks, re‐level; otherwise trust an existing QGL call
        if gcmd.get_int('LEVEL', 0):
            self.gcode.run_script_from_command('QUAD_GANTRY_LEVEL')
            self.toolhead.wait_moves()

        # Flat, +X, +Y
        zf = self._probe_once()
        self._tilt('x'); zx = self._probe_once(); self._tilt('flat')
        self.toolhead.wait_moves()
        self._tilt('flat')
        self.toolhead.wait_moves()
        self._tilt('y'); zy = self._probe_once(); self._tilt('flat')

        gcmd.respond_info(f"// Zflat={zf:.5f} Zx={zx:.5f} Zy={zy:.5f}")

def load_config(cfg):
    return QuadGantryOffsets(cfg)