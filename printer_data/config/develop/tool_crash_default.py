import logging
from .toolchanger import STATUS_CHANGING, STATUS_INITIALIZING

IGN_TOOLCHANGES = 'toolchanges'
IGN_PROBING = 'probing'
IGN_ALL = 'all'
_ALLOWED_IGNORES = {IGN_TOOLCHANGES, IGN_PROBING, IGN_ALL}

class ToolCrash:
    cmd_START_TOOL_CRASH_DETECTION_help = "Enable tool crash detection. Optional T=<num>|TOOL=<name> to set the expected tool"
    cmd_STOP_TOOL_CRASH_DETECTION_help = "Disable tool crash detection."

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
        self.ignore = {IGN_TOOLCHANGES, IGN_PROBING} if IGN_ALL in raw else raw

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
        self._arm_is_manual = False
        self._z_stepper_names = set()
        self._probe_depth = 0
        self._last_fire_print_time = 0.0
        self._watchdog_timer = None

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
            msg = 'tool_crash: unable to resolve Z rail steppers; probing filter disabled.'
            logging.warning(msg)
            self._z_stepper_names = set()

    def _on_ready(self):
        self._sync_expected(initial=True)
        if self.enabled:
            self._ensure_watchdog()

    def _rails_include_z(self, rails):
        if not self._z_stepper_names:
            return False
        try:
            for r in rails:
                for s in r.get_steppers():
                    if s.get_name() in self._z_stepper_names:
                        return True
        except Exception:
            pass
        return False

    def _on_home_rails_begin(self, hstate, rails):
        if IGN_PROBING not in self.ignore:
            return
        if self._rails_include_z(rails):
            self._probe_depth += 1
            # self.gcode.respond_info(f"tool_crash: Z home begin (depth={self._probe_depth})")

    def _on_home_rails_end(self, hstate, rails):
        if IGN_PROBING not in self.ignore:
            return
        if self._rails_include_z(rails):
            if self._probe_depth:
                self._probe_depth -= 1
            # self.gcode.respond_info(f"tool_crash: Z home end (depth={self._probe_depth})")
            if self.enabled and self._probe_depth == 0:
                self._evaluate_state(self._now_pt())

    def _now_pt(self):
        return self.toolhead.mcu.estimated_print_time(self.reactor.monotonic())

    def _ensure_watchdog(self):
        if self._watchdog_timer is not None:
            return
        def _tick(evt):
            if not self.enabled:
                self._watchdog_timer = None
                return self.reactor.NEVER
            self._sync_expected()
            self._evaluate_state(self._now_pt())
            return evt + 0.100
        self._watchdog_timer = self.reactor.register_timer(_tick, self.reactor.monotonic() + 0.100)

    def _cancel_watchdog(self):
        if self._watchdog_timer is not None:
            self.reactor.unregister_timer(self._watchdog_timer)
            self._watchdog_timer = None

    def _sync_expected(self, initial=False):
        if not self.auto_on or self._manual_expected:
            return
        if self.toolchanger.status in (STATUS_CHANGING, STATUS_INITIALIZING):
            return
        new_expected = self.toolchanger.active_tool
        if new_expected is not self.expected_tool or initial:
            self.expected_tool = new_expected
            if not self._arm_is_manual:
                self._armed_since_pt = None

    def _on_detect_edge(self, eventtime, is_triggered):
        if not self.enabled:
            return
        self._sync_expected()
        self._evaluate_state(self.toolhead.mcu.estimated_print_time(eventtime))

    def _evaluate_state(self, mcu_pt, force_arm=False):
        det = self.toolchanger.detected_tool
        expect = self.expected_tool
        mismatch = (det is not None) if expect is None else (det is not expect)
        if (self.toolchanger.status in (STATUS_CHANGING, STATUS_INITIALIZING)) and (IGN_TOOLCHANGES in self.ignore) and not force_arm:
            return
        if mismatch or force_arm:
            if self._armed_since_pt is None:
                self._armed_since_pt = mcu_pt
                self._arm_is_manual = force_arm
                self._arm_seq += 1

            seq = self._arm_seq

            def _timer(evt):
                if seq != self._arm_seq:
                    return self.reactor.NEVER
                now_pt = self.toolhead.mcu.estimated_print_time(evt)
                arm_pt = self._armed_since_pt
                if arm_pt is None:
                    return self.reactor.NEVER
                det_now = self.toolchanger.detected_tool
                expect_now = self.expected_tool
                mismatch_now = (det_now is not None) if expect_now is None else (det_now is not expect_now)
                if not mismatch_now:
                    self._armed_since_pt = None
                    self._arm_is_manual = False
                    return self.reactor.NEVER
                if self._gate_allows_fire() and (now_pt - arm_pt) >= self.crash_mintime:
                    self._fire(now_pt)
                    return self.reactor.NEVER
                return evt + 0.050
            
            self.reactor.register_timer(_timer, self.reactor.monotonic() + self.crash_mintime)
        else:
            self._armed_since_pt = None
            self._arm_is_manual = False

    def _gate_allows_fire(self):
        if self.toolchanger.status in (STATUS_CHANGING, STATUS_INITIALIZING) and (IGN_TOOLCHANGES in self.ignore):
            return False
        if self._probe_depth and (IGN_PROBING in self.ignore):
            return False
        return True

    def _fire(self, mcu_pt):
        if not self.enabled:
            return
        if (mcu_pt - self._last_fire_print_time) < self.crash_mintime:
            return
        self._last_fire_print_time = mcu_pt
        self.enabled = False
        self._arm_seq += 1
        self._armed_since_pt = None
        self._arm_is_manual = False
        self._cancel_watchdog()
        ctx = {
            'expected': (self.expected_tool.name if self.expected_tool else None),
            'detected': (self.toolchanger.detected_tool.name if self.toolchanger.detected_tool else None),
            'active': (self.toolchanger.active_tool.name if self.toolchanger.active_tool else None),
        }
        if self.crash_gcode is not None:
            try:
                _gcode = self.crash_gcode.render(ctx)
                self.gcode.respond_info("tool_crash: running crash_gcode")
                self.gcode.run_script_from_command(_gcode)
            except Exception as e:
                logging.exception(f"tool_crash: crash script error {e}")
                self.printer.invoke_shutdown(f"TOOL_CRASH | script error: {e}")
        else:
            self.printer.invoke_shutdown(f"TOOL_CRASH | expected {ctx['expected']} got {ctx['detected']}")

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
        self._ensure_watchdog()
        self._evaluate_state(self._now_pt(), force_arm=True)
        gcmd.respond_info(f'tool_crash: enabled for {self.expected_tool.name if self.expected_tool else None}')

    def cmd_STOP_TOOL_CRASH_DETECTION(self, gcmd):
        self._arm_seq += 1
        def _disable_cb(_pt):
            self.enabled = False
            self._armed_since_pt = None
            self._manual_expected = False
            self._arm_is_manual = False
            self._cancel_watchdog()
        self.toolhead.register_lookahead_callback(_disable_cb)
        gcmd.respond_info('tool_crash: disabled')

def load_config(config):
    return ToolCrash(config)
