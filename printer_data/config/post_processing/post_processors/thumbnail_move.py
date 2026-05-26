#!/usr/bin/env python3
import sys
import pathlib

MARK_START = "; THUMBNAIL_BLOCK_START"
MARK_END   = "; THUMBNAIL_BLOCK_END"


def move_thumbnail_block_to_end(path_str: str) -> None:
    p = pathlib.Path(path_str)
    text = p.read_text(encoding="utf-8", errors="ignore")
    lines = text.splitlines(keepends=True)

    start, end = None, None

    for i, line in enumerate(lines):
        if MARK_START in line and start is None:
            start = i
        elif MARK_END in line and start is not None:
            end = i
            break

    if start is None or end is None:
        print("no thumbnail block found, leaving file unchanged")
        return

    block = lines[start:end + 1]
    del lines[start:end + 1]

    if lines and not lines[-1].endswith("\n"):
        lines[-1] += "\n"

    lines.extend(block)

    p.write_text("".join(lines), encoding="utf-8")
    rescan(p)

def rescan(path_obj: pathlib.Path):
    try:
        gc_root = pathlib.Path.home() / "printer_data" / "gcodes"
        rel = path_obj.relative_to(gc_root)
        import urllib.request, urllib.parse
        url = "http://localhost:7125/server/files/metascan"
        qs = urllib.parse.urlencode({"filename": str(rel)})
        req = urllib.request.Request(
            url + "?" + qs,
            method="POST",
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=2) as resp:
            resp.read()
    except:
        return


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: thumbnail_move.py <input_file>")
        sys.exit(1)

    move_thumbnail_block_to_end(sys.argv[1])
