import logging
from .toolchanger import STATUS_CHANGING, STATUS_INITIALIZING

IGN_TOOLCHANGES = 'toolchanges'
IGN_PROBING = 'probing'
IGN_STANDSTILL = 'standstill'
IGN_ALL = 'all'
_ALLOWED_IGNORES = {IGN_TOOLCHANGES, IGN_PROBING, IGN_STANDSTILL, IGN_ALL}

class ToolCrash:
    def __init__(self, config):
        self.printer = config.get_printer()
        self.reactor = self.printer.get_reactor()
        self.config = config
        self.name = config.get_name()
        self.gcode = self.printer.lookup_object('gcode')
        self.toolhead = None
        self.toolchanger = self.printer.lookup_object('toolchanger')

        self.auto_on = config.getboolean('auto_on', False)

        raw = {s.strip().lower() for s in config.getlist('ignore_events', [], sep=',') if s.strip()}
        unknown = raw - _ALLOWED_IGNORES
        if unknown:
            allowed = ", ".join(sorted(_ALLOWED_IGNORES))
            bad = ", ".join(sorted(unknown))
            raise config.error(f"[tool_crash] invalid ignore_events: {bad}. Allowed: {allowed}")
        self.ignore = {IGN_TOOLCHANGES, IGN_PROBING, IGN_STANDSTILL} if IGN_ALL in raw else raw

        self.crash_mintime = config.getfloat('crash_mintime', 0.50, above=0.)

        self.gcode_macro = self.printer.load_object(config, 'gcode_macro')
        self.crash_gcode = self.gcode_macro.load_template(config, 'crash_gcode') if config.get('crash_gcode', None) else None

        for section in config.get_prefix_sections('tool '):
            if not section.get('detection_pin', None):
                raise config.error(f"[tool_crash] tool '{section.get_name()}' is missing 'detection_pin'")

        self.enabled = False
        self.expected_tool = None
        self._manual_expected = False
        self._armed_since_pt = None
        self._arm_seq = 0
        self._z_stepper_names = set()
        self._z_home_active = 0
        self._z_home_sticky = False
        self._z_sticky_cb_pending = False
        self._last_fire_print_time = 0.0

        self._orig_add_move = None
        self._trap_installed = False
        self._pending_fire = False
        self._admitting_crash_script = False

        self.printer.register_event_handler('klippy:connect', self._on_connect)
        self.printer.register_event_handler('klippy:ready', self._on_ready)
        self.printer.register_event_handler('homing:home_rails_begin', self._on_home_rails_begin)
        self.printer.register_event_handler('homing:home_rails_end', self._on_home_rails_end)

        self.gcode.register_command('START_TOOL_CRASH_DETECTION', self.cmd_START_TOOL_CRASH_DETECTION, desc=self.cmd_START_TOOL_CRASH_DETECTION_help)
        self.gcode.register_command('STOP_TOOL_CRASH_DETECTION', self.cmd_STOP_TOOL_CRASH_DETECTION, desc=self.cmd_STOP_TOOL_CRASH_DETECTION_help)

        buttons = self.printer.load_object(config, 'buttons')
        ppins = self.printer.lookup_object('pins')
        for section in config.get_prefix_sections('tool '):
            p = ppins.parse_pin(section.get('detection_pin'), can_invert=True, can_pullup=True)
            base = f"{p['chip_name']}:{p['pin']}"
            ppins.allow_multi_use_pin(base)
            buttons.register_buttons([base], self._on_detect_edge)

    def _on_connect(self):
        self.toolhead = self.printer.lookup_object('toolhead')
        try:
            zrail = self.toolhead.get_kinematics().rails[2]
            self._z_stepper_names = {s.get_name() for s in zrail.get_steppers()}
        except Exception:
            logging.warning('tool_crash: unable to resolve Z rail steppers; probing filter disabled.')
            self._z_stepper_names = set()

    def _on_ready(self):
        self._sync_expected(initial=True)

    def _on_home_rails_begin(self, hstate, rails):
        if IGN_PROBING not in self.ignore or not self._z_stepper_names:
            return
        try:
            for r in rails:
                for s in r.get_steppers():
                    if s.get_name() in self._z_stepper_names:
                        self._z_home_active += 1
                        self._z_home_sticky = True
                        self.gcode.respond_info('tool_crash: Z home begin (depth=%d)' % self._z_home_active)
                        if self._trap_installed:
                            self._remove_trap()
                        return
        except Exception:
            pass

    def _on_home_rails_end(self, hstate, rails):
        if self._z_home_active:
            self._z_home_active -= 1
            self.gcode.respond_info('tool_crash: Z home end (depth=%d)' % self._z_home_active)
        if self._z_home_active == 0 and (IGN_PROBING in self.ignore):
            if not self._z_sticky_cb_pending:
                self._z_sticky_cb_pending = True
                self.toolhead.register_lookahead_callback(self._z_sticky_clear_cb)

    def _z_sticky_clear_cb(self, _pt):
        if self._z_home_active > 0:
            self.toolhead.register_lookahead_callback(self._z_sticky_clear_cb)
            return
        self._z_home_sticky = False
        self._z_sticky_cb_pending = False
        if self.enabled:
            self._sync_expected()
            self._evaluate_state(self._now_pt(), force_arm=True)

    def _on_detect_edge(self, eventtime, is_triggered):
        if not self.enabled:
            return
        self._sync_expected()
        self._evaluate_state(self.toolhead.mcu.estimated_print_time(eventtime))

    def _now_pt(self):
        return self.toolhead.mcu.estimated_print_time(self.reactor.monotonic())

    def _sync_expected(self, initial=False):
        if not self.auto_on or self._manual_expected:
            return
        if self.toolchanger.status in (STATUS_CHANGING, STATUS_INITIALIZING):
            return
        new_expected = self.toolchanger.active_tool
        if new_expected is not self.expected_tool or initial:
            self.expected_tool = new_expected
            self._armed_since_pt = None

    def _expected_now(self):
        return self.expected_tool if self._manual_expected else self.toolchanger.active_tool

    def _evaluate_state(self, mcu_pt, force_arm=False):
        det = self.toolchanger.detected_tool
        expect = self._expected_now()
        mismatch = (det is not None) if expect is None else (det is not expect)
        if mismatch or force_arm:
            if self._armed_since_pt is None:
                self._armed_since_pt = mcu_pt
                self._arm_seq += 1
            seq = self._arm_seq
            if IGN_STANDSTILL in self.ignore:
                self._install_trap(seq)
                return
            def _timer(evt):
                if seq != self._arm_seq:
                    return self.reactor.NEVER
                now_pt = self.toolhead.mcu.estimated_print_time(evt)
                if self._gate_allows_fire() and (now_pt - self._armed_since_pt) >= self.crash_mintime:
                    self._fire(now_pt)
                    return self.reactor.NEVER
                return evt + 0.050
            self.reactor.register_timer(_timer, self.reactor.monotonic() + self.crash_mintime)
        else:
            self._armed_since_pt = None

    def _gate_allows_fire(self):
        if self.toolchanger.status in (STATUS_CHANGING, STATUS_INITIALIZING) and (IGN_TOOLCHANGES in self.ignore):
            return False
        if (IGN_PROBING in self.ignore) and (self._z_home_active or self._z_home_sticky):
            return False
        return True

    def _install_trap(self, seq):
        if self._trap_installed or not self._gate_allows_fire():
            return
        self._trap_installed = True
        self.gcode.respond_info('tool_crash: armed pre-admission trap (seq=%d)' % seq)
        if self._orig_add_move is None:
            self._orig_add_move = self.toolhead.lookahead.add_move
        def _wrapper(move):
            if not self.enabled or seq != self._arm_seq:
                self._remove_trap()
                return None
            if self._admitting_crash_script:
                return self._orig_add_move(move)
            if not self._gate_allows_fire():
                self._remove_trap()
                return None
            if not self._pending_fire:
                self._pending_fire = True
                self.gcode.respond_info('tool_crash: blocking client move; scheduling crash action')
                self.reactor.register_callback(self._deferred_fire_cb)
            return None
        self.toolhead.lookahead.add_move = _wrapper

    def _remove_trap(self):
        if not self._trap_installed:
            return
        try:
            if self._orig_add_move is not None:
                self.toolhead.lookahead.add_move = self._orig_add_move
        except Exception:
            pass
        self._trap_installed = False
        self._orig_add_move = None
        self._pending_fire = False

    def _deferred_fire_cb(self, evt):
        self._pending_fire = False
        self._admitting_crash_script = True
        try:
            now_pt = self.toolhead.mcu.estimated_print_time(evt)
            self._fire(now_pt)
        finally:
            self._admitting_crash_script = False
        if not self.enabled:
            self._remove_trap()
            return self.reactor.NEVER
        det = self.toolchanger.detected_tool
        expect = self._expected_now()
        mismatch_still = (det is not None) if expect is None else (det is not expect)
        if (IGN_STANDSTILL in self.ignore) and mismatch_still and self._gate_allows_fire():
            self._trap_installed = True
        else:
            self._remove_trap()
        return self.reactor.NEVER

    def _fire(self, mcu_pt):
        if not self.enabled:
            return
        if (mcu_pt - self._last_fire_print_time) < self.crash_mintime:
            return
        if not self._gate_allows_fire():
            return
        self._last_fire_print_time = mcu_pt
        self._armed_since_pt = None
        ctx = {
            'expected': (self._expected_now().name if self._expected_now() else None),
            'detected': (self.toolchanger.detected_tool.name if self.toolchanger.detected_tool else None),
            'active': (self.toolchanger.active_tool.name if self.toolchanger.active_tool else None),
        }
        if self.crash_gcode is not None:
            try:
                self.gcode.respond_info('tool_crash: running crash_gcode')
                script = self.crash_gcode.render(ctx)
                self.gcode.run_script_from_command(script)
            except Exception as e:
                self.printer.invoke_shutdown(f"TOOL_CRASH | error running script. {e}")
        else:
            self.printer.invoke_shutdown(f"TOOL_CRASH | expected={ctx['expected']} detected={ctx['detected']} active={ctx['active']}")

    cmd_START_TOOL_CRASH_DETECTION_help = (
        "Enable tool crash detection. Optional T=<num>|TOOL=<name> to set the expected tool; use T=-1 for 'expect none'."
    )
    def cmd_START_TOOL_CRASH_DETECTION(self, gcmd):
        expected = self.toolchanger.gcmd_tool(gcmd, default=None)
        self._manual_expected = expected is not None
        if expected is None:
            expected = self.toolchanger.active_tool
        self.expected_tool = expected
        if expected is not None:
            actual = self.toolchanger.detected_tool
            if actual is not expected:
                raise gcmd.error('tool_crash: cannot enable — expected %s is not currently detected' % (expected.name,))
        self.enabled = True
        gcmd.respond_info(f'tool_crash: enabled for {self.expected_tool.name if self.expected_tool else None}')

    cmd_STOP_TOOL_CRASH_DETECTION_help = "Disable tool crash detection."
    def cmd_STOP_TOOL_CRASH_DETECTION(self, gcmd):
        self._arm_seq += 1
        def _disable_cb(_pt):
            self.enabled = False
            self._armed_since_pt = None
            self._manual_expected = False
            self._pending_fire = False
            self._admitting_crash_script = False
            self._remove_trap()
        self.toolhead.register_lookahead_callback(_disable_cb)
        gcmd.respond_info('tool_crash: disabled')

def load_config(config):
    return ToolCrash(config)
