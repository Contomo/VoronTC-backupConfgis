#!/usr/bin/env python3

import sys
import pathlib
import re


# --- regexes -------------------------------------------------------

PAT_T    = re.compile(r"^\s*(T(\d+))\s*$", re.IGNORECASE)
PAT_E    = re.compile(r"\bE([-+]?\d*\.?\d+)", re.IGNORECASE)
PAT_AXES = re.compile(r"\b([XYZ])([-+]?\d*\.?\d+)", re.IGNORECASE)


class ModalPosition:
    __slots__ = ("x", "y", "z")

    def __init__(self, x=None, y=None, z=None):
        self.x = x
        self.y = y
        self.z = z

    def copy(self):
        return ModalPosition(self.x, self.y, self.z)

    def update_from_move(self, line: str) -> None:
        """Update modal coordinates from a G0/G1 line."""
        for axis, val in PAT_AXES.findall(line):
            v = float(val)
            if axis in ("X", "x"):
                self.x = v
            elif axis in ("Y", "y"):
                self.y = v
            elif axis in ("Z", "z"):
                self.z = v


class Coord:
    __slots__ = ("x", "y", "z")

    def __init__(self, x=None, y=None, z=None):
        self.x = x
        self.y = y
        self.z = z

    @staticmethod
    def _fmt_value(v: float) -> str:
        s = f"{v:.4f}"
        s = s.rstrip("0").rstrip(".")
        return s

    @classmethod
    def from_anchor(cls, anchor: ModalPosition, pre: ModalPosition):
        if anchor is None or anchor.x is None or anchor.y is None:
            return None
        if anchor.z is not None:
            z_val = anchor.z
        else:
            z_val = pre.z
        return cls(anchor.x, anchor.y, z_val)

    def to_gcode_suffix(self) -> str:
        parts = []
        if self.x is not None:
            parts.append(f"X={self._fmt_value(self.x)}")
        if self.y is not None:
            parts.append(f"Y={self._fmt_value(self.y)}")
        if self.z is not None:
            parts.append(f"Z={self._fmt_value(self.z)}")
        return " ".join(parts)


def is_comment(line: str) -> bool:
    return line.lstrip().startswith(";")

def is_move(line: str) -> bool:
    ls = line.lstrip()
    return ls.startswith("G0") or ls.startswith("G1")

def has_extrusion(line: str) -> bool:
    return bool(PAT_E.search(line))

def has_any_xyz(line: str) -> bool:
    return bool(PAT_AXES.search(line))

def find_anchor(lines, start_index: int, pre_modal: ModalPosition):
    modal = pre_modal.copy()
    travel_indices = []

    for idx in range(start_index, len(lines)):
        ln = lines[idx]

        if is_comment(ln):
            continue

        if PAT_T.match(ln):
            break
        
        if is_move(ln):
            if has_extrusion(ln):
                break
            if has_any_xyz(ln):
                travel_indices.append(idx)
            modal.update_from_move(ln)

    if modal.x is None or modal.y is None:
        return None, []
    return modal, travel_indices

def patch_file(path_str: str) -> None:
    p = pathlib.Path(path_str)
    text = p.read_text(encoding="utf-8", errors="ignore")
    lines = text.splitlines()

    modal = ModalPosition()      # global modal position
    comment_indices = set()      # travel moves we will replace with comments

    out_lines = []
    i = 0

    while i < len(lines):
        line = lines[i]

        if i in comment_indices:
            out_lines.append(f"; {line} ←  removed by RESTORE_POS_FIX")
            i += 1
            continue

        if is_move(line):
            modal.update_from_move(line)

        mT = PAT_T.match(line)
        if mT:
            pre_modal = modal.copy()

            anchor_modal, travel_to_comment = find_anchor(lines, i + 1, pre_modal)

            coord = Coord.from_anchor(anchor_modal, pre_modal)

            if coord is not None:
                comment_indices.update(travel_to_comment)

                suffix = coord.to_gcode_suffix()
                tool_name = mT.group(1)  # e.g. "T3"

                if suffix:
                    out_lines.append(f"{tool_name} {suffix}")
                else:
                    out_lines.append(tool_name)
                modal = ModalPosition(coord.x, coord.y, coord.z)
            else:
                out_lines.append(line)

            i += 1
            continue

        out_lines.append(line)
        i += 1

    p.write_text("\n".join(out_lines) + "\n", encoding="utf-8")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python restore_pos_fix.py input_file")
        sys.exit(1)

    patch_file(sys.argv[1])
