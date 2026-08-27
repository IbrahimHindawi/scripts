from __future__ import annotations

import argparse
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_RINBIND = ROOT.parent / "rin" / "build" / "rinbind.exe"
DEFAULT_HEADER = ROOT / "extern" / "cgltf" / "cgltf.h"
DEFAULT_OUTPUT = ROOT / "src" / "bindings" / "cgltf.rin"


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate focused rin bindings for cgltf.h.")
    parser.add_argument("--rinbind", type=Path, default=DEFAULT_RINBIND)
    parser.add_argument("--header", type=Path, default=DEFAULT_HEADER)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--print-cmd", action="store_true")
    args = parser.parse_args()

    rinbind = args.rinbind.resolve()
    header = args.header.resolve()
    output = args.output.resolve()

    if not rinbind.exists():
        raise SystemExit(f"missing rinbind executable: {rinbind}")
    if not header.exists():
        raise SystemExit(f"missing cgltf header: {header}")

    output.parent.mkdir(parents=True, exist_ok=True)
    tmp_output = output.with_name(output.name + ".tmp")
    if tmp_output.exists():
        tmp_output.unlink()

    cmd = [
        str(rinbind),
        str(header),
        str(tmp_output),
        "--filter",
        "cgltf.h",
        "--prefix",
        "cgltf_",
    ]
    if args.print_cmd:
        cmd.append("--print-cmd")
    cmd.append("--")

    print("+ " + " ".join(cmd), flush=True)
    subprocess.run(cmd, check=True)
    if output.exists() and output.read_bytes() == tmp_output.read_bytes():
        tmp_output.unlink()
        print(f"unchanged {output}", flush=True)
    else:
        tmp_output.replace(output)
        print(f"generated {output}", flush=True)


if __name__ == "__main__":
    main()
