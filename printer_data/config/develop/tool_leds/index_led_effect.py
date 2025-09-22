
# revision - 0.1

try:
    from .extras import led_effect as led_effect_mod
except:
    led_effect_mod = None

class _EffectConfigShim:
    def __init__(self, base_cfg, printer, child_name, leds_line, layers_text, overrides):
        self._base = base_cfg
        self._printer = printer
        self._name = f"led_effect {child_name}"
        self._overrides = dict(overrides)
        self._overrides["leds"] = leds_line.strip()
        self._overrides["layers"] = layers_text

    def get_printer(self): return self._printer
    def get_name(self): return self._name

    def get(self, key, default=None):
        if key in self._overrides:
            return self._overrides[key]
        return self._base.get(key, default)

    def getint(self, key, default=None, **kw):
        v = self.get(key, default)
        return None if v is None else int(v)

    def getfloat(self, key, default=None, **kw):
        v = self.get(key, default)
        return None if v is None else float(v)

    def getboolean(self, key, default=None, **kw):
        v = self.get(key, default)
        if v is None: return default
        return str(v).strip().lower() in ("1","true","yes","on")

    def getlist(self, key, default=None):  # comma-separated list expected by led_effect for button_pins etc.
        v = self.get(key, None)
        if v is None: return default
        if isinstance(v, (list, tuple)): return list(v)
        return [x.strip() for x in str(v).replace("\n", " ").split(",") if x.strip()]

    # called by led_effect
    def get_prefix_sections(self, *a, **k): return []
    def getsection(self, *a, **k): return self

class idxLedEffect:
    # keys we will forward per child if present in group config
    _BIND_KEYS = ("heater", "analog_pin", "button_pins", "stepper")
    _PASSTHROUGH_KEYS = ("fadetime", "frame_rate", "autostart", "run_on_error", "recalculate", "endstops")

    def __init__(self, config):
        if led_effect_mod is None:
            raise config.error('index led effect is an extention to LED_EFFECT. Please install LED_EFFECT first.')

        self.config   = config
        self.printer  = config.get_printer()
        self.gcode    = self.printer.lookup_object('gcode')
        self.name     = config.get_name().split()[1]

        self.layers_text = config.get('layers')
        self.leds_text   = config.get('leds')

        self._raw_lists = {}
        for k in self._BIND_KEYS:
            vals = str(config.get(k, '')).replace('\n', ',')
            self._raw_lists[k] = tuple(x.strip() for x in vals.split(',') if x.strip())
        self.tools_by_idx = self._load_all_tools_indexed(config)

        self.children = []
        self._index_to_child = {}

        self.gcode.register_mux_command('SET_IDX_LED_EFFECT', 'EFFECT', self.name,
                                        self.cmd_SET_IDX_LED_EFFECT,
                                        desc=self.cmd_SET_IDX_LED_EFFECT_help)

        self._build_children()

    def _iter_led_lines(self):
        for ln in self.leds_text.splitlines():
            s = ln.strip()
            if s:
                yield s

    def _load_all_tools_indexed(self, config):
        try:
            tool_secs = config.get_prefix_sections('tool ')
        except Exception:
            return []
        tools = []
        for sec in tool_secs:
            try:
                tool = self.printer.load_object(sec, sec.get_name())
                tools.append(tool)
            except Exception:
                continue
        tools.sort(key=lambda t: getattr(t, 'tool_number', 1 << 30))
        return tools

    def _derive_values_for_children(self, key, child_count):
        vals = list(self._raw_lists.get(key) or [])
        if not vals:
            if key == "heater":
                out = []
                for i in range(child_count):
                    ex = getattr(self.tools_by_idx[i], 'extruder_name', None) if i < len(self.tools_by_idx) else None
                    out.append(ex)
                return out
            return [None] * child_count

        if len(vals) == 1 and child_count > 1:
            return [vals[0]] * child_count

        if len(vals) != child_count:
            raise self.config.error(f"[tool_led_effect {self.name}] {key} list length {len(vals)} must be 1 or {child_count}")
        return vals

    def _build_children(self):
        led_lines = list(self._iter_led_lines())
        if not led_lines:
            raise self.config.error(f"[tool_led_effect {self.name}] requires at least one leds: line")

        N = len(led_lines)

        per_child = {}
        for k in self._BIND_KEYS:
            per_child[k] = self._derive_values_for_children(k, N)

        passthrough = {k: self.config.get(k) for k in self._PASSTHROUGH_KEYS if self.config.get(k, None) is not None}

        for i, line in enumerate(led_lines):
            overrides = dict(passthrough)

            for k in self._BIND_KEYS:
                v = per_child[k][i]
                if v not in (None, ""):
                    overrides[k] = v

            child_name = f"{self.name}__{i}"
            shim = _EffectConfigShim(self.config, self.printer, child_name, line, self.layers_text, overrides)
            eff = led_effect_mod.ledEffect(shim)
            self.children.append({"index": i, "line": line, "effect": eff})
            self._index_to_child[i] = self.children[-1]

    # ---------- mux command: pure fan-out ----------
    cmd_SET_IDX_LED_EFFECT_help = "SET_IDX_LED_EFFECT EFFECT=<group> [IDX=0,2,3] [any params supported by SET_LED_EFFECT]"

    def cmd_SET_IDX_LED_EFFECT(self, gcmd):
        idx_param = gcmd.get('IDX', None)
        if idx_param is not None:
            try:
                idxs = [int(x.strip()) for x in str(idx_param).split(',') if x.strip()]
            except Exception:
                raise gcmd.error(f"[{self.name}] IDX must be comma-separated integers")
        else:
            idxs = sorted(self._index_to_child.keys())

        params = dict(gcmd.get_command_parameters() or {})
        params.pop('IDX', None)
        params.pop('EFFECT', None)

        tail = " ".join(f"{k}='{v}'" for k, v in params.items()) if params else ""

        for i in idxs:
            rec = self._index_to_child.get(i)
            if not rec:
                self.gcode.respond_info(f"[{self.name}] ignoring unknown child index {i}")
                continue
            child = rec["effect"]
            self.gcode.run_script_from_command(f"SET_LED_EFFECT EFFECT={child.name}" + (f" {tail}" if tail else ""))

    def child_count(self): 
        return len(self.children)

def load_config_prefix(config):
    return idxLedEffect(config)
