from __future__ import annotations

import argparse
from pathlib import Path


def format_line(line: str) -> str:
    if line.lstrip().startswith("#"):
        return line

    out: list[str] = []
    i = 0
    in_string = False
    in_char = False
    escaped = False

    while i < len(line):
        ch = line[i]
        next_ch = line[i + 1] if i + 1 < len(line) else ""

        if in_string:
            out.append(ch)
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
            i += 1
            continue

        if in_char:
            out.append(ch)
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == "'":
                in_char = False
            i += 1
            continue

        if ch == "/" and next_ch == "/":
            out.append(line[i:])
            break

        if ch == '"':
            in_string = True
            out.append(ch)
            i += 1
            continue

        if ch == "'":
            in_char = True
            out.append(ch)
            i += 1
            continue

        if ch == ":" and next_ch and not next_ch.isspace() and next_ch not in ":=":
            out.append(": ")
            i += 1
            continue

        out.append(ch)
        i += 1

    return "".join(out)


def format_file(path: Path) -> bool:
    text = path.read_text(encoding="utf-8")
    lines = text.splitlines()
    formatted = "\n".join(format_line(line) for line in lines)
    if text.endswith(("\n", "\r\n")):
        formatted += "\n"
    if formatted == text:
        return False
    path.write_text(formatted, encoding="utf-8", newline="\n")
    return True


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("paths", nargs="+", type=Path)
    args = parser.parse_args()

    for root in args.paths:
        if root.is_file():
            paths = [root]
        else:
            paths = sorted(root.rglob("*.i"))
        for path in paths:
            if format_file(path):
                print(path)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
