import logging
from collections import OrderedDict

_LOG = "status_leds"

def _parse_rules_block(val):
    rules = OrderedDict()
    order = []
    last_key = None
    if not val:
        return {}, []
    for raw in val.splitlines():
        s = raw.strip()
        if not s:
            continue
        if ':' in s:
            key, rest = s.split(':', 1)
            key = key.strip()
            rest = rest.strip()
            if key not in rules:
                rules[key] = []
                order.append(key)
            last_key = key
            if rest:
                _append_effect_line(rules[key], rest)
        elif last_key:
            _append_effect_line(rules[last_key], s)
    return dict(rules), order

def _append_effect_line(dst_list, rest):
    name, extras = None, ""
    if rest.lower().startswith("index_led_effect "):
        parts = rest.split(None, 2)
        name   = parts[1] if len(parts) >= 2 else None
        extras = parts[2] if len(parts) >= 3 else ""
    else:
        parts = rest.split(None, 1)
        name   = parts[0]
        extras = parts[1] if len(parts) >= 2 else ""
    if name:
        dst_list.append({'name': name.strip(), 'extras': extras.strip()})

class StatusLeds:
    def __init__(self, config):
        self.config  = config
        self.printer = config.get_printer()
        self.gcode   = self.printer.lookup_object('gcode')
        self.log     = logging.getLogger(_LOG)

        self.watcher     = StatusWatcher(config)
        self.toolchanger = self.printer.load_object(config, 'toolchanger')

        self.rules_active,   self.order_active   = _parse_rules_block(config.get('active', ''))
        self.rules_inactive, self.order_inactive = _parse_rules_block(config.get('inactive', ''))

        self.fade_default   = config.getfloat('fadetime_default', 1.0, minval=0.0)
        self.always_replace = config.getboolean('always_replace', False)
        self._last_effects_by_tool = {}  # idx -> set(effect_name)

        self.printer.register_event_handler("klippy:connect", self._on_connect)

        self.gcode.register_command(
            'STATUS_LEDS_DUMP', self.cmd_STATUS_LEDS_DUMP,
            desc="Print per-tool status/effects decision for debugging.")


        self.gcode.register_command(
            'STATUS_LEDS_SYNC', self.cmd_STATUS_LEDS_SYNC,
            desc="Force status recomputation and LED updates.")
        self.watcher.subscribe(self._on_signal)

    #-----------------------------------------------------------------------------------------------------------------
    INDEXED = {
        "changing_from": ("active",  lambda s, idx: bool(s.get("tc_changing", False)) and
                                                    idx == int(s.get("tc_from", -1) or -1)),
        "changing_to":   ("active",  lambda s, idx: bool(s.get("tc_changing", False)) and
                                                    idx == int(s.get("tc_to",   -1) or -1)),
        "changing":      ("inactive",lambda s, idx: bool(s.get("tc_changing", False)) and
                                                    idx not in { int(s.get("tc_from", -1) or -1),
                                                                 int(s.get("tc_to",   -1) or -1)
                                                               }),
    }
    def _build_aliases(self, _snap_keys=None):
        self.ALIAS = {
            "printing": lambda s: bool(s.get("printing", False)) or
                                str(s.get("print_state", "")).lower() == "printing",
            "paused":   lambda s: bool(s.get("paused", False)) or
                                str(s.get("print_state", "")).lower() == "paused",

            "homing":   lambda s: bool(s.get("homing", False)) or
                                bool(s.get("homing_x", False)) or
                                bool(s.get("homing_y", False)) or
                                bool(s.get("homing_z", False)),
            "changing": lambda s: bool(s.get("tc_changing", False)),
            "error":    lambda s: bool(s.get("tc_error", False)),

            "idle":     lambda s: str(s.get("idle_state", "")).lower() == "idle",
            "ready":    lambda s: str(s.get("idle_state", "")).lower() == "ready",

            'heating':  lambda s: s.get("heating", False),

            "busy":     lambda s: s.get("idle_state", "").lower() == "printing",

            "probing":  lambda s: bool(s.get("probing", False)),

            "leveling": lambda s: bool(s.get("qgl_active", False)) or
                                bool(s.get("ztilt_active", False)) or
                                bool(s.get("bedmesh_active", False)),
            "calibrating": lambda s: bool(s.get("homing", False)) or
                                    bool(s.get("probing", False)) or
                                    bool(s.get("qgl_active", False)) or
                                    bool(s.get("ztilt_active", False)) or
                                    bool(s.get("bedmesh_active", False)),
        }

    def cmd_STATUS_LEDS_DUMP(self, gcmd):
        snap = self.watcher.snapshot() or {}
        try:
            nums = list(self.toolchanger.tool_numbers)
            all_idxs = list(range(len(nums)))
        except Exception:
            all_idxs = []
        active_idx = None
        try:
            at = getattr(self.toolchanger, "active_tool", None)
            if at is not None:
                nums = list(self.toolchanger.tool_numbers)
                active_idx = nums.index(at.tool_number)
        except Exception:
            active_idx = None
        lines = []
        for idx in all_idxs:
            role = "inactive"
            if active_idx is not None and idx == active_idx:
                role = "active"
            elif snap.get("tc_changing", False):
                frm = int(snap.get("tc_from", -1) or -1)
                to  = int(snap.get("tc_to",   -1) or -1)
                if idx in (frm, to):
                    role = "active"
            order  = self.order_active if role == "active" else self.order_inactive
            status = self._select_from_order_for_idx(order, snap, idx)
            effs   = self._effects_for(status, role)
            prev   = sorted(self._last_effects_by_tool.get(idx, set()))
            want   = [nm for (nm, _ex) in effs]
            lines.append(f"idx={idx} role={role} status={status} prev={prev} want={want}")
        self.gcode.respond_info("\n".join(lines) if lines else "no tools")

    #-----------------------------------------------------------------------------------------------------------------
    def _on_connect(self):
        try:
            snapshot_keys = set(self.watcher.list_signals())
        except Exception:
            snapshot_keys = set((self.watcher.snapshot() or {}).keys())
        self._build_aliases(snapshot_keys)
        self._validate_rules_strict(snapshot_keys)


    def cmd_STATUS_LEDS_SYNC(self, gcmd):
        """Force status recomputation and LED updates."""
        snap = self.watcher.snapshot()
        self._apply(snap)

    def _validate_rules_strict(self, snapshot_keys=[]):
        errs = []
        allowed = set(snapshot_keys) | set(self.ALIAS.keys()) | set(self.INDEXED.keys()) | {"off"}

        for blk, rules in (("active", self.rules_active), ("inactive", self.rules_inactive)):
            for key, effects in rules.items():
                if key not in allowed:
                    errs.append(f"{blk}:{key} is not a known status key")
                exp_blk = self.INDEXED.get(key, (None, None))[0]
                if exp_blk and exp_blk != blk:
                    errs.append(f"{blk}:{key} not allowed (must be in {exp_blk})")
                for rec in effects:
                    nm = (rec.get("name") or "").strip()
                    if not nm:
                        errs.append(f"{blk}:{key} has an empty effect name")
                        continue
                    if self.printer.lookup_object(f"index_led_effect {nm}", None) is None:
                        errs.append(f"{blk}:{key} references unknown index_led_effect '{nm}'")
        if errs:
            raise self.config.error("[status_leds] configuration errors:\n" + "\n".join(errs))

    def _on_signal(self, name, newv, oldv, snapshot):
        self._apply(snapshot)

    def _select_from_order(self, order, snapshot):
        for token in order:
            if snapshot.get(token, False):
                return token
            pred = self.ALIAS.get(token)
            if pred and pred(snapshot):
                return token
        return "off"

    def _effects_for(self, status, which):
        rules = self.rules_active if which == 'active' else self.rules_inactive
        lst = rules.get(status, [])
        return [(rec['name'], (rec.get('extras') or '').strip()) for rec in lst]

    #------------------- changing tolchanger etc --------------------------------------------------------

    def _select_from_order_for_idx(self, order, snap, idx):
        for token in order:
            meta = self.INDEXED.get(token)
            if meta and meta[1](snap, idx):
                return token
            if snap.get(token, False):
                return token
            pred = self.ALIAS.get(token)
            if pred and pred(snap):
                return token
        return "off"
    
    def _apply(self, snap):
        # Figure out which tool indexes exist
        try:
            nums = list(self.toolchanger.tool_numbers)
            all_idxs = list(range(len(nums)))
        except Exception:
            all_idxs = []

        # Compute active tool index (if any)
        active_idx = None
        try:
            at = getattr(self.toolchanger, "active_tool", None)
            if at is not None:
                nums = list(self.toolchanger.tool_numbers)
                active_idx = nums.index(at.tool_number)
        except Exception:
            active_idx = None

        # Plan desired effects per tool
        desired = {}  # idx -> (role, [(name, extras), ...])
        for idx in all_idxs:
            role = "inactive"
            if active_idx is not None and idx == active_idx:
                role = "active"
            elif snap.get("tc_changing", False):
                frm = int(snap.get("tc_from", -1) or -1)
                to  = int(snap.get("tc_to",   -1) or -1)
                if idx in (frm, to):
                    role = "active"

            order  = self.order_active if role == "active" else self.order_inactive
            status = self._select_from_order_for_idx(order, snap, idx)
            effs   = self._effects_for(status, role) or []
            desired[idx] = (role, effs)

        # Apply without flicker
        for idx in all_idxs:
            prev_set   = self._last_effects_by_tool.get(idx, set())
            _role, effs = desired.get(idx, ("inactive", []))
            effs = effs or []

            # If nothing matched, do nothing (keep whatever was already on)
            if not effs:
                continue

            # Names only: extras don't force a restart
            desired_names = {nm for (nm, _ex) in effs}
            if desired_names == prev_set:
                self._last_effects_by_tool[idx] = prev_set
                continue

            eff_map = {}
            for nm, ex in effs:
                eff_map[nm] = ex

            # Enable new first (prevents blackout)
            for nm in (desired_names - prev_set):
                self._send_cmd(nm, [idx], enable=True, extras=eff_map.get(nm))

            for nm in (prev_set - desired_names):
                self._send_cmd(nm, [idx], enable=False)

            self._last_effects_by_tool[idx] = desired_names

    def _compose_extras(self, extras):
        e = (extras or '').strip()
        if self.always_replace and "REPLACE=" not in e.upper():
            e = (e + (" " if e else "") + "REPLACE=1").strip()
        if "FADETIME=" not in e.upper() and self.fade_default is not None:
            e = (e + (" " if e else "") + f"FADETIME={self.fade_default}").strip()
        return e or None
    
    def _send_cmd(self, effect_name, idxs, enable, extras=None):
        if not effect_name:
            return
        idx_part = ""
        if idxs:
            idx_part = f" IDX={','.join(map(str, idxs))}"
        parts = [f"SET_IDX_LED_EFFECT EFFECT={effect_name}{idx_part}"]
        if enable is False:
            parts.append("STOP=1")
        ex = self._compose_extras(extras)
        if ex:
            parts.append(ex)
        self.gcode.run_script_from_command(" ".join(parts))




_POLL_S   = 0.1
class StatusWatcher:
    def __init__(self, config):
        self.config   = config
        self.printer  = config.get_printer()
        self.reactor  = self.printer.get_reactor()
        self.log      = logging.getLogger("status_watcher")

        self._signals            = {}
        self._subs               = []          # (cb, allowed_set_or_None)
        self._heater_target_subs = []

        self._poll_tasks = []       # list of callables()
        self._poll_timer = None

        self._home_counts = {"x": 0, "y": 0, "z": 0}
        self._last_homing_axes = set()

        self._register_notes()
        self.printer.register_event_handler("klippy:shutdown", self._on_shutdown)
        self.printer.register_event_handler("klippy:connect", self._on_connect)

        #self.printer.lookup_object('gcode').register_command("STATUS_WATCHER_DUMP", self.cmd_dump,
        #                   desc="Dump current watcher signals")
        
    def _on_connect(self):
        self._wire_heaters()
        self._register_notes()
        if self._poll_tasks and self._poll_timer is None:
            self._poll_timer = self.reactor.register_timer(self._poll_cb)
            now = self.reactor.monotonic()
            self.reactor.update_timer(self._poll_timer, now + _POLL_S)

    def cmd_dump(self, gcmd):
        snap = self.snapshot()
        lines = [f"{k}={snap[k]!r}" for k in sorted(snap)]
        self.printer.lookup_object('gcode').respond_info("status_watcher: " + ", ".join(lines))

    # ----- public -----
    def subscribe(self, cb, signals=None):
        if not callable(cb):
            raise TypeError("subscribe(cb[, signals]) requires a callable")
        allowed = None
        if signals is not None:
            allowed = set(signals) if isinstance(signals, (list, tuple, set)) else {signals}
            missing = [s for s in allowed if s not in self._signals]
            if missing:
                raise ValueError(f"cannot subscribe to unknown signals: {missing}")
        self._subs.append((cb, allowed))
        snap = self.snapshot()
        if snap:
            for name, val in snap.items():
                if allowed is None or name in allowed:
                    try:
                        cb(name, val, None, snap)
                    except Exception:
                        self.log.exception("initial callback failed for %s", name)

    def subscribe_heater_targets(self, cb):
        """Register for heater target changes. cb(name: str, target: float) -> None"""
        if not callable(cb):
            raise TypeError("subscribe_heater_targets(cb) requires a callable")
        self._heater_target_subs.append(cb)

    def list_signals(self):
        return tuple(self._signals.keys())

    def snapshot(self):
        return dict(self._signals)

    def _wire_heaters(self):
        try:
            heaters_mgr = self.printer.lookup_object("heaters")
            names = list(heaters_mgr.get_all_heaters())
        except Exception:
            return

        resolved = []
        for name in names:
            try:
                h = heaters_mgr.lookup_heater(name)
            except Exception:
                h = None
            if h is not None:
                resolved.append((name, h))
        if not resolved:
            return

        self._ensure("heating", False)
        last_target = {}

        def _poll_heaters():
            any_heat = False
            now = self.reactor.monotonic()
            for name, h in resolved:
                try:
                    st = h.get_status(now)  # {'temperature','target','power'}
                    tgt = float(st.get("target") or 0.0)
                except Exception:
                    continue

                if last_target.get(name) != tgt:
                    last_target[name] = tgt
                    for cb in self._heater_target_subs:
                        try:
                            cb(name, tgt)  # just name + new target, like you asked
                        except Exception:
                            pass
                if tgt > 0.0:
                    any_heat = True

            self._note("heating", any_heat)
        self._add_poll(_poll_heaters)

    # ----- registration -----
    def _register_notes(self):
        self._register_home_noter()
        self._register_printing_noter()
        self._register_idle_noter()
        self._register_toolchange_noter()
        self._register_qgl_noter()
        self._register_ztilt_noter()
        self._register_bedmesh_noter()

    # --- homing xyz/all ---
    def _register_home_noter(self):
        for k in ("homing_x", "homing_y", "homing_z", "homing"):
            self._ensure(k, False)

        def _axes_from_indices(indices):
            m = {0: "x", 1: "y", 2: "z"}
            return {m[i] for i in indices if i in m}

        def _on_begin(homing_state, rails):
            try:
                axes = _axes_from_indices(tuple(homing_state.get_axes()))
            except Exception:
                axes = set()
            self._last_homing_axes = set(axes)
            for a in axes:
                self._home_counts[a] += 1
            self._bulk_note({f"homing_{a}": self._home_counts[a] > 0 for a in ("x", "y", "z")})
            self._note("homing", self._any_homing())

        def _on_end(homing_state, rails):
            axes = self._last_homing_axes or {"x", "y", "z"}
            for a in axes:
                if self._home_counts[a] > 0:
                    self._home_counts[a] -= 1
            self._bulk_note({f"homing_{a}": self._home_counts[a] > 0 for a in ("x", "y", "z")})
            self._note("homing", self._any_homing())
            self._last_homing_axes.clear()

        self.printer.register_event_handler("homing:home_rails_begin", _on_begin)
        self.printer.register_event_handler("homing:home_rails_end",   _on_end)

    def _any_homing(self):
        return any(self._signals.get(k, False) for k in ("homing_x", "homing_y", "homing_z"))
    # --- printing --- paused --- busy ---
    def _register_printing_noter(self):
        def _poll_printing():
            # print_stats
            try:
                ps = self.printer.lookup_object('print_stats', None)
            except Exception:
                ps = None

            st = (getattr(ps, 'state', None) or getattr(ps, 'print_state', '') or '').lower() if ps else ''
            # declare only what we can maintain
            self._ensure('print_state', st)
            self._ensure('printing',   st == 'printing')
            self._note('print_state', st)
            self._note('printing',   st == 'printing')

            # pause_resume augments paused; still lazy
            try:
                pr = self.printer.lookup_object('pause_resume', None)
            except Exception:
                pr = None

            if pr is not None:
                is_paused = bool(getattr(pr, 'is_paused', False))
                sd_paused = bool(getattr(pr, 'sd_paused', False))
                cmd_sent  = bool(getattr(pr, 'pause_command_sent', False))
                self._ensure('sd_paused', sd_paused)
                self._ensure('pause_cmd_sent', cmd_sent)
                self._ensure('paused', is_paused or sd_paused or (st == 'paused'))
                self._bulk_note({
                    'sd_paused': sd_paused,
                    'pause_cmd_sent': cmd_sent,
                    'paused': (is_paused or sd_paused or (st == 'paused')),
                })
            else:
                if 'paused' in self._signals:
                    self._note('paused', st == 'paused')

        self._add_poll(_poll_printing)

    def _register_idle_noter(self):
        it = self.printer.lookup_object('idle_timeout', None)
        if not it:
            return
        # state is 'ready' | 'printing' | 'idle'
        self._ensure('idle_state', getattr(it, 'state', 'ready'))

        def _poll_idle():
            st = getattr(it, 'state', 'ready') or 'ready'
            self._note('idle_state', st)
        self._add_poll(_poll_idle)

    def _register_toolchange_noter(self):
        tc = self.printer.lookup_object('toolchanger', None)
        if not tc:
            return

        # only register signals we can actually drive
        for k, v in (
            ("tc_status", ""), ("tc_changing", False), ("tc_error", False),
            ("tc_error_message", ""), ("tc_active_tool", -1),
            ("tc_from", -1), ("tc_to", -1), ("tc_change_id", -1),
            ("tc_before_change", False), ("tc_after_change", False),
        ):
            self._ensure(k, v)

        state = {"last_status": None, "last_change_id": -1,
                "last_active": -1}

        def _poll_tc():
            fired_before = False
            fired_after  = False

            status = getattr(tc, "status", "")
            change_id = getattr(tc, "current_change_id", -1)
            active = tc.active_tool.tool_number if getattr(tc, "active_tool", None) else -1

            # rising edge: READY -> CHANGING
            if status == "changing" and state["last_status"] != "changing":
                self._note("tc_before_change", True); fired_before = True
                self._note("tc_changing", True)
                self._note("tc_from", state["last_active"])
                pickup = getattr(tc, "last_change_pickup_tool", None)  # set at start of select_tool
                self._note("tc_to", getattr(pickup, "tool_number", -1))
                self._note("tc_change_id", change_id)

            # falling edge: CHANGING -> READY
            elif state["last_status"] == "changing" and status == "ready":
                self._note("tc_after_change", True); fired_after = True
                self._note("tc_changing", False)

            # falling edge: CHANGING -> ERROR
            elif state["last_status"] == "changing" and status == "error":
                self._note("tc_changing", False)
                self._note("tc_error", True)
                self._note("tc_error_message", getattr(tc, "error_message", "") or "")

            # baseline updates
            self._note("tc_status", status)
            self._note("tc_active_tool", active)

            # clear pulses if they didn’t fire this tick
            if not fired_before and self._signals.get("tc_before_change"):
                self._note("tc_before_change", False)
            if not fired_after and self._signals.get("tc_after_change"):
                self._note("tc_after_change", False)

            # auto-clear error once toolchanger leaves ERROR
            if status != "error" and self._signals.get("tc_error"):
                self._note("tc_error", False)
                if self._signals.get("tc_error_message"):
                    self._note("tc_error_message", "")

            state["last_status"] = status
            state["last_change_id"] = change_id
            state["last_active"] = active

        self._add_poll(_poll_tc)

    def _register_qgl_noter(self):
        qgl = self.printer.lookup_object('quad_gantry_level', None)
        if not qgl:
            return
        ph = getattr(qgl, 'probe_helper', None)
        if not ph or getattr(ph, '_sw_wrapped', False):
            return
        self._ensure("qgl_active", False)

        orig = ph.start_probe
        def _wrap_start(gcmd):
            self._note("qgl_active", True)
            try:
                return orig(gcmd)
            finally:
                self._note("qgl_active", False)
        ph.start_probe = _wrap_start
        ph._sw_wrapped = True

    def _register_ztilt_noter(self):
        zt = self.printer.lookup_object('z_tilt', None)
        if not zt:
            return
        ph = getattr(zt, 'probe_helper', None)
        if not ph or getattr(ph, '_sw_wrapped', False):
            return
        self._ensure("ztilt_active", False)

        orig = ph.start_probe
        def _wrap_start(gcmd):
            self._note("ztilt_active", True)
            try:
                return orig(gcmd)
            finally:
                self._note("ztilt_active", False)
        ph.start_probe = _wrap_start
        ph._sw_wrapped = True

    def _register_bedmesh_noter(self):
        bm = self.printer.load_object(self.config, 'bed_mesh', None)
        if not bm:
            return
        self._ensure("bedmesh_active", False)
        bmc = getattr(bm, 'bmc', None)
        pm = getattr(bmc, 'probe_mgr', None)
        if not pm:
            return
        
        ph = getattr(pm, 'probe_helper', None)
        if ph and not getattr(ph, '_sw_wrapped', False):
            orig = ph.start_probe
            def _wrap_start(gcmd):
                self._note("bedmesh_active", True)
                try:
                    return orig(gcmd)
                finally:
                    self._note("bedmesh_active", False)
            ph.start_probe = _wrap_start
            ph._sw_wrapped = True

        rsh = getattr(pm, 'rapid_scan_helper', None)
        if rsh and not getattr(rsh, '_sw_wrapped', False):
            orig2 = rsh.perform_rapid_scan
            def _wrap_rapid(gcmd):
                self._note("bedmesh_active", True)
                try:
                    return orig2(gcmd)
                finally:
                    self._note("bedmesh_active", False)
            rsh.perform_rapid_scan = _wrap_rapid
            rsh._sw_wrapped = True

    # ----- polling -----
    def _add_poll(self, func):
        if func not in self._poll_tasks:
            self._poll_tasks.append(func)

    def _poll_cb(self, eventtime):
        for fn in list(self._poll_tasks):
            try:
                fn()
            except Exception:
                self.log.exception("poll task failed")
        return eventtime + _POLL_S

    # ----- internals -----
    def _on_shutdown(self, *a, **k):
        return

    def _ensure(self, name, initial):
        if name not in self._signals:
            self._signals[name] = initial

    def _note(self, name, value):
        old = self._signals.get(name, None)
        if old == value:
            return False
        self._signals[name] = value
        snap = self.snapshot()
        for cb, allowed in list(self._subs):
            if allowed is None or name in allowed:
                try:
                    cb(name, value, old, snap)
                except Exception:
                    self.log.exception("callback failed for %s", name)
        return True
    
    def _bulk_note(self, mapping: dict):
        changed = False
        for k, v in mapping.items():
            changed |= self._note(k, v)
        return changed

def load_config(config):
    return StatusLeds(config)
