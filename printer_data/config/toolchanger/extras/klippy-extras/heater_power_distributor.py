import logging

# ---- constants ----
POLLING_INTERVAL_DEFAULT = 1.0
DEFAULT_WATT             = 60.0

AT_HEAT_TOLERANCE       = 2     # °C below target still considered heating
WARMUP_GUARD            = 5     # s after a target change before considering rescue
RESCUE_URGENCY_FLOOR    = 0.10  # ignore tiny urgency below this

VERBOSE = False

# Built‑in derating models (factor vs temperature °C). Coarse approximations.
# whats v1? v3? no clue. i stole this from reddit.
BUILTIN_MODELS = {
    'none':        [(0,1.0),(400,1.0)],
    'bambulab':    [(25,1.00),(100,0.85),(200,0.70),(260,0.62),(320,0.52)],
    'v1':          [(25,1.00),(100,0.82),(200,0.66),(260,0.58),(320,0.48)],
    'rapido_v2':   [(25,1.00),(100,0.90),(200,0.78),(260,0.70),(320,0.63)],
    'v3':          [(25,1.00),(100,0.88),(200,0.76),(260,0.68),(320,0.60)],
    'triangle70':  [(25,1.00),(100,0.80),(200,0.62),(260,0.54),(320,0.45)],
}

class HeaterState:
    def __init__(self, temp, target, slope, base_w, cap_w, need_heat, last_target_change):
        self.temp = temp
        self.target = target
        self.slope = slope
        self.base_w = base_w
        self.cap_w = cap_w
        self.need_heat = need_heat
        self.last_target_change = last_target_change

class GroupState:
    def __init__(self, names, states, total_cap_w):    
        self.names = names # list of heater names that currently need heat
        self.states = states # dict name-> HeaterState
        self.total_cap_w = total_cap_w

class WeightPlan:
    def __init__(self, weights, urgent, triggered, details):
        self.weights = weights # dict name-> weight; urgent: list of names
        self.urgent = urgent
        self.triggered = triggered
        self.details = details # list of (name, urgency, scale_hint)

class HeaterRec:
    """Per-heater runtime record"""
    def __init__(self, heater, rated_watt, orig_max_power, vh, vh_params):
        self.heater = heater
        self.rated_watt = rated_watt
        self.orig_max_power = orig_max_power
        self.vh = vh
        self.vh_params = vh_params  # (hg, cgt, hyst)
        # evolving samples
        self.prev_temp = None
        self.prev_time = None
        self.prev_target = None
        self.last_target_change = 0.0
        self.slope_ema = None

class HeaterPowerDistributor:
    def __init__(self, config):
        self.printer = config.get_printer()
        self.name = config.get_name().split()[-1]

        self.heater_list           = config.getlist('heaters')
        self.max_total_watts       = config.getfloat('max_power',                                above=0.0)
        self.poll_interval         = config.getfloat('poll_interval',  POLLING_INTERVAL_DEFAULT, above=0.0)
        self.active_extruder_boost = config.getfloat('active_extruder_boost', 1.0,               above=0.0)

        model_name = config.getchoice('model', list(BUILTIN_MODELS) + ['custom'], default='none')
        powers     = config.getfloatlist('powers', (DEFAULT_WATT,))

        if len(powers) == 1:
            self.rated_watts_cfg = [float(powers[0])] * len(self.heater_list)
        elif len(powers) == len(self.heater_list):
            self.rated_watts_cfg = [float(p) for p in powers]
        else:
            raise config.error("'powers' length (%d) must be 1 or match heaters count (%d)" % (len(powers), len(self.heater_list)))

        if model_name == 'custom':
            pairs  = config.getlists('model_points', seps=(',', '\n'), count=2, parser=float)
            points = [(t, f) for (t, f) in pairs]
            if len(points) < 2:
                raise config.error("'model_points' must have at least two 'temp, factor' pairs")
        else:
            points = BUILTIN_MODELS[model_name]
        self.model_curve = _LinearCurve(points)
        self.model_name  = model_name

        # Runtime state
        self.heaters = {}     # name -> HeaterRec
        self.last_time = None
        self.rescue_on = False
        self.last_notify_time = 0.0

        self.pheaters = self.printer.load_object(config, 'heaters')
        self.gcode = self.printer.lookup_object('gcode')
        self.gcode.register_mux_command(
            'SET_HEATER_DISTRIBUTOR', 'GROUP', self.name,
            self.cmd_SET_HEATER_DISTRIBUTOR,
            desc=self.cmd_SET_HEATER_DISTRIBUTOR_help
        )
        self.printer.register_event_handler('klippy:ready', self._handle_ready)

    # ---------------------------- setup ----------------------------
    def _handle_ready(self):
        for idx, name in enumerate(self.heater_list):
            heater = self.pheaters.lookup_heater(name)
            orig_cap = float(getattr(heater.control, 'heater_max_power', 1.0))
            rated = float(self.rated_watts_cfg[idx])

            # verify_heater params (best-effort)
            vh = self.printer.lookup_object('verify_heater %s' % (name,), None)
            if vh is not None:
                hg   = float(getattr(vh, 'heating_gain', 2.0))
                cgt  = float(getattr(vh, 'check_gain_time', 60.0 if name == 'heater_bed' else 20.0))
                hyst = float(getattr(vh, 'hysteresis', 5.0))
            else:
                cgt_default = 60.0 if name == 'heater_bed' else 20.0
                hg, cgt, hyst = 2.0, cgt_default, 5.0

            self.heaters[name] = HeaterRec(
                heater=heater,
                rated_watt=rated,
                orig_max_power=orig_cap,
                vh=vh,
                vh_params=(hg, cgt, hyst),
            )

        reactor = self.printer.get_reactor()
        reactor.register_timer(self._update_callback, reactor.monotonic() + self.poll_interval)

        logging.info(
            "HeaterPowerDistributor '%s' heaters=%s rated=%sW budget=%.1fW model=%s",
            self.name,
            list(self.heaters.keys()),
            [self.heaters[h].rated_watt for h in self.heater_list],
            self.max_total_watts,
            self.model_name,
        )

    # ---------------------------- commands ----------------------------
    cmd_SET_HEATER_DISTRIBUTOR_help = "Set max total power (watts) for this group"
    def cmd_SET_HEATER_DISTRIBUTOR(self, gcmd):
        power = gcmd.get_float('POWER', above=0.0)
        self.max_total_watts = power
        gcmd.respond_info("%s: budget set to %.1f W." % (self.name, self.max_total_watts))

    # ---------------------------- helpers ----------------------------
    def _derate_factor(self, temp_c):
        try:
            f = float(self.model_curve.at(temp_c))
        except Exception:
            f = 1.0
        return max(0.0, min(1.0, f))

    def _get_active_extruder_name(self, eventtime):
        th = self.printer.lookup_object('toolhead', None)
        if th is None:
            return None
        try:
            status = th.get_status(eventtime) or {}
            return status.get('extruder')
        except Exception:
            return None

    def _collect_state(self, eventtime):
        """Return GroupState snapshot for this tick."""
        TAU = 1.5  # EMA time-constant for slope (s)
        states = {}
        active = []

        for name, rec in self.heaters.items():
            heater = rec.heater
            temp, target = heater.get_temp(eventtime)
            base_w = max(1e-6, rec.rated_watt * self._derate_factor(temp))
            cap_w  = max(0.0, base_w * rec.orig_max_power)

            # Target change tracking
            if rec.prev_target is None or rec.prev_target != target:
                rec.last_target_change = eventtime
                rec.prev_target = target

            # EMA slope calc
            if rec.prev_time is not None and rec.prev_temp is not None:
                dt = max(1e-6, eventtime - rec.prev_time)
                inst = (temp - rec.prev_temp) / dt
                alpha = dt / (TAU + dt)
                prev_ema = rec.slope_ema
                ema = inst if prev_ema is None else (alpha * inst + (1.0 - alpha) * prev_ema)
                rec.slope_ema = ema
                slope = ema
            else:
                slope = None

            rec.prev_temp = temp
            rec.prev_time = eventtime

            need_heat = (target > 0.0) and (temp <= (target - AT_HEAT_TOLERANCE))
            hs = HeaterState(
                temp=temp,
                target=target,
                slope=slope,
                base_w=base_w,
                cap_w=cap_w,
                need_heat=bool(need_heat and cap_w > 0.0),
                last_target_change=rec.last_target_change,
            )
            states[name] = hs
            if hs.need_heat:
                active.append(name)

        total_cap_w = sum(states[n].cap_w for n in active)
        return GroupState(active, states, total_cap_w)

    def _compute_weights(self, group, active_extruder_name, budget_w, eventtime):
        """Compute base weights and detect urgent heaters. Returns a WeightPlan."""
        active_names = group.names
        weights = {n: 1.0 for n in active_names}
        triggered = False
        details = []
        urgent = []

        boost = float(self.active_extruder_boost)
        power_limited = budget_w < group.total_cap_w - 1e-9

        # Active extruder bass boost
        if boost != 1.0 and active_extruder_name in weights:
            weights[active_extruder_name] = boost

        if not power_limited:
            return WeightPlan(weights, [], False, [])

        # Build rescue candidates
        seen = set()
        for n in active_names:
            if n in seen:
                continue
            seen.add(n)

            s = group.states[n]
            vh = self.heaters[n].vh
            hg, cgt, vh_hyst = self.heaters[n].vh_params
            base_slope = hg / cgt

            # warmup guard
            if (eventtime - s.last_target_change) < WARMUP_GUARD:
                continue
            if s.temp >= s.target - vh_hyst:
                continue

            urgency = 0.0
            if vh is not None and getattr(vh, 'approaching_target', False):
                # Values are numeric already in Klipper; same reactor time base.
                goal_temp = getattr(vh, 'goal_temp', s.temp)
                goal_time = getattr(vh, 'goal_systime', eventtime)
                D = max(0.0, goal_temp - s.temp)
                T_rem = max(0.5, goal_time - eventtime)
                req_slope = D / T_rem
                if base_slope > 1e-9:
                    urgency = max(urgency, req_slope / base_slope - 1.0)

            if s.slope is not None and base_slope > 1e-9:
                urgency = max(urgency, (base_slope - s.slope) / base_slope)

            if urgency > RESCUE_URGENCY_FLOOR:
                urgent.append(n)
                # keep a small record for notify only
                scale_hint = 1.0 + urgency * max(1, len(active_names))
                triggered = True
                details.append((n, round(urgency, 3), round(scale_hint, 2)))

        return WeightPlan(weights, urgent, triggered, details)

    def _water_fill(self, active_names, weights, eff_cap_w, target_budget):
        alloc_w   = {n: 0.0 for n in active_names}
        remaining = target_budget
        unsat     = set(active_names)
        while unsat and remaining > 1e-9:
            total_w = sum(weights[n] for n in unsat)
            if total_w <= 1e-12:
                break
            share_per_weight = remaining / total_w
            newly_sat = set()
            for n in list(unsat):
                room = eff_cap_w[n] - alloc_w[n]
                want = weights[n] * share_per_weight
                add  = want if want <= room else room
                if add > 0.0:
                    alloc_w[n] += add
                if (eff_cap_w[n] - alloc_w[n]) <= 1e-12:
                    newly_sat.add(n)
            remaining = target_budget - sum(alloc_w.values())
            if not newly_sat:
                break
            unsat -= newly_sat
        return alloc_w

    def _apply_caps(self, alloc_w, group):
        for name, rec in self.heaters.items():
            orig_cap = rec.orig_max_power
            if name in alloc_w and name in group.states:
                base = group.states[name].base_w
                frac = max(0.0, min(orig_cap, alloc_w[name] / base))  # watts @ 100% PWM
                rec.heater.control.heater_max_power = frac
            else:
                rec.heater.control.heater_max_power = orig_cap

    # ---------------------------- loop ----------------------------
    def _update_callback(self, eventtime):
        if self.last_time is None:
            self.last_time = eventtime
            return eventtime + self.poll_interval
        self.last_time = eventtime

        active_extruder = self._get_active_extruder_name(eventtime)
        group = self._collect_state(eventtime)

        # If nobody needs heat or capacities collapsed, restore and bail
        if not group.names or group.total_cap_w <= 1e-9:
            for name, rec in self.heaters.items():
                rec.heater.control.heater_max_power = rec.orig_max_power
            return eventtime + self.poll_interval

        budget_w = max(0.0, float(self.max_total_watts))
        target_budget = min(budget_w, group.total_cap_w)

        plan = self._compute_weights(group, active_extruder, budget_w, eventtime)

        if plan.triggered and (not self.rescue_on or (eventtime - self.last_notify_time) > 5.0):
            self.rescue_on = True
            self.last_notify_time = eventtime
            msg = ("%s: verify-heater assist active; power-limited. urgent=%s boosted: %s" % (
                self.name,
                list(plan.urgent)[:4],
                ", ".join(["%s(urg=%s,x%s)" % (n,u,sc) for (n,u,sc) in plan.details[:4]])
            ))
            self.gcode.respond_info if VERBOSE else logging.info(msg)

        elif not plan.triggered:
            self.rescue_on = False

        caps = {n: group.states[n].cap_w for n in group.names}
        alloc_w = self._alloc_priority(list(plan.urgent), list(group.names), plan.weights, caps, target_budget)
        self._apply_caps(alloc_w, group)
        return eventtime + self.poll_interval

    # ---------------------------- priority allocator ----------------------------
    def _alloc_priority(self, urgent_names, active_names, weights, eff_cap_w, target_budget):
        # Nothing to do
        if target_budget <= 1e-9:
            return {n: 0.0 for n in active_names}
        urgent_set = set(urgent_names)
        urgent_cap = sum(eff_cap_w[n] for n in urgent_set)
        alloc = {n: 0.0 for n in active_names}

        if not urgent_set:
            return self._water_fill(active_names, weights, eff_cap_w, target_budget)

        if urgent_cap <= target_budget + 1e-9:
            # Give urgent heaters their full instantaneous caps first
            for n in urgent_set:
                alloc[n] = eff_cap_w[n]
            remaining = target_budget - urgent_cap
            others = [n for n in active_names if n not in urgent_set]
            if remaining > 1e-9 and others:
                fill = self._water_fill(others, {k: weights[k] for k in others}, eff_cap_w, remaining)
                alloc.update(fill)
            return alloc

        # Not enough budget even for urgent set: share only among urgent
        fill = self._water_fill(list(urgent_set), {k: 1.0 for k in urgent_set}, eff_cap_w, target_budget)
        alloc.update(fill)
        return alloc


class _LinearCurve:
    def __init__(self, points):
        pts = sorted([(float(x), float(y)) for x, y in points], key=lambda p: p[0])
        self.x = [p[0] for p in pts]
        self.y = [p[1] for p in pts]
    def at(self, x):
        if x <= self.x[0]:
            return self.y[0]
        if x >= self.x[-1]:
            return self.y[-1]
        lo, hi = 0, len(self.x)-1
        while hi - lo > 1:
            mid = (lo+hi)//2
            if x < self.x[mid]:
                hi = mid
            else:
                lo = mid
        x0, y0 = self.x[lo], self.y[lo]
        x1, y1 = self.x[hi], self.y[hi]
        t = (x - x0) / (x1 - x0)
        return y0 + t*(y1 - y0)


def load_config_prefix(config):
    return HeaterPowerDistributor(config)
