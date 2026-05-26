#!/usr/bin/env python3
"""
Post-processor: auto-disable temps for tools that won't be used again.
    python3 disable_unused_tool_temps.py /path/to/file.gcode
"""

import sys, pathlib, re, traceback

# ── regular expressions ──────────────────────────────────────────
pat_T = re.compile(r'^\s*(T(\d+))\s*$', re.I)


def patch_file(path_str: str) -> None:
    p     = pathlib.Path(path_str)
    lines = p.read_text(encoding='utf-8', errors='ignore').splitlines()

    last_use = {}
    for idx, ln in enumerate(lines):
        m = pat_T.match(ln)
        if m:
            last_use[int(m.group(2))] = idx

    out          = []
    pending_cool = None
    i            = 0

    while i < len(lines):
        ln = lines[i]
        mT = pat_T.match(ln)

        if mT:
            tool = int(mT.group(2))
            out.append(ln)
            if pending_cool is not None:
                out.append(f'M104 S0 T{pending_cool} ; auto-off unused')
                pending_cool = None
            if i == last_use[tool]:
                pending_cool = tool
            i += 1
            continue
        out.append(ln)
        i += 1
    lines = out
    lines.append('; === DISABLE_UNUSED_TOOL_TEMPS PATCHED ===')
    p.write_text('\n'.join(lines), encoding='utf-8')


if len(sys.argv) < 2:
    print("Usage: python disable_unused_tool_temps.py input_file")
else:
    patch_file(sys.argv[1])