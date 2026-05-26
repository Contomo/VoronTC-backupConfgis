#!/usr/bin/env python3
import sys
import re
from pathlib import Path


PRIME_TAGS = ("prime tower", "wipe_tower")
END_PREFIXES: tuple[str, ...] = ()
NON_OBJECT_CMD_PREFIXES = ("M104", "M109", "T")
END_TAGS = ("; WIPE_TOWER_END", "; WIPE_TOWER_BRIM_END")


def parse_xy(line: str):
    """Return (x, y) from a move line if present, else (None, None)."""
    m_x = re.search(r"\bX(-?\d+(\.\d*)?)", line)
    m_y = re.search(r"\bY(-?\d+(\.\d*)?)", line)
    x = float(m_x.group(1)) if m_x else None
    y = float(m_y.group(1)) if m_y else None
    return x, y


def is_prime_indicator(line: str) -> bool:
    """True if the line marks the prime/wipe tower region."""
    lower = line.lower()
    if "wipe_tower_end" in lower or "wipe_tower_brim_end" in lower:
        return False
    return any(tag in lower for tag in PRIME_TAGS)


def is_prime_end(line: str) -> bool:
    """True if the line is a clear boundary back to normal printing."""
    stripped = line.lstrip()
    if stripped.startswith(END_TAGS):
        return True
    if stripped.startswith(";TYPE:") and "prime tower" not in stripped.lower():
        return True
    if stripped.startswith("EXCLUDE_OBJECT_START") and "PrimeTower" not in stripped:
        return True
    return False


def find_segments_and_bbox(lines):
    segments = []
    active_segment = False
    prime_context = False
    seg_start = None
    last_move_idx = None
    last_boundary_idx = -1

    minx = float("inf")
    miny = float("inf")
    maxx = float("-inf")
    maxy = float("-inf")

    def update_bbox_from_line(idx):
        nonlocal minx, miny, maxx, maxy
        line = lines[idx]
        if not line.startswith(("G0", "G1", "G00", "G01")):
            return
        x, y = parse_xy(line)
        if x is None or y is None:
            return
        minx = min(minx, x)
        maxx = max(maxx, x)
        miny = min(miny, y)
        maxy = max(maxy, y)

    for idx, line in enumerate(lines):
        stripped = line.lstrip()
        if stripped.startswith(("G0", "G1", "G00", "G01")):
            last_move_idx = idx

        if is_prime_indicator(line):
            prime_context = True

        # Close on clear boundaries
        if active_segment and is_prime_end(line):
            segments.append((seg_start, idx))
            active_segment = False
            seg_start = None
            prime_context = False
            last_boundary_idx = idx
            continue

        # Do not include temp/tool commands inside the exclude object
        if active_segment and stripped.startswith(NON_OBJECT_CMD_PREFIXES):
            segments.append((seg_start, idx))
            active_segment = False
            seg_start = None
            # stay in prime_context so we can reopen after the command
            last_boundary_idx = idx
            continue

        # Start a segment only when in prime context AND not inside toolchange
        if prime_context and not active_segment:
            seg_start = last_move_idx if last_move_idx is not None else idx
            active_segment = True
            if seg_start != idx:
                update_bbox_from_line(seg_start)

        if active_segment:
            update_bbox_from_line(idx)

    if active_segment and seg_start is not None:
        segments.append((seg_start, len(lines)))

    if not segments:
        return [], None

    segments.sort(key=lambda s: s[0])
    merged = []
    cur_start, cur_end = segments[0]
    for s_start, s_end in segments[1:]:
        if s_start <= cur_end:
            cur_end = max(cur_end, s_end)
        else:
            merged.append((cur_start, cur_end))
            cur_start, cur_end = s_start, s_end
    merged.append((cur_start, cur_end))

    if minx == float("inf") or miny == float("inf"):
        bbox = None
    else:
        bbox = (minx, miny, maxx, maxy)

    return merged, bbox


def format_define_line(bbox):
    if not bbox:
        return "EXCLUDE_OBJECT_DEFINE NAME=PrimeTower\n"
    minx, miny, maxx, maxy = bbox
    cx = (minx + maxx) / 2.0
    cy = (miny + maxy) / 2.0
    polygon = [
        (minx, miny),
        (maxx, miny),
        (maxx, maxy),
        (minx, maxy),
    ]
    poly_str = "[{}]".format(
        ",".join("[{:.3f},{:.3f}]".format(x, y) for x, y in polygon)
    )
    return (
        "EXCLUDE_OBJECT_DEFINE NAME=PrimeTower "
        "CENTER={:.3f},{:.3f} POLYGON={}\n".format(cx, cy, poly_str)
    )


def process(lines):
    segments, bbox = find_segments_and_bbox(lines)
    if not segments:
        return lines, []

    # If present, prefer to inject the DEFINE before EXECUTABLE_BLOCK_START
    exe_block_idx = None
    for idx, line in enumerate(lines):
        if line.lstrip().startswith("; EXECUTABLE_BLOCK_START"):
            exe_block_idx = idx
            break

    starts = {s for s, _ in segments}
    ends = {e for _, e in segments}

    define_line = format_define_line(bbox)
    define_emitted = False

    out_lines = []
    for idx, line in enumerate(lines):
        # Emit the EXECUTABLE_BLOCK_START line, then inject DEFINE immediately after
        if exe_block_idx is not None and idx == exe_block_idx:
            out_lines.append(line)
            if not define_emitted:
                out_lines.append(define_line)
                define_emitted = True
            continue

        if idx in starts:
            if not define_emitted:
                out_lines.append(define_line)
                define_emitted = True
            out_lines.append("EXCLUDE_OBJECT_START NAME=PrimeTower\n")
        out_lines.append(line)

        if (idx + 1) in ends:
            out_lines.append("EXCLUDE_OBJECT_END NAME=PrimeTower\n")

    return out_lines, segments


def patch_file(path_str: str) -> None:
    path = Path(path_str)
    data = path.read_text(encoding="utf-8", errors="ignore")

    if "EXCLUDE_OBJECT_DEFINE NAME=PrimeTower" in data:
        print("PrimeTower already defined; skipping.")
        return

    lines = data.splitlines(keepends=True)
    processed, segments = process(lines)

    if not segments:
        print("No prime tower regions detected; no changes made.")
        return

    path.write_text("".join(processed), encoding="utf-8")
    print(f"Inserted PrimeTower exclude markers for {len(segments)} segment(s).")


def main():
    if len(sys.argv) < 2:
        print("Usage: prime_tower_exclude.py <gcode_file>")
        sys.exit(1)
    patch_file(sys.argv[1])


if __name__ == "__main__":
    main()
