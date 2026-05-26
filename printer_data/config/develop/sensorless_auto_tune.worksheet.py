from __future__ import annotations

from typing import List, Dict, Optional, Any, Iterator, Tuple
from contextlib import contextmanager
from dataclasses import dataclass, field
import inspect, math


try:
    from klippy import homing  # Kalico
except ImportError:
    from . import homing  # Klipper


TRINAMIC_DRIVERS: List[str] = ["tmc2130", "tmc2208", "tmc2209", "tmc2240", "tmc2660", "tmc5160"]
AXIS_TO_INDEX: Dict[str, int] = {"X": 0, "Y": 1, "Z": 2, "x": 0, "y": 1, "z": 2}

PREFLIGHT_DISTANCE_MAX_MOVE = 10.0


def _linspace(a: float, b: float, steps: int) -> List[float]:
    if steps <= 1:
        return [0.5 * (a + b)]
    out = []
    for i in range(steps):
        t = i / float(steps - 1)
        out.append(a + (b - a) * t)
    return out


@dataclass(frozen=True)
class HomeResult:
    triggered: bool
    moved_mm: float

@dataclass
class TunePoint:
    sgt: int
    current: float
    baseline: float
    threshold: float
    runs: int = 0
    no_trigger: int = 0
    early: int = 0
    moved_wall: List[float] = field(default_factory=list)  # moved_mm values (non-early, triggered)

    def stats(self, wall_target: float) -> Tuple[Optional[float], Optional[float], Optional[float], Optional[float]]:
        vals = self.moved_wall
        n = len(vals)
        if n == 0:
            return None, None, None, None
        mean = sum(vals) / float(n)
        if n >= 2:
            var = sum((x - mean) * (x - mean) for x in vals) / float(n - 1)
            std = math.sqrt(var)
        else:
            std = 0.0
        errs = [abs(x - wall_target) for x in vals]
        mean_e = sum(errs) / float(n)
        if n >= 2:
            var_e = sum((e - mean_e) * (e - mean_e) for e in errs) / float(n - 1)
            std_e = math.sqrt(var_e)
        else:
            std_e = 0.0
        return mean, std, mean_e, std_e

class SensorlessAutoTune:
    def __init__(self, config):
        self.config = config
        self.printer = config.get_printer()
        self.gcode = self.printer.lookup_object("gcode")

        self.stepper_name = config.get_name().split(None, 1)[-1]
        if not config.has_section(self.stepper_name):
            raise config.error("Could not find stepper config section '[%s]'" % (self.stepper_name,))

        self.axis = config.get("axis", self.stepper_name[-1])
        if self.axis not in AXIS_TO_INDEX:
            raise config.error("x/y/z only for now.")

        self.tmc_name = None
        for driver in TRINAMIC_DRIVERS:
            sec = "%s %s" % (driver, self.stepper_name)
            if config.has_section(sec):
                self.tmc_name = sec
                break
        if self.tmc_name is None:
            raise config.error("Could not find any TMC driver config section for '%s'" % (self.stepper_name,))
            
        _cur_defaults = self._get_current_defaults(config, self.tmc_name)
        self.home_current_min = config.getfloat(
            "home_current_min", _cur_defaults[0], minval=0.0
        )
        self.home_current_max = config.getfloat(
            "home_current_max", _cur_defaults[1], minval=self.home_current_min
        )
        self.home_current_steps = config.getint(
            "home_current_steps", 1, minval=1
        )

        self.runs_per_test = config.getint("runs_per_test", 1, minval=1)

        self.overtravel_window = config.getfloat("overtravel_window", 0.25, minval=0.0)
        self.restore_speed = config.getfloat("restore_speed", 100.0, above=0.0)

        self.stall_min = config.getint("stall_min", None)
        self.stall_max = config.getint("stall_max", None)

        self.gcode.register_mux_command(
            "SENSORLESS_AUTOTUNE",
            "AXIS",
            self.axis,
            self.cmd_SENSORLESS_AUTOTUNE,
            desc=self.cmd_SENSORLESS_AUTOTUNE_help,
        )

        self.gcode.register_mux_command(
            "SENSORLESS_AUTOTUNE_FIND_CONSTRAINTS",
            "AXIS",
            self.axis,
            self.cmd_SENSORLESS_AUTOTUNE_FIND_CONSTRAINTS,
            desc=self.cmd_SENSORLESS_AUTOTUNE_FIND_CONSTRAINTS_help,
        )

        self.printer.register_event_handler("klippy:connect", self._on_connect)
        self.printer.register_event_handler("homing:homing_move_end", self._on_hmove_end)
        self.printer.register_event_handler("homing:homing_move_begin", self._on_hmove_begin)

        self._hm_kin_spos0: Optional[Dict[str, Any]] = None
        self._hm_last_dvec = None

    def _get_current_defaults(self, config, tmc_name):
        tmc_cfg  = config.getsection(tmc_name)
        home_cur = tmc_cfg.getfloat("home_current", None, note_valid=False)
        run_cur  = tmc_cfg.getfloat("run_current",  1.0, note_valid=False)
        if home_cur is not None:
            return (0.80 * home_cur, 1.20 * home_cur)
        return (0.60 * run_cur, run_cur)


    def _on_connect(self):
        self.toolhead = self.printer.lookup_object("toolhead")
        self.kin = self.toolhead.get_kinematics()

        self.tmc_object = self.printer.lookup_object(self.tmc_name)
        self.stall_manager = StallField.from_cmdhelper(self.tmc_object.get_status.__self__, self.toolhead)
        
        self.stall_min = self.config.getint(
            "stall_min",
            self.stall_manager.value_min,
            minval=self.stall_manager.value_min,
            maxval=self.stall_manager.value_max,
        )
        self.stall_max = self.config.getint(
            "stall_max",
            self.stall_manager.value_max,
            minval=self.stall_manager.value_min,
            maxval=self.stall_manager.value_max,
        )


    def _on_hmove_begin(self, hmove, *args):
        self._hm_kin_spos0 = { s.get_name(): s.get_commanded_position() 
                               for s in self.kin.get_steppers() }

    def _on_hmove_end(self, hmove, *args):
        spos0, self._hm_kin_spos0 = self._hm_kin_spos0, None
        if spos0 is None or self._hm_last_dvec is not None:
            return

        trig_steps = {sp.stepper_name: int(sp.trig_pos - sp.start_pos)
                      for sp in hmove.stepper_positions}

        p0 = self.kin.calc_position(spos0)
        p1 = hmove.calc_toolhead_pos(spos0, trig_steps)
        self._hm_last_dvec = [b - a for a, b in zip(p0, p1)]

    def home_once_and_measure(self, max_distance: Optional[float] = None, *, reverse_dir: bool = False) -> HomeResult:
        ax = AXIS_TO_INDEX[self.axis]
        rail = self.kin.rails[ax]
        hi = rail.get_homing_info()

        pos_min, pos_max = rail.get_range()
        if max_distance is None:
            max_distance = 1.5 * (pos_max - pos_min)
        positive_dir_eff = hi.positive_dir ^ reverse_dir

        position_target = pos_max if positive_dir_eff else pos_min

        npos = len(self.toolhead.get_position())
        homepos = [None] * npos
        homepos[ax] = position_target

        start = position_target - (1.0 if positive_dir_eff else -1.0) * max_distance  # type: ignore
        forcepos = [None] * npos
        forcepos[ax] = start

        self._hm_kin_spos0 = None
        self._hm_last_dvec = None

        triggered = True
        with self._preserve_position(self.restore_speed):
            try:
                hs = homing.Homing(self.printer)
                hs.set_axes([ax])
                hs.home_rails([rail], forcepos, homepos)
            except self.printer.command_error as e:
                msg = str(e)
                if "No trigger on" in msg:
                    triggered = False
                elif self._hm_last_dvec is not None:
                    triggered = True
                else:
                    raise

        if (not triggered) or (self._hm_last_dvec is None):
            result = HomeResult(False, 0.0)
        else:
            moved = abs(float(self._hm_last_dvec[ax]))
            result = HomeResult(True, moved if moved > 0.0 else 0.0)

        self._hm_last_dvec = None
        return result

    cmd_SENSORLESS_AUTOTUNE_help = """
    """
    def cmd_SENSORLESS_AUTOTUNE(self, gcmd):
        overtravel_window = gcmd.get_float("OVERTRAVEL_WINDOW", self.overtravel_window, minval=0.0)
        runs_per_test = gcmd.get_int("RUNS_PER_TEST", self.runs_per_test, minval=1)

        home_current_min = gcmd.get_float("HOME_CURRENT_MIN", self.home_current_min, minval=0.0)
        home_current_max = gcmd.get_float("HOME_CURRENT_MAX", self.home_current_max, minval=0.0)
        home_current_steps = gcmd.get_int("HOME_CURRENT_STEPS", self.home_current_steps, minval=1)

        stall_min = gcmd.get_int(
            "STALL_MIN", self.stall_min,
            minval=self.stall_manager.value_min,
            maxval=self.stall_manager.value_max
        )
        stall_max = gcmd.get_int(
            "STALL_MAX", self.stall_max,
            minval=stall_min,
            maxval=self.stall_manager.value_max
        )
        sgt_vals = self.stall_manager.ordered_least_to_most_sensitive(stall_min, stall_max)
        sgt_vals.reverse()

        currents = _linspace(home_current_min, home_current_max, home_current_steps)

        with self.stall_manager.temporary(self.stall_manager.max_sensitive):
            pre = self.home_once_and_measure(max_distance=PREFLIGHT_DISTANCE_MAX_MOVE)
        if not pre.triggered:
            raise gcmd.error(
                "Precheck: stallguard did not trigger within %.1fmm at max sensitivity."
                % (PREFLIGHT_DISTANCE_MAX_MOVE,)
            )

        baseline = float(pre.moved_mm)
        margin = min(10.0, max(baseline * 0.5, 2.0))
        threshold = baseline + margin

        gcmd.respond_info(
            "Running sensorless autotune grid:\n"
            "  SGT:     %d..%d (%d)\n"
            "  Current: %.3f..%.3f (%d)\n"
            "  Runs/pt: %d\n"
            "  Baseline: %.3fmm  Margin: %.3fmm  Threshold: %.3fmm\n"
            "  Overtravel: %.3fmm"
            % (
                stall_min, stall_max, len(sgt_vals),
                currents[0], currents[-1], len(currents),
                runs_per_test,
                baseline, margin, threshold,
                overtravel_window,
            )
        )

        def _run_point(sgt: int, cur: float) -> TunePoint:
            pt = TunePoint(int(sgt), float(cur), baseline, threshold)

            wall_dist: Optional[float] = None

            with self._temporary_home_current(cur):
                # 1) First: discover wall (full-range) once
                with self.stall_manager.temporary(int(sgt)):
                    pt.runs += 1
                    r0 = self.home_once_and_measure()

                if not r0.triggered:
                    pt.no_trigger += 1
                    return pt

                moved0 = float(r0.moved_mm)

                # Early trigger: don't waste repeats on this point
                if moved0 <= threshold:
                    pt.early += 1
                    return pt

                wall_dist = moved0
                pt.moved_wall.append(moved0)

                for _ in range(runs_per_test - 1):
                    with self.stall_manager.temporary(int(sgt)):
                        pt.runs += 1
                        r = self.home_once_and_measure(max_distance=wall_dist + overtravel_window)

                    if not r.triggered:
                        pt.no_trigger += 1
                        break

                    moved = float(r.moved_mm)

                    # If it suddenly becomes "early", stop further repeats (point is unstable)
                    if moved <= threshold:
                        pt.early += 1
                        break

                    pt.moved_wall.append(moved)

                    # Tighten cap to smallest observed wall distance (more conservative)
                    if moved < wall_dist:
                        wall_dist = moved

            return pt

        # Build grid: SGT outer, CURRENT inner
        grid: List[List[TunePoint]] = []
        for sgt in sgt_vals:
            row: List[TunePoint] = []
            grid.append(row)

            # Your rule: if a point produces "no trigger", stop this SGT's current sweep.
            # (Increasing current generally reduces sensitivity; continuing is low value.)
            stop_currents_for_this_sgt = False

            for cur in currents:
                if stop_currents_for_this_sgt:
                    # Still append an empty record so the grid shape stays rectangular.
                    row.append(TunePoint(int(sgt), float(cur), baseline, threshold))
                    continue

                pt = _run_point(int(sgt), float(cur))
                row.append(pt)

                if pt.no_trigger > 0:
                    stop_currents_for_this_sgt = True

        # Scoring / "valley" selection
        # We want: stable + safe margin beyond threshold + neighborhood stability.
        def _good(pt: TunePoint) -> bool:
            return (pt.no_trigger == 0) and (pt.early == 0) and (len(pt.moved_wall) > 0)

        def _mean_std(vals: List[float]) -> Tuple[float, float]:
            n = len(vals)
            m = sum(vals) / float(n)
            if n < 2:
                return m, 0.0
            var = sum((x - m) * (x - m) for x in vals) / float(n - 1)
            return m, math.sqrt(var)

        ranked: List[Tuple[float, int, int, TunePoint, float, float, float]] = []
        # tuple: (score, sidx, cidx, pt, mean, std, safety)
        for sidx, row in enumerate(grid):
            for cidx, pt in enumerate(row):
                if not _good(pt):
                    continue
                mean, std = _mean_std(pt.moved_wall)
                safety = mean - threshold  # bigger is more separation from false-trigger zone

                # Score: prioritize low jitter; then prefer more safety.
                # The constants here are intentionally simple and inspectable.
                score = std - 0.10 * safety
                ranked.append((score, sidx, cidx, pt, mean, std, safety))

        ranked.sort(key=lambda x: x[0])

        def _neighbor_count(sidx: int, cidx: int) -> int:
            cnt = 0
            for ds, dc in ((-1, 0), (1, 0), (0, -1), (0, 1)):
                si = sidx + ds
                ci = cidx + dc
                if si < 0 or si >= len(grid):
                    continue
                if ci < 0 or ci >= len(grid[si]):
                    continue
                if _good(grid[si][ci]):
                    cnt += 1
            return cnt

        best_valley: Optional[Tuple[int, int, float, int]] = None  # (sidx, cidx, score, neigh)
        for score, sidx, cidx, pt, mean, std, safety in ranked[:200]:
            neigh = _neighbor_count(sidx, cidx)
            if best_valley is None:
                best_valley = (sidx, cidx, score, neigh)
                continue
            _, _, best_score, best_neigh = best_valley
            if (neigh > best_neigh) or (neigh == best_neigh and score < best_score):
                best_valley = (sidx, cidx, score, neigh)

        # Output
        out: List[str] = []
        out.append("Top points (stable):")
        for score, sidx, cidx, pt, mean, std, safety in ranked[:5]:
            out.append(
                "  SGT=%d cur=%.3fA runs=%d n=%d | mean=%.3f std=%.3f | safety=%.3f | score=%.5f"
                % (pt.sgt, pt.current, pt.runs, len(pt.moved_wall), mean, std, safety, score)
            )

        if best_valley is not None:
            sidx, cidx, score, neigh = best_valley
            pt = grid[sidx][cidx]
            mean, std = _mean_std(pt.moved_wall) if pt.moved_wall else (0.0, 0.0)
            safety = mean - threshold
            out.append(
                "VALLEY PICK: SGT=%d cur=%.3fA neigh=%d | mean=%.3f std=%.3f | safety=%.3f | score=%.5f"
                % (pt.sgt, pt.current, neigh, mean, std, safety, score)
            )
        else:
            out.append("No stable region found (everything early/no-trigger).")

        gcmd.respond_info("\n".join(out))


    cmd_SENSORLESS_AUTOTUNE_FIND_CONSTRAINTS_help = """
    Home both directions from the same start position to estimate span
    """

    def cmd_SENSORLESS_AUTOTUNE_FIND_CONSTRAINTS(self, gcmd):
        margin = gcmd.get_float("MARGIN", 0.1, minval=0.0)

        ax = AXIS_TO_INDEX[self.axis]
        rail = self.kin.rails[ax]
        hi = rail.get_homing_info()

        r1 = self.home_once_and_measure()
        r2 = self.home_once_and_measure(reverse_dir=True)
        if not r2.triggered or not r1.triggered:
            raise gcmd.error("homing seemed to have failed")

        span = r1.moved_mm + r2.moved_mm

        pos_min, pos_max = rail.get_range()
        if abs(hi.position_endstop - pos_min) <= abs(pos_max - hi.position_endstop):
            opposite, stage_key = hi.position_endstop + span - margin, "position_max"
        else:
            opposite, stage_key = hi.position_endstop - span + margin, "position_min"

        configfile = self.printer.lookup_object("configfile")
        configfile.set(self.stepper_name, stage_key, "%.6f" % opposite)
        gcmd.respond_info("Distance endstop <-> bound: %.3fmm, new %s: %.2f" % (span, stage_key, opposite))
        gcmd.respond_info("Staged for SAVE_CONFIG in [%s]:" % self.stepper_name)


    @contextmanager
    def _preserve_position(self, restore_speed: float) -> Iterator[None]:
        self.toolhead.wait_moves()

        kin = self.kin
        steppers = list(kin.get_steppers())

        pos0 = list(self.toolhead.get_position())
        limits_entry = list(kin.limits)
        mcu0 = {s.get_name(): int(s.get_mcu_position()) for s in steppers}

        try:
            yield
        finally:
            self.toolhead.wait_moves()

            mcu1 = {s.get_name(): int(s.get_mcu_position()) for s in steppers}

            dstep_mm = {}
            for s in steppers:
                name = s.get_name()
                a = mcu0.get(name)
                b = mcu1.get(name)
                if a is None or b is None:
                    dstep_mm[name] = 0.0
                else:
                    dstep_mm[name] = (b - a) * float(s.get_step_dist())

            dxyz = kin.calc_position(dstep_mm)
            moved_phys = any(abs(float(v)) > 1e-9 for v in dxyz[:3])

            def _restore_limits_to_entry():
                for i, v in enumerate(limits_entry):
                    kin.limits[i] = v

            if not moved_phys:
                self.toolhead.set_position(pos0)
                _restore_limits_to_entry()
                return

            pos1_est = pos0[:]
            for i in range(min(3, len(pos1_est), len(dxyz))):
                pos1_est[i] = float(pos1_est[i]) + float(dxyz[i])

            self.toolhead.set_position(pos1_est)

            limits_tmp = list(kin.limits)
            try:
                for i in range(len(kin.limits)):
                    kin.limits[i] = (float("-inf"), float("inf"))

                coord = [None] * len(pos0)
                for i in range(min(3, len(coord))):
                    coord[i] = float(pos0[i])

                self.toolhead.manual_move(coord, restore_speed)
                self.toolhead.wait_moves()
            finally:
                for i, v in enumerate(limits_tmp):
                    kin.limits[i] = v

            self.toolhead.set_position(pos0)

            _restore_limits_to_entry()


    @contextmanager
    def _temporary_home_current(self, current: Optional[float]) -> Iterator[None]:
        if current is None:
            yield
            return

        ch = self.tmc_object.get_status.__self__.current_helper
        prev = ch.get_current()
        supports_home_current = (
            len(prev) >= 5 and hasattr(ch, "set_home_current") and hasattr(ch, "req_home_current")
        )
        try:
            if supports_home_current:
                ch.set_home_current(float(current))
            else:
                ch.set_current(float(current), prev[2], self.toolhead.get_last_move_time())
            yield
        finally:
            if supports_home_current:
                ch.set_home_current(prev[4])
            else:
                ch.set_current(prev[0], prev[2], self.toolhead.get_last_move_time())

    def get_status(self, eventtime):
        return {}


class StallField:
    def __init__(
        self,
        *,
        field: str,
        value_min: int,
        value_max: int,
        more_sensitive_step: int,
        fields: object,
        toolhead: object,
        mcu_tmc: object,
    ):
        self.field = field
        self.value_min = int(value_min)
        self.value_max = int(value_max)
        self.more_sensitive_step = int(more_sensitive_step)

        self.toolhead = toolhead

        self._fields = fields
        self._mcu_tmc = mcu_tmc

        reg = self._fields.lookup_register(self.field, None)  # type: ignore
        if reg is None:
            raise RuntimeError("Stall field '%s' has no register" % (self.field,))
        self._reg = reg

        sig = None
        try:
            sig = inspect.signature(self._mcu_tmc.set_register)  # type: ignore
        except Exception:
            pass
        self._supports_print_time = bool(sig is None or len(sig.parameters) >= 3)

    @classmethod
    def from_cmdhelper(cls, cmdhelper: object, toolhead) -> "StallField":
        fields = cmdhelper.fields  # type: ignore
        for field in ("sgthrs", "sg4_thrs", "sgt"):
            reg = fields.lookup_register(field, None)
            if reg not in fields.all_fields or field not in fields.all_fields[reg]:
                continue

            fmask = int(fields.all_fields[reg][field])
            fwidth = int(fmask.bit_count())

            if field in getattr(fields, "signed_fields", ()):
                value_min = -(1 << (fwidth - 1))
                value_max = (1 << (fwidth - 1)) - 1
                more_sensitive_step = -1
            else:
                value_min = 0
                value_max = (1 << fwidth) - 1
                more_sensitive_step = +1

            return cls(
                field=field,
                value_min=value_min,
                value_max=value_max,
                more_sensitive_step=more_sensitive_step,
                fields=fields,
                toolhead=toolhead,
                mcu_tmc=cmdhelper.mcu_tmc,  # type: ignore
            )
        raise RuntimeError("Unable to detect a StallGuard threshold field on this TMC driver")

    def clamp(self, v: int) -> int:
        lo = min(self.value_min, self.value_max)
        hi = max(self.value_min, self.value_max)
        v = int(v)
        if v < lo:
            return lo
        if v > hi:
            return hi
        return v

    def sensitivity_key(self, v: int) -> int:
        return int(v) * self.more_sensitive_step

    @property
    def max_sensitive(self) -> int:
        return self.value_max if self.more_sensitive_step > 0 else self.value_min

    @property
    def min_sensitive(self) -> int:
        return self.value_min if self.more_sensitive_step > 0 else self.value_max

    def ordered_least_to_most_sensitive(self, vmin: int, vmax: int) -> List[int]:
        vmin, vmax  = self.clamp(vmin), self.clamp(vmax)
        lo, hi = min(vmin, vmax), max(vmin, vmax)
        vals = list(range(int(lo), int(hi) + 1))
        vals.sort(key=self.sensitivity_key)
        return vals


    def read(self) -> int:
        return int(self._fields.get_field(self.field))  # type: ignore

    def write(self, value: int):
        v = self.clamp(value)
        reg_val = self._fields.set_field(self.field, v)  # type: ignore
        if self._supports_print_time:
            self._mcu_tmc.set_register(self._reg, reg_val, self.toolhead.get_last_move_time())  # type: ignore
        else:
            self._mcu_tmc.set_register(self._reg, reg_val)  # type: ignore

    @contextmanager
    def temporary(self, value: int) -> Iterator[None]:
        prev = self.read()
        try:
            self.write(value)
            yield
        finally:
            self.write(prev)


def load_config_prefix(config):
    return SensorlessAutoTune(config)
