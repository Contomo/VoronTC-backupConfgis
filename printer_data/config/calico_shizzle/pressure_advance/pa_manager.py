# Pressure Advance Manager for Klipper
#
# Copyright (C) 2025 Eric Billmeyer <eric.billmeyer@freenet.de>
#
# This file may be distributed under the terms of the GNU GPLv3 license.

import os, json, logging, ast, copy, math, re
from collections import OrderedDict

MATERIAL_SYNONYM_GROUPS = [
    {"abs", "asa"},
    {"petg", "pctg"},
    {"pa12", "nylon", "pa", "pa6"},
]

PA_PROFILE_TO_GCODE_MAP = OrderedDict([
    ('pressure_advance_model', 'MODEL'),
    ('pressure_advance', 'ADVANCE'),          # For linear model
    ('linear_advance', 'ADVANCE'),            # For non-linear model
    ('nonlinear_offset', 'OFFSET'),
    ('linearization_velocity', 'VELOCITY'),
    ('pressure_advance_smooth_time', 'SMOOTH_TIME'),
    ('pressure_advance_time_offset', 'TIME_OFFSET'),
])

TOKEN_SPLIT_RE = re.compile(r"[^a-z0-9]+")
PARAMETER_ALIASES = {
    "MATERIAL": ("filament", "type"),
    "FILAMENT": ("filament", "type"),
    "FILAMENT_TYPE": ("filament", "type"),
    "FILAMENT_DIAMETER": ("filament", "diameter"),
    "NOZZLE": ("tool_info", "nozzle"),
    "NOZZLE_DIAMETER": ("tool_info", "nozzle"),
    "NOZZLE_TYPE": ("tool_info", "nozzle_type"),
    "NOZZLE_MATERIAL": ("tool_info", "nozzle_material"),
}

def _build_synonym_lookup(groups):
    lookup = {}
    for group in groups:
        canonical = sorted([term.lower() for term in group])[0]
        for term in group:
            lookup[term.lower()] = canonical
    return lookup

SYNONYM_LOOKUP = _build_synonym_lookup(MATERIAL_SYNONYM_GROUPS)

def tokenize_string(value):
    if value is None:
        return []
    text = str(value).strip().lower()
    tokens = [tok for tok in TOKEN_SPLIT_RE.split(text) if tok]
    return [SYNONYM_LOOKUP.get(tok, tok) for tok in tokens]

# ------------------------ similarities ------------------------ 
def numeric_similarity(a, b):
    if not all(math.isfinite(val) for val in (a, b)):
        return 0.0
    diff = abs(a - b)
    scale = max(abs(a), abs(b), 1.0)
    score = 1.0 - diff / scale
    return max(0.0, min(1.0, score))

def string_similarity(a, b):
    tokens_a = tokenize_string(a)
    tokens_b = tokenize_string(b)
    if not tokens_a and not tokens_b:
        return 1.0
    if not tokens_a or not tokens_b:
        return 0.0
    set_a = set(tokens_a)
    set_b = set(tokens_b)
    intersection = len(set_a & set_b)
    denom = max(len(set_a), len(set_b), 1)
    base_score = intersection / denom
    return max(0.0, min(1.0, base_score))

def compute_similarity(desired, actual):
    if actual is None:
        return 0.0
    if desired is None:
        return 0.0
    desired_bool, desired_bool_val = coerce_bool(desired)
    actual_bool, actual_bool_val = coerce_bool(actual)
    if desired_bool and actual_bool:
        return 1.0 if desired_bool_val == actual_bool_val else 0.0
    desired_num, desired_num_val = coerce_float(desired)
    actual_num, actual_num_val = coerce_float(actual)
    if desired_num and actual_num:
        return numeric_similarity(desired_num_val, actual_num_val)
    if isinstance(desired, (list, tuple, set)) or isinstance(
        actual, (list, tuple, set)
    ):
        desired_text = " ".join(map(str, desired))
        actual_text = " ".join(map(str, actual))
        return string_similarity(desired_text, actual_text)
    return string_similarity(desired, actual)

# ------------------------ coerce ------------------------ 
def coerce_bool(value):
    if isinstance(value, bool):
        return True, value
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in ("true", "false", "yes", "no", "on", "off", "1", "0"):
            return True, lowered in ("true", "yes", "on", "1")
    return False, None

def coerce_float(value):
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return True, float(value)
    if isinstance(value, str):
        stripped = value.strip()
        try:
            return True, float(stripped)
        except ValueError:
            return False, None
    return False, None

# ------------------------ Dick helpers ------------------------ 
def set_nested_value(target, path, value):
    if not path:
        return
    node = target
    for key in path[:-1]:
        key = key.lower()
        child = node.get(key)
        if not isinstance(child, dict):
            child = {}
            node[key] = child
        node = child
    node[path[-1].lower()] = value


def deep_merge_dict(dest, src):
    for key, value in src.items():
        if isinstance(value, dict) and isinstance(dest.get(key), dict):
            deep_merge_dict(dest[key], value)
        else:
            dest[key] = copy.deepcopy(value)
    return dest

def get_value_by_path(data, path):
    node = data
    for key in path:
        if not isinstance(node, dict):
            return None
        node = node.get(key)
        if node is None:
            return None
    return node

def key_to_path(key):
    alias = PARAMETER_ALIASES.get(key.upper())
    if alias:
        return alias
    parts = [segment.strip().lower() for segment in key.split(".") if segment]
    return tuple(parts)


def flatten_query(data):
    flat = {}

    def _walk(node, path):
        if isinstance(node, dict):
            for key, value in node.items():
                _walk(value, path + (key.lower(),))
        else:
            if path and node is not None:
                flat[path] = node

    _walk(data, tuple())
    return flat

def normalize_dict_keys(obj):
    if isinstance(obj, dict):
        normalized = {}
        for key, value in obj.items():
            lowered = str(key).lower()
            normalized[lowered] = normalize_dict_keys(value)
        return normalized
    if isinstance(obj, list):
        return [normalize_dict_keys(item) for item in obj]
    return obj

def parse_profile_date(profile):
    raw = profile.get("date")
    if raw is None:
        return None
    text = str(raw).strip()
    if not text:
        return None
    parts = [part for part in re.split(r"\D+", text) if part]
    if len(parts) != 3:
        return None
    if len(parts[0]) == 4:
        year, month, day = parts
    elif len(parts[2]) == 4:
        day, month, year = parts
    else:
        day, month, year = parts
    try:
        year = int(year)
        month = int(month)
        day = int(day)
    except ValueError:
        return None
    if not (1 <= month <= 12 and 1 <= day <= 31):
        return None
    return (year, month, day)

# ------------------------ scoring ------------------------ 
def score_profile(profile, query_flat):
    contributions = []
    total_weight = 0.0
    score_sum = 0.0
    match_count = 0
    for path, desired in query_flat.items():
        actual = get_value_by_path(profile, path)
        similarity = compute_similarity(desired, actual)
        total_weight += 1.0
        score_sum += similarity
        if similarity > 0.0:
            match_count += 1
        contributions.append(
            {
                "path": path,
                "desired": desired,
                "actual": actual,
                "similarity": similarity,
            }
        )
    normalized = (score_sum / total_weight * 100.0) if total_weight else 0.0
    return {
        "score": normalized,
        "weight": total_weight,
        "match_count": match_count,
        "details": contributions,
    }


def describe_profile(profile):
    filament = profile.get("filament", {})
    tool_info = profile.get("tool_info", {})
    pa_info = profile.get("pa_info", {})
    parts = []
    if filament.get("type"):
        parts.append(str(filament["type"]))
    if tool_info.get("hotend"):
        parts.append(str(tool_info["hotend"]))
    if tool_info.get("nozzle"):
        parts.append(f"nozzle {tool_info['nozzle']}")
    if pa_info.get("pa_type"):
        parts.append(str(pa_info["pa_type"]))
    return " / ".join(parts) or profile.get("source", "unknown profile")


def format_score_details(contributions, limit=5):
    if not contributions:
        return "No comparable parameters."
    ordered = sorted(contributions, key=lambda item: item["similarity"])
    lines = []
    for entry in ordered[:limit]:
        path = ".".join(entry["path"])
        desired = entry["desired"]
        actual = entry["actual"] if entry["actual"] is not None else "missing"
        lines.append(
            f"{path}: wanted '{desired}' vs '{actual}' ({entry['similarity']*100:.0f})"
        )
    return "; ".join(lines)

def get_config_prefix_options(config, prefix):
    query = {}
    for opt in config.get_prefix_options(prefix):
        raw_value = config.get(opt)
        path = opt[len(prefix) :]
        try:
            value = ast.literal_eval(raw_value)
        except Exception as e:
            raise config.error(f'unable to parse {opt} : {str(e)}')
        segments = [segment for segment in path.split(".") if segment]
        set_nested_value(query, tuple(segment.lower() for segment in segments), value)
    return query

def load_models_from_disk(file_path):
    path = os.path.expanduser(file_path)
    try:
        with open(path, "r") as f:
            data = json.load(f)
    except FileNotFoundError as e:
        raise RuntimeError(f"models_file not found: {path}") from e
    except json.JSONDecodeError as e:
        raise RuntimeError(f"JSON parse error in {path}: {e}") from e
    
    if not isinstance(data, dict):
        raise RuntimeError(
            f"JSON root must be an object with a list of profiles, got {type(data).__name__}"
            )
    if "profiles" not in data:
        raise RuntimeError("JSON root object must contain a 'profiles' key")
    profiles = data.pop("profiles")
    metadata = data
    if not isinstance(profiles, list):
        raise RuntimeError("'profiles' key must contain a JSON list")
    return profiles, metadata


class PressureAdvanceManager:
    def __init__(self, config):
        self.printer = config.get_printer()
        self.gcode = self.printer.lookup_object('gcode')

        _path = config.get('models_file', '~/printer_data/pa_profiles.json')
        self.models_file_path = os.path.expanduser(_path)
        self.min_score = config.getfloat('min_score', 75.0, minval=0.0, maxval=100.0)

        self.default_query = get_config_prefix_options(config, "params_")

        self.pa_models = []
        self.pa_metadata = {}

        try:
            profiles, metadata = load_models_from_disk(self.models_file_path)
            self.pa_models = [normalize_dict_keys(profile) for profile in profiles]
            self.pa_metadata = normalize_dict_keys(metadata)
        except Exception as e:
            raise config.error(f"Failed to load models_file: {e}")

        self.gcode.register_command('LOAD_PA_PROFILE',
                                    self.cmd_LOAD_PA_PROFILE,
                                    desc=self.cmd_LOAD_PA_PROFILE_help)

    cmd_LOAD_PA_PROFILE_help = """LOAD_PA_PROFILE [EXTRUDER=name] [path.to.field=value ...] - 
                                  Selects the best pressure advance profile and applies it."""
    def cmd_LOAD_PA_PROFILE(self, gcmd):
        if not self.pa_models:
            raise gcmd.error("PA_MANAGER: no profiles loaded. Check models_file: %s"
                                                            % (self.models_file_path,) )
        params = dict(gcmd.get_command_parameters())

        extruder_name = params.pop("EXTRUDER", None)
        if extruder_name:
            extruder = self.printer.lookup_object(extruder_name, None)
        else:
            extruder = self.printer.lookup_object("toolhead").get_extruder()
        if extruder is None:
            raise gcmd.error("PA_MANAGER: unable to resolve extruder.")
        
        # defaults from extruder
        tree = {}
        nozzle = getattr(extruder, "nozzle_diameter", None)
        if nozzle:
            set_nested_value(tree, ("tool_info", "nozzle"), nozzle)
        filament_area = getattr(extruder, "filament_area", None)
        if filament_area:
            diameter = 2.0 * math.sqrt(filament_area / math.pi)
            set_nested_value(tree, ("filament", "diameter"), diameter)

        # parameter overwrites
        for key, value in params.items():
            path = key_to_path(key)
            if not path:
                continue
            try:
                value = ast.literal_eval(value)
            except:
                pass
            set_nested_value(tree, path, value)

        query = copy.deepcopy(self.default_query)
        # shove them all into one
        deep_merge_dict(query, tree)

        query_flat = flatten_query(query)
        if not query_flat:
            raise gcmd.error("PA_MANAGER: no parameters were provided for scoring.")

        best_profile, best_result = self._select_profile(query_flat)
        if not best_profile or not best_result:
            raise gcmd.error("PA_MANAGER: unable to select a matching profile.")

        if best_result["score"] < self.min_score:
            detail = format_score_details(best_result["details"])
            description = describe_profile(best_profile)
            raise gcmd.error(
                "PA_MANAGER: best profile '%s' scored %.1f which is below min_score %.1f. %s"
                % (description, best_result["score"], self.min_score, detail)
            )

        applied_summary = self._apply_profile(best_profile, gcmd, extruder_name)
        message = (
            "PA_MANAGER: applied profile '%s' (score %.1f) using %s"
            % (describe_profile(best_profile), best_result["score"], applied_summary)
        )
        logging.info(message)
        gcmd.respond_info(message)

    def _select_profile(self, query_flat):
        best_profile = None
        best_result = None
        for profile in self.pa_models:
            result = score_profile(profile, query_flat)
            if best_result is None:
                best_profile = profile
                best_result = result
                continue
            if result["score"] > best_result["score"]:
                best_profile = profile
                best_result = result
                continue
            if math.isclose(result["score"], best_result["score"]) and result[
                "match_count"
            ] > best_result["match_count"]:
                best_profile = profile
                best_result = result
                continue
            if math.isclose(result["score"], best_result["score"]) and result[
                "match_count"
            ] == best_result["match_count"]:
                best_date = parse_profile_date(best_profile)
                candidate_date = parse_profile_date(profile)
                if candidate_date and (not best_date or candidate_date > best_date):
                    best_profile = profile
                    best_result = result
        return best_profile, best_result

    def _apply_profile(self, profile, gcmd, extruder_name=None):
        pa_payload = profile.get("pa_info") or profile
        cmd_parts = ["SET_PRESSURE_ADVANCE"]
        if extruder_name:
            cmd_parts.append(f"EXTRUDER={extruder_name}")
        applied_keys = []
        for field, gcode_key in PA_PROFILE_TO_GCODE_MAP.items():
            if field not in pa_payload:
                continue
            value = pa_payload[field]
            if value is None:
                continue
            if isinstance(value, float):
                formatted = f"{value:.6g}"
            else:
                formatted = str(value)
            applied_keys.append(f"{gcode_key}={formatted}")
        if len(applied_keys) == 0:
            raise gcmd.error(
                "PA_MANAGER: selected profile does not contain any PA parameters."
            )
        cmd_parts.extend(applied_keys)
        self.gcode.run_script_from_command(" ".join(cmd_parts))
        return ", ".join(applied_keys)

    def get_status(self, eventtime):
        return {
            "profiles_path": self.models_file_path,
            "profiles_count": len(self.pa_models),
            "profiles": self.pa_models,
            "metadata": self.pa_metadata,
            "min_score": self.min_score,
        }


def load_config(config):
    return PressureAdvanceManager(config)
