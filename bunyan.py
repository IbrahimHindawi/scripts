from __future__ import annotations

import argparse
import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable


Hook = Callable[["BuildContext"], None]
I_IMPORT_RE = re.compile(r'^\s*import\s+"([^"]+)"', re.MULTILINE)


def resolve_i_tool(tool_name: str) -> Path | None:
    rin_home = os.environ.get("RIN_HOME")
    if rin_home:
        candidate = Path(rin_home) / tool_name
        if candidate.exists():
            return candidate

    found = shutil.which(tool_name)
    return Path(found) if found else None


def require_rin_tool(tool_name: str) -> Path:
    found = resolve_i_tool(tool_name)
    if found:
        return found
    raise SystemExit(
        f"{tool_name} not found. Set RIN_HOME to the rin package directory "
        f"or put that directory on PATH."
    )


class BuildArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        self.print_help(sys.stderr)
        self.exit(2, f"\nerror: {message}\n")


@dataclass(frozen=True)
class BuildConfig:
    name: str
    cmake_build_type: str
    build_dir: Path


@dataclass(frozen=True)
class Project:
    name: str
    mode: str = "c"
    exe_name: str | None = None
    rin_entry: Path | None = None
    rin_compiler: Path | None = None
    rin_compiler_build_command: tuple[os.PathLike[str] | str, ...] = ()
    rin_compiler_build_cwd: Path | None = None
    rin_gen_dir: Path = Path("rin_gen")
    rin_import_dirs: tuple[Path, ...] = ()
    rin_emit_header: bool = True
    root_dir: Path = field(default_factory=lambda: Path.cwd())
    generator: str = "Ninja"
    c_compiler: str = "clang-cl"
    cxx_compiler: str = "clang-cl"
    debugger: tuple[str, ...] = ("devenv", "/debugexe")
    hooks: dict[str, Hook] = field(default_factory=dict)
    extra_clean_paths: tuple[Path, ...] = ()


@dataclass(frozen=True)
class BuildContext:
    project: Project
    config: BuildConfig
    extra_args: tuple[str, ...] = ()

    @property
    def root_dir(self) -> Path:
        return self.project.root_dir

    @property
    def build_dir(self) -> Path:
        return self.root_dir / self.config.build_dir

    @property
    def exe_name(self) -> str:
        return self.project.exe_name or f"{self.project.name}.exe"

    @property
    def exe_path(self) -> Path:
        return self.build_dir / self.exe_name

    @property
    def generator(self) -> str:
        return self.project.generator

    @property
    def c_compiler(self) -> str:
        return self.project.c_compiler

    @property
    def cxx_compiler(self) -> str:
        return self.project.cxx_compiler

    @property
    def is_i_project(self) -> bool:
        return self.project.mode == "rin"

    @property
    def rin_entry_path(self) -> Path:
        if not self.project.rin_entry:
            raise RuntimeError("rin project mode requires rin_entry.")
        if self.project.rin_entry.is_absolute():
            return self.project.rin_entry
        return self.root_dir / self.project.rin_entry

    @property
    def rin_compiler_path(self) -> Path:
        if self.project.rin_compiler is None:
            return require_rin_tool("rin.exe")
        compiler = self.project.rin_compiler
        if compiler.is_absolute():
            return compiler
        return self.root_dir / compiler

    @property
    def rin_generated_dir(self) -> Path:
        if self.project.rin_gen_dir.is_absolute():
            return self.project.rin_gen_dir
        return self.build_dir / self.project.rin_gen_dir

    @property
    def rin_generated_c_path(self) -> Path:
        return self.rin_generated_dir / f"{self.rin_entry_path.stem}.c"

    @property
    def rin_generated_h_path(self) -> Path:
        return self.rin_generated_dir / f"{self.rin_entry_path.stem}.h"

    @property
    def rin_compiler_dir(self) -> Path:
        return self.rin_compiler_path.parent

    @property
    def rin_std_dir(self) -> Path:
        return self.rin_compiler_dir / "std"

    @property
    def rin_import_dir_paths(self) -> tuple[Path, ...]:
        result: list[Path] = []
        for path in self.project.rin_import_dirs:
            result.append(path if path.is_absolute() else self.root_dir / path)
        return tuple(result)

    @property
    def cmake_defines(self) -> dict[str, object]:
        defines: dict[str, object] = {}
        if self.is_i_project:
            defines.update(
                {
                    "BUNYAN_RIN_ENTRY": self.rin_entry_path,
                    "BUNYAN_RIN_GEN_DIR": self.rin_generated_dir,
                    "BUNYAN_RIN_GENERATED_C": self.rin_generated_c_path,
                    "BUNYAN_RIN_COMPILER_DIR": self.rin_compiler_dir,
                    "BUNYAN_RIN_STD_DIR": self.rin_std_dir,
                }
            )
            if self.project.rin_emit_header:
                defines["BUNYAN_RIN_GENERATED_H"] = self.rin_generated_h_path
        return defines


def default_configs() -> dict[str, BuildConfig]:
    return {
        "debug": BuildConfig("debug", "Debug", Path("build")),
        "release": BuildConfig("release", "Release", Path("build-release")),
        "reldebug": BuildConfig("reldebug", "RelWithDebInfo", Path("build-reldebug")),
    }


def run_cmd(args: Iterable[os.PathLike[str] | str], *, cwd: Path | str | None = None) -> None:
    cmd = [str(arg) for arg in args]
    print("+ " + " ".join(cmd), flush=True)
    subprocess.run(cmd, cwd=str(cwd) if cwd else None, check=True)


def cmake_configure(
    *,
    source_dir: Path | str,
    build_dir: Path | str,
    generator: str,
    c_compiler: str,
    build_type: str,
    cxx_compiler: str | None = None,
    defines: dict[str, object] | None = None,
    cwd: Path | str | None = None,
) -> None:
    args: list[os.PathLike[str] | str] = [
        "cmake",
        "-S",
        source_dir,
        "-B",
        build_dir,
        "-G",
        generator,
        f"-DCMAKE_C_COMPILER={c_compiler}",
        f"-DCMAKE_BUILD_TYPE={build_type}",
        "-DCMAKE_EXPORT_COMPILE_COMMANDS=ON",
    ]
    if cxx_compiler:
        args.append(f"-DCMAKE_CXX_COMPILER={cxx_compiler}")
    for key, value in (defines or {}).items():
        if isinstance(value, bool):
            value = "ON" if value else "OFF"
        args.append(f"-D{key}={value}")
    run_cmd(args, cwd=cwd)


def cmake_build(build_dir: Path | str, *, cwd: Path | str | None = None) -> None:
    run_cmd(["cmake", "--build", build_dir], cwd=cwd)


def call_hook(ctx: BuildContext, name: str) -> None:
    hook = ctx.project.hooks.get(name)
    if hook:
        hook(ctx)


def resolve_i_import(ctx: BuildContext, owner: Path, raw_import: str) -> Path | None:
    raw_path = Path(raw_import)
    if raw_path.is_absolute():
        return raw_path if raw_path.exists() else None

    search_dirs = [
        owner.parent,
        *ctx.rin_import_dir_paths,
        ctx.root_dir,
        ctx.rin_compiler_dir,
        ctx.rin_std_dir,
    ]
    for base in search_dirs:
        candidate = base / raw_path
        if candidate.exists():
            return candidate
    return None


def read_i_imports(path: Path) -> list[str] | None:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return None
    return I_IMPORT_RE.findall(text)


def collect_i_dependencies(ctx: BuildContext) -> list[Path] | None:
    deps: list[Path] = []
    seen: set[Path] = set()
    stack = [ctx.rin_entry_path]

    while stack:
        path = stack.pop().resolve()
        if path in seen:
            continue
        seen.add(path)
        deps.append(path)

        imports = read_i_imports(path)
        if imports is None:
            return None
        for raw_import in imports:
            imported = resolve_i_import(ctx, path, raw_import)
            if imported is None:
                return None
            stack.append(imported)

    return deps


def rin_translation_outputs(ctx: BuildContext) -> list[Path]:
    outputs = [ctx.rin_generated_c_path]
    if ctx.project.rin_emit_header:
        outputs.append(ctx.rin_generated_h_path)
    return outputs


def rin_translation_is_up_to_date(ctx: BuildContext) -> bool:
    outputs = rin_translation_outputs(ctx)
    if any(not path.exists() for path in outputs):
        return False

    oldest_output_mtime = min(path.stat().st_mtime_ns for path in outputs)
    deps = collect_i_dependencies(ctx)
    if deps is None:
        return False

    for dep in deps:
        if not dep.exists() or dep.stat().st_mtime_ns > oldest_output_mtime:
            return False

    compiler = ctx.rin_compiler_path
    if compiler.exists() and compiler.stat().st_mtime_ns > oldest_output_mtime:
        return False

    return True


def command_i_translate(ctx: BuildContext) -> None:
    if not ctx.is_i_project:
        return

    compiler = ctx.rin_compiler_path
    if not compiler.exists() and ctx.project.rin_compiler_build_command:
        run_cmd(
            ctx.project.rin_compiler_build_command,
            cwd=ctx.project.rin_compiler_build_cwd or ctx.root_dir,
        )
    if not compiler.exists():
        raise SystemExit(
            f"rin compiler not found: {compiler}. Set RIN_HOME to the rin package "
            f"directory or put that directory on PATH."
        )

    ctx.rin_generated_dir.mkdir(parents=True, exist_ok=True)
    if rin_translation_is_up_to_date(ctx):
        print(f"+ rin compile skipped (up to date): {ctx.rin_generated_c_path}", flush=True)
        return

    args: list[os.PathLike[str] | str] = [
        compiler,
        "compile",
        ctx.rin_entry_path,
        "-o",
        ctx.rin_generated_c_path,
    ]
    if ctx.project.rin_emit_header:
        args.extend(["--header", ctx.rin_generated_h_path])
    else:
        args.append("--no-header")
    for import_dir in ctx.rin_import_dir_paths:
        args.extend(["--importdir", import_dir])

    run_cmd(args, cwd=ctx.root_dir)


def command_config(ctx: BuildContext) -> None:
    call_hook(ctx, "pre_config")
    ctx.build_dir.mkdir(parents=True, exist_ok=True)
    command_i_translate(ctx)
    cmake_configure(
        source_dir=ctx.root_dir,
        build_dir=ctx.build_dir,
        generator=ctx.generator,
        c_compiler=ctx.c_compiler,
        cxx_compiler=ctx.cxx_compiler,
        build_type=ctx.config.cmake_build_type,
        defines=ctx.cmake_defines,
    )
    call_hook(ctx, "post_config")


def command_build(ctx: BuildContext) -> None:
    command_config(ctx)
    call_hook(ctx, "pre_build")
    cmake_build(ctx.build_dir)
    call_hook(ctx, "post_build")


def command_run(ctx: BuildContext) -> None:
    command_build(ctx)
    call_hook(ctx, "pre_run")
    run_cmd([ctx.exe_path, *ctx.extra_args])
    call_hook(ctx, "post_run")


def command_debugexe(ctx: BuildContext) -> None:
    command_build(ctx)
    call_hook(ctx, "pre_debugexe")
    run_cmd([*ctx.project.debugger, ctx.exe_path, *ctx.extra_args])
    call_hook(ctx, "post_debugexe")


def command_clean(ctx: BuildContext) -> None:
    paths = [ctx.build_dir, *(ctx.root_dir / p for p in ctx.project.extra_clean_paths)]
    for path in paths:
        if path.exists():
            print(f"+ remove {path}", flush=True)
            shutil.rmtree(path)


def _normalize_extra_args(args: list[str]) -> tuple[str, ...]:
    if args and args[0] == "--":
        args = args[1:]
    return tuple(args)


def main(
    *,
    project_name: str,
    mode: str = "c",
    exe_name: str | None = None,
    rin_entry: Path | str | None = None,
    rin_compiler: Path | str | None = None,
    rin_compiler_build_command: Iterable[os.PathLike[str] | str] = (),
    rin_compiler_build_cwd: Path | str | None = None,
    rin_gen_dir: Path | str = "rin_gen",
    rin_import_dirs: Iterable[Path | str] = (),
    rin_emit_header: bool = True,
    root_dir: Path | None = None,
    generator: str = "Ninja",
    c_compiler: str = "clang-cl",
    cxx_compiler: str = "clang-cl",
    debugger: Iterable[str] = ("devenv", "/debugexe"),
    hooks: dict[str, Hook] | None = None,
    extra_clean_paths: Iterable[str | Path] = (),
) -> None:
    parser = BuildArgumentParser()
    parser.add_argument("command", choices=("config", "build", "run", "debugexe", "clean"))
    parser.add_argument("config", nargs="?", default="debug")
    parser.add_argument("extra", nargs=argparse.REMAINDER)
    args = parser.parse_args()

    configs = default_configs()
    if args.config not in configs:
        raise SystemExit(f"unknown config: {args.config}")
    if mode not in ("c", "rin"):
        raise SystemExit(f"unknown project mode: {mode}")
    if mode == "rin" and not rin_entry:
        raise SystemExit("mode='rin' requires rin_entry.")

    project = Project(
        name=project_name,
        mode=mode,
        exe_name=exe_name,
        rin_entry=Path(rin_entry) if rin_entry else None,
        rin_compiler=Path(rin_compiler) if rin_compiler else None,
        rin_compiler_build_command=tuple(rin_compiler_build_command),
        rin_compiler_build_cwd=Path(rin_compiler_build_cwd) if rin_compiler_build_cwd else None,
        rin_gen_dir=Path(rin_gen_dir),
        rin_import_dirs=tuple(Path(path) for path in rin_import_dirs),
        rin_emit_header=rin_emit_header,
        root_dir=root_dir or Path.cwd(),
        generator=generator,
        c_compiler=c_compiler,
        cxx_compiler=cxx_compiler,
        debugger=tuple(debugger),
        hooks=hooks or {},
        extra_clean_paths=tuple(Path(p) for p in extra_clean_paths),
    )
    ctx = BuildContext(project=project, config=configs[args.config], extra_args=_normalize_extra_args(args.extra))

    if args.command == "config":
        command_config(ctx)
    elif args.command == "build":
        command_build(ctx)
    elif args.command == "run":
        command_run(ctx)
    elif args.command == "debugexe":
        command_debugexe(ctx)
    elif args.command == "clean":
        command_clean(ctx)
