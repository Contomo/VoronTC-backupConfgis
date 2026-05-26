import sys
import pathlib
import re

MOVEMENT_CMD = re.compile(r'^[GM]\d+[^;]*\b[XY][-+]?\d', re.I)

def has_movement_commands(lines: list[str]) -> bool:
    """Checks a list of strings for any G-code movement commands."""
    for line in lines:
        if MOVEMENT_CMD.search(line):
            return True
    return False

def process_gcode(lines: list[str]) -> bool:
    """
    Analyzes G-code lines. If the specific Prime Tower sequence exists 
    at the end, comment those lines out.
    
    Returns True if changes were made, False otherwise.
    """
    total_lines = len(lines)

    exclude_end_idx = -1
    for i in range(total_lines - 1, -1, -1):
        if lines[i].startswith("EXCLUDE_OBJECT_END"):
            exclude_end_idx = i
            break
    
    if exclude_end_idx == -1:
        return False

    prime_start_idx = -1
    for i in range(exclude_end_idx + 1, total_lines):
        if ";TYPE:Prime tower" in lines[i]:
            prime_start_idx = i
            break
    
    if prime_start_idx == -1:
        return False

    toolchange_end_idx = -1
    for i in range(prime_start_idx, total_lines):
        if lines[i].startswith("; CP TOOLCHANGE END"):
            toolchange_end_idx = i
            break

    if toolchange_end_idx == -1:
        return False

    remaining_lines = lines[toolchange_end_idx + 1:]
    if has_movement_commands(remaining_lines):
        return False

    start_mod = exclude_end_idx + 1
    end_mod = toolchange_end_idx

    print(f"Commenting out Prime Tower sequence between line {start_mod} and {end_mod}")

    for i in range(start_mod, end_mod + 1):
        original_line = lines[i]
        # Avoid double commenting if ran twice
        if "removed by PRIME_TOWER_PRUNE" not in original_line:
            lines[i] = f"; {original_line} <- removed by PRIME_TOWER_PRUNE"

    return True

def patch_file(path_str: str) -> None:
    path = pathlib.Path(path_str)
    
    if not path.exists():
        print(f"Error: File {path_str} not found.")
        sys.exit(1)

    try:
        text = path.read_text(encoding="utf-8", errors="ignore")
    except Exception as e:
        print(f"Error reading file: {e}")
        sys.exit(1)

    lines = text.splitlines()
    
    was_modified = process_gcode(lines)

    if was_modified:
        new_text = "\n".join(lines) + "\n"
        path.write_text(new_text, encoding="utf-8")
        print("File patched successfully.")
    else:
        print("No changes required.")

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python prime_tower_tail_prune.py input_file")
        sys.exit(1)

    patch_file(sys.argv[1])