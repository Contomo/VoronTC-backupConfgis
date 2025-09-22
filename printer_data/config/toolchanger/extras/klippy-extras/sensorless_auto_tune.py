from typing import Dict, Any

AXES: Dict[str, int] = {'X': 0, 'Y': 1, 'Z': 2}

class SensorlessAutoTune:
    def __init__(self, config):
        self.printer  = config.get_printer()
        self.gcode    = self.printer.lookup_object('gcode')
        self.toolhead = None
        self.kin      = None

        self._p1_baseline = {}   # axis -> d_base travel seen at first OK
        self._p2_arm   = None    # arming record for current homing
        self._status   = {}      # axis -> last result bundle

        self._p2_dvec = None               # tool-space delta to FIRST TRIGGER (X,Y,Z)
        self._p2_base_kin_spos = None      # FULL commanded map captured BEFORE any bias

        self.restore_speed = config.getfloat('speed', 100, above=0.0)

        # Wiring
        self.gcode.register_command('SENSORLESS_AUTOTUNE',
                                    self.cmd_SENSORLESS_AUTOTUNE,
                                    self.cmd_SENSORLESS_AUTOTUNE_help)
        self.printer.register_event_handler('klippy:connect', self._on_connect)
        self.printer.register_event_handler('homing:homing_move_begin', self._on_hmove_begin)
        self.printer.register_event_handler('homing:homing_move_end',   self._on_hmove_end)

    # ---- Event handlers ---------------------------------------------------

    def _on_connect(self):
        self.toolhead = self.printer.lookup_object('toolhead')
        self.kin      = self.toolhead.get_kinematics()

    # --- homing hooks ---
    def _on_hmove_begin(self, hmove, *args):
        arm = self._p2_arm
        if not arm:
            return
        try:
            es_names = {s.get_name() for es in hmove.get_mcu_endstops() for s in es.get_steppers()}
        except Exception:
            es_names = set()
        if not es_names.intersection(arm['stepper_names']):
            return

        kin = self.kin
        self._p2_base_kin_spos = {s.get_name(): s.get_commanded_position() for s in kin.get_steppers()}
        self._p2_dvec = None
        if arm.get('capture_only'):
            return
        cur = list(self.toolhead.get_position())
        cur[arm['ax']] = float(arm['startpos'])
        self.toolhead.set_position(cur)

    def _on_hmove_end(self, hmove, *args):
        arm = self._p2_arm
        if not arm:
            return
        stepper_positions = getattr(hmove, 'stepper_positions', None)
        if not stepper_positions:
            return
        hm_names = {sp.stepper.get_name() for sp in stepper_positions}
        if not hm_names.intersection(arm['stepper_names']):
            return  # don't disarm on a miss
        kin = self.kin
        base = self._p2_base_kin_spos or {s.get_name(): s.get_commanded_position()
                                        for s in kin.get_steppers()}
        kin_s1 = dict(base)
        for sp in stepper_positions:
            s = sp.stepper; n = s.get_name()
            d_steps = float(sp.trig_pos - sp.start_pos)
            d_mm    = d_steps * float(s.get_step_dist())
            kin_s1[n] = base[n] + d_mm

        p0 = kin.calc_position(base)
        p1 = kin.calc_position(kin_s1)
        self._p2_dvec = [(b or 0.0) - (a or 0.0) for a, b in zip(p0, p1)]
        self._p2_arm = None
        self._p2_base_kin_spos = None


    def _axis_stepper_names(self, ax: int):
        rail = self.kin.rails[ax]
        return {s.get_name() for s in rail.get_steppers()}

    def _arm_capture(self, axis: str):
        ax   = AXES[axis]
        stepper_names = self._axis_stepper_names(ax)
        return {'ax': ax, 'stepper_names': stepper_names, 'capture_only': True}

    def _arm_home_window(self, axis: str, window: float, *, mode: str='abs'):
        """Plan a protective shorter first-approach for P2.
        mode='abs'  -> total = |window|
        mode='plus' -> total = |window| + d_base (P1 baseline)
        """
        ax   = AXES[axis]
        rail = self.kin.rails[ax]
        hi   = rail.get_homing_info()
        sgn  = 1.0 if hi.positive_dir else -1.0
        end  = float(hi.position_endstop)
        d_base = abs(float(self._p1_baseline.get(axis, 0.0)))
        total  = abs(window) if mode == 'abs' else abs(window) + d_base
        start  = end - sgn * total
        stepper_names = self._axis_stepper_names(ax)
        return {'ax': ax, 'startpos': float(start), 'stepper_names': stepper_names}

    def _snapshot_homed_axes(self) -> set[str]:
        st = self.toolhead.get_status(self.printer.get_reactor().monotonic())
        return set((st.get('homed_axes') or ""))

    # ---- one-shot homing & measure ----------------------------------------

    def _home_once_and_measure(self, axis: str, window: float|None, *, window_mode: str='plus', gcmd):
        ax = AXES[axis]
        self._p2_arm = self._arm_capture(axis) if window is None else self._arm_home_window(axis, window, mode=window_mode)
        err_msg = None
        pre_homed_axes = self._snapshot_homed_axes()  
        try:
            self.gcode.run_script_from_command(f"G28 {axis}")
        except self.printer.command_error as e:
            err_msg = str(e)

        self.toolhead.wait_moves()
        dvec = self._p2_dvec
        if dvec is None:
            if err_msg:
                return 0.0, err_msg
            raise gcmd.error("internal error: No distance captured from homing move")

        distance = float(dvec[ax])
        self._p2_dvec = self._p2_arm = None

        if abs(distance) > 1e-9:
            post = self.toolhead.get_position()
            if err_msg:
                self.toolhead.set_position(post, homing_axes=pre_homed_axes)
            target = float(post[ax] - dvec[ax])
            rail = self.kin.rails[ax]
            rmin, rmax = rail.get_range()
            if not (rmin <= target <= rmax):
                raise gcmd.error('internal error: cannot restore position: %.2f\u2009<=\u2009%.2f\u2009<=\u2009%.2f' 
                                % (rmin, target, rmax))
            vec = [None] * len(post)
            vec[ax] = target
            self.toolhead.manual_move(vec, self.restore_speed)
            self.toolhead.wait_moves()
        return distance, err_msg

    # ---- Pre-flight check -------------------------------------------------

    def _run_preflight_check(self, sgh, gcmd):
        sgh.bump(-999)
        moved, err = self._home_once_and_measure(sgh.axis, window=10.0, window_mode='abs', gcmd=gcmd)
        if err and 'No trigger' in err:
            raise gcmd.error("Pre-flight FAILED: sensorless did not trigger at max sensitivity; "
                             "check DIAG wiring and mechanics.")
        self.toolhead.wait_moves()

    # ---- G-code command ---------------------------------------------------

    cmd_SENSORLESS_AUTOTUNE_help = """SENSORLESS_AUTOTUNE AXIS=<X|Y|Z>
                                      [MIN_MOVE=0.20] [WINDOW=0.60]
                                      [START=<int>] [STOP=<int>]"""

    def cmd_SENSORLESS_AUTOTUNE(self, gcmd):
        axis = gcmd.get('AXIS', '').upper()
        if not axis or axis[0] not in AXES:
            raise gcmd.error("Specify AXIS=X|Y|Z (got %r)" % (axis,))

        sgh   = AxisStallGuard(self.printer, axis, gcmd=gcmd)
        field = sgh.info['field']
        more  = sgh.info['more_sensitive_step']
        vmin, vmax = sgh.info['value_min'], sgh.info['value_max']
        most_sensitive, least_sensitive = (vmin, vmax) if more > 0 else (vmax, vmin)

        min_move = gcmd.get_float('MIN_MOVE', 0.20, above=0.0)
        window   = gcmd.get_float('WINDOW',   0.60, minval=0.1)
        start    = gcmd.get_int('START', most_sensitive)
        stop     = gcmd.get_int('STOP',  least_sensitive)

        legal_min, legal_max = min(vmin, vmax), max(vmin, vmax)
        if not (legal_min <= start <= legal_max) or not (legal_min <= stop <= legal_max):
            raise gcmd.error("START/STOP out of range (got %d,\u2009%d)\u2009—\u2009legal: [%d,\u2009%d]."
                             % (start, stop, legal_min, legal_max))

        if (more > 0 and start < stop) or (more < 0 and start > stop):
            raise gcmd.error("Invalid START/STOP ordering. Begin near most sensitive (%d) ⇒ sweep to %d."
                             % (most_sensitive, least_sensitive))

        self._run_preflight_check(sgh, gcmd)
        gcmd.respond_info("[P1] Testing: %d\u2009>>>\u2009%d" % (start, stop))

        # -------- Phase 1: find first OK --------
        cur = sgh.set(start)
        first_ok = None
        distance_to_home = None
        while True:
            moved, err = self._home_once_and_measure(axis, window=None, gcmd=gcmd)
            if err:
                raise gcmd.error("[P1] Homing errored.")
            if abs(moved) >= min_move:
                first_ok, distance_to_home = cur, abs(moved)
                gcmd.respond_info("[P1] try: %d, moved: %.2f\u2009mm (success)" % (cur, abs(moved)))
                break
            if cur == stop:
                break
            gcmd.respond_info("[P1] try: %d, moved: %.2f\u2009mm" % (cur, abs(moved)))
            cur = sgh.bump(+1)

        if first_ok is None:
            raise gcmd.error("No working value found within sweep.")

        self._p1_baseline[axis] = float(distance_to_home)

        # -------- Phase 2: push until first FAIL --------
        gcmd.respond_info("[P2] Testing: (%d\u2009>>>\u2009%d)" % (first_ok, stop))
        last_ok = first_ok
        while True:
            if cur == stop:
                break
            cur = sgh.bump(+1)
            moved, err = self._home_once_and_measure(axis, window=window, window_mode='plus', gcmd=gcmd)
            expected_min = max(0.0, distance_to_home - window)
            if not err and (abs(moved) >= expected_min):
                gcmd.respond_info("[P2] try: %d: moved: %.2f\u2009mm" % (cur, abs(moved)))
                last_ok = cur
                continue
            if abs(moved) < expected_min:
                raise gcmd.error("[P2] Triggered too early try: %d (%.2f\u2009mm < %.2f\u2009mm)" % (cur, abs(moved), expected_min))
            break

        # -------- Done --------
        v_lo, v_hi = (first_ok, last_ok) if first_ok <= last_ok else (last_ok, first_ok)
        recommend = v_lo + (v_hi - v_lo) // 2
        sgh.set(recommend)
        gcmd.respond_info("Done testing %s\nRecommended value: %s=%d\nWorking range: %d <-> %d" % 
                          (sgh._stepper, field.upper(), recommend, v_lo, v_hi))
        self._status[axis] = {'min_ok': v_lo, 'max_ok': v_hi, 'stepper': sgh._stepper}

    def get_status(self, eventtime):
        return dict(self._status)



class AxisStallGuard:
    def __init__(self, printer, axis: str, stepper_name: str = None, gcmd=None):
        self.printer = printer
        self.gcode   = printer.lookup_object('gcode')
        self.axis    = axis.upper()
        self.gcmd    = gcmd
        kin = printer.lookup_object('toolhead').get_kinematics()
        self._stepper = stepper_name or self._resolve_stepper_for_axis(self.axis, kin, gcmd)
        self._tmc    = self._get_tmc_for_stepper(self._stepper, gcmd)
        self.info    = self._compute_field_info(self._tmc, gcmd)

    # --- public API ---
    def set(self, value: int):
        v = self._clamp(value)
        self.gcode.run_script_from_command(
            f"SET_TMC_FIELD STEPPER={self._stepper} FIELD={self.info['field']} VALUE={int(v)}"
        )
        return v

    def bump(self, step: int = 1):
        cur = self.read()
        delta = -1 * self.info['more_sensitive_step'] * int(step)
        return self.set(cur + delta)

    def read(self) -> int:
        return int(self._tmc.fields.get_field(self.info['field']))

    # --- internals ---
    def _err(self, gcmd, msg: str):
        if gcmd is not None:
            raise gcmd.error(msg)
        raise self.printer.command_error(msg)

    def _resolve_stepper_for_axis(self, axis: str, kin, gcmd=None) -> str:
        ax   = AXES[axis]
        rail = kin.rails[ax]
        for _, name in rail.get_endstops():
            try:
                self._get_tmc_for_stepper(name, gcmd)
                return name
            except Exception:
                continue
        _ = self._get_tmc_for_stepper('stepper_' + axis.lower(), gcmd)
        return 'stepper_' + axis.lower()

    def _get_tmc_for_stepper(self, stepper_suffix: str, gcmd=None):
        for name, obj in self.printer.lookup_objects():
            if name.startswith('tmc') and name.endswith(stepper_suffix):
                return obj
        self._err(gcmd, f"No TMC driver found for '{stepper_suffix}'")

    def _compute_field_info(self, tmc_obj, gcmd=None) -> Dict[str, Any]:
        fields = tmc_obj.fields
        field = ("sgthrs" if fields.lookup_register("sgthrs", None) is not None
                 else "sgt"  if fields.lookup_register("sgt",  None) is not None
                 else None)
        if field is None:
            self._err(gcmd, "Driver lacks a StallGuard threshold field")

        reg   = fields.lookup_register(field)
        mask  = fields.all_fields[reg][field]
        shift = (mask & -mask).bit_length() - 1
        width = (mask >> shift).bit_length()

        if field in fields.signed_fields:
            value_min = -(1 << (width - 1))
            value_max =  (1 << (width - 1)) - 1
            more_sensitive_step = -1
            signed = True
        else:
            value_min = 0
            value_max = (1 << width) - 1
            more_sensitive_step = +1
            signed = False

        return {
            'field': field,
            'value_min': value_min,
            'value_max': value_max,
            'more_sensitive_step': more_sensitive_step,
            'signed': signed,
        }

    def _clamp(self, v: int) -> int:
        if v < self.info['value_min']:
            return self.info['value_min']
        if v > self.info['value_max']:
            return self.info['value_max']
        return int(v)


def load_config(config):
    return SensorlessAutoTune(config)
