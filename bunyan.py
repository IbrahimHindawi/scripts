from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable


Hook = Callable[["BuildContext"], None]


class BuildArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        self.print_help(sys.stderr)
        self.exit(2, f"\nerror: {message}\n")


ROOT_BUILD_TEMPLATE = """from __future__ import annotations

from scripts.bunyan import BuildContext, cmake_build, cmake_configure, main, run_cmd


def build_haikal(ctx: BuildContext) -> None:
    haikal_build_dir = ctx.root_dir / "extern" / "haikal" / "build"
    haikal_build_dir.mkdir(parents=True, exist_ok=True)

    cmake_configure(
        source_dir=ctx.root_dir / "extern" / "haikal",
        build_dir=haikal_build_dir,
        generator=ctx.generator,
        c_compiler=ctx.c_compiler,
        build_type="Debug",
    )
    cmake_build(haikal_build_dir)
    run_cmd(
        [
            haikal_build_dir / "haikal.exe",
            "--entry",
            ctx.root_dir / "src" / "main.c",
            "--meta",
            ctx.root_dir / "extern" / "haikal" / "src" / "meta_arena",
        ],
        cwd=ctx.root_dir,
    )


if __name__ == "__main__":
    main(
        project_name="{project_name}",
        hooks={{
            "pre_build": build_haikal,
        }},
        extra_clean_paths=(
            "extern/haikal/build",
        ),
    )
"""


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
    test_target: str | None = None
    test_exe_name: str | None = None
    i_entry: Path | None = None
    i_compiler: Path | None = None
    i_compiler_build_command: tuple[os.PathLike[str] | str, ...] = ()
    i_compiler_build_cwd: Path | None = None
    i_gen_dir: Path = Path("i_gen")
    i_import_dirs: tuple[Path, ...] = ()
    root_dir: Path = field(default_factory=lambda: Path.cwd())
    generator: str = "Ninja"
    c_compiler: str = "clang-cl"
    debugger: tuple[str, ...] = ("devenv", "/debugexe")
    configs: dict[str, BuildConfig] = field(default_factory=dict)
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
    def test_exe_name(self) -> str:
        if self.project.test_exe_name:
            return self.project.test_exe_name
        if self.project.test_target:
            return f"{self.project.test_target}.exe"
        raise RuntimeError("No test executable configured.")

    @property
    def test_exe_path(self) -> Path:
        return self.build_dir / self.test_exe_name

    @property
    def generator(self) -> str:
        return self.project.generator

    @property
    def c_compiler(self) -> str:
        return self.project.c_compiler

    @property
    def is_i_project(self) -> bool:
        return self.project.mode == "i"

    @property
    def i_entry_path(self) -> Path:
        if not self.project.i_entry:
            raise RuntimeError("I project mode requires i_entry.")
        if self.project.i_entry.is_absolute():
            return self.project.i_entry
        return self.root_dir / self.project.i_entry

    @property
    def i_compiler_path(self) -> Path:
        compiler = self.project.i_compiler or Path("I.exe")
        if compiler.is_absolute():
            return compiler
        if compiler.parent == Path("."):
            return compiler
        return self.root_dir / compiler

    @property
    def i_compiler_resolved_path(self) -> Path | None:
        compiler = self.i_compiler_path
        if compiler.is_absolute() or compiler.parent != Path("."):
            return compiler if compiler.exists() else None
        found = shutil.which(str(compiler))
        return Path(found) if found else None

    @property
    def i_compiler_dir(self) -> Path | None:
        resolved = self.i_compiler_resolved_path
        return resolved.parent if resolved else None

    @property
    def i_std_dir(self) -> Path | None:
        compiler_dir = self.i_compiler_dir
        return compiler_dir / "std" if compiler_dir else None

    @property
    def i_generated_dir(self) -> Path:
        if self.project.i_gen_dir.is_absolute():
            return self.project.i_gen_dir
        return self.build_dir / self.project.i_gen_dir

    @property
    def i_generated_c_path(self) -> Path:
        return self.i_generated_dir / f"{self.i_entry_path.stem}.c"

    @property
    def i_generated_h_path(self) -> Path:
        return self.i_generated_dir / f"{self.i_entry_path.stem}.h"

    @property
    def i_import_dir_paths(self) -> tuple[Path, ...]:
        paths: list[Path] = []
        for path in self.project.i_import_dirs:
            paths.append(path if path.is_absolute() else self.root_dir / path)
        return tuple(paths)

    @property
    def cmake_defines(self) -> dict[str, object]:
        defines: dict[str, object] = {}
        if self.is_i_project:
            defines.update(
                {
                    "BUNYAN_I_ENTRY": self.i_entry_path,
                    "BUNYAN_I_GEN_DIR": self.i_generated_dir,
                    "BUNYAN_I_GENERATED_C": self.i_generated_c_path,
                    "BUNYAN_I_GENERATED_H": self.i_generated_h_path,
                }
            )
            compiler_dir = self.i_compiler_dir
            std_dir = self.i_std_dir
            if compiler_dir:
                defines["BUNYAN_I_COMPILER_DIR"] = compiler_dir
            if std_dir:
                defines["BUNYAN_I_STD_DIR"] = std_dir
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
    for key, value in (defines or {}).items():
        if isinstance(value, bool):
            value = "ON" if value else "OFF"
        args.append(f"-D{key}={value}")
    run_cmd(args, cwd=cwd)


def cmake_build(build_dir: Path | str, *, target: str | None = None, cwd: Path | str | None = None) -> None:
    args: list[os.PathLike[str] | str] = ["cmake", "--build", build_dir]
    if target:
        args.extend(["--target", target])
    run_cmd(args, cwd=cwd)


def call_hook(ctx: BuildContext, name: str) -> None:
    hook = ctx.project.hooks.get(name)
    if hook:
        hook(ctx)


def command_i_translate(ctx: BuildContext) -> None:
    if not ctx.is_i_project:
        return

    compiler = ctx.i_compiler_path
    compiler_is_file = compiler.is_absolute() or compiler.parent != Path(".")
    if compiler_is_file and not compiler.exists() and ctx.project.i_compiler_build_command:
        run_cmd(
            ctx.project.i_compiler_build_command,
            cwd=ctx.project.i_compiler_build_cwd or ctx.root_dir,
        )
    if compiler_is_file and not compiler.exists():
        raise SystemExit(f"I compiler not found: {compiler}")
    if not compiler_is_file and not shutil.which(str(compiler)):
        raise SystemExit(f"I compiler not found on PATH: {compiler}")

    ctx.i_generated_dir.mkdir(parents=True, exist_ok=True)
    args: list[os.PathLike[str] | str] = [
        compiler,
        "compile",
        ctx.i_entry_path,
        "-o",
        ctx.i_generated_c_path,
        "--header",
        ctx.i_generated_h_path,
    ]
    for path in ctx.i_import_dir_paths:
        args.extend(["--importdir", path])
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


def command_test(ctx: BuildContext) -> None:
    if not ctx.project.test_target:
        raise SystemExit("No test target configured.")

    command_config(ctx)
    call_hook(ctx, "pre_build")
    cmake_build(ctx.build_dir, target=ctx.project.test_target)
    call_hook(ctx, "post_build")
    call_hook(ctx, "pre_test")
    run_cmd([ctx.test_exe_path, *ctx.extra_args])
    call_hook(ctx, "post_test")


def _safe_rmtree(root: Path, target: Path) -> None:
    resolved_root = root.resolve()
    resolved_target = target.resolve()
    if resolved_target == resolved_root or resolved_root not in resolved_target.parents:
        raise RuntimeError(f"Refusing to remove path outside project root: {resolved_target}")
    if resolved_target.exists():
        print(f"remove {resolved_target}")
        shutil.rmtree(resolved_target)


def command_clean(project: Project) -> None:
    root = project.root_dir
    for config in project.configs.values():
        _safe_rmtree(root, root / config.build_dir)
    for path in project.extra_clean_paths:
        _safe_rmtree(root, root / path)


def _normalize_extra_args(args: list[str]) -> tuple[str, ...]:
    if args and args[0] == "--":
        args = args[1:]
    return tuple(args)


def _write_init_file(path: Path, content: str, *, force: bool) -> bool:
    if path.exists() and not force:
        print(f"skip existing {path}")
        return False

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8", newline="\n")
    print(f"write {path}")
    return True


def command_init(root_dir: Path, *, project_name: str | None = None, force: bool = False) -> None:
    root = root_dir.resolve()
    name = project_name or root.name

    files = {
        root / "bunyan.py": ROOT_BUILD_TEMPLATE.format(project_name=name),
    }

    for path, content in files.items():
        _write_init_file(path, content, force=force)

    print()
    print("next:")
    print("  git submodule add https://github.com/IbrahimHindawi/haikal extern/haikal")
    print("  git submodule update --init --recursive")
    print("  python bunyan.py build debug")


def main(
    *,
    project_name: str,
    mode: str = "c",
    exe_name: str | None = None,
    test_target: str | None = None,
    test_exe_name: str | None = None,
    i_entry: Path | str | None = None,
    i_compiler: Path | str | None = None,
    i_compiler_build_command: Iterable[os.PathLike[str] | str] = (),
    i_compiler_build_cwd: Path | str | None = None,
    i_gen_dir: Path | str = "i_gen",
    i_import_dirs: Iterable[Path | str] = (),
    root_dir: Path | str | None = None,
    generator: str = "Ninja",
    c_compiler: str = "clang-cl",
    debugger: Iterable[str] = ("devenv", "/debugexe"),
    configs: dict[str, BuildConfig] | None = None,
    hooks: dict[str, Hook] | None = None,
    extra_clean_paths: Iterable[Path | str] = (),
) -> None:
    root = Path(root_dir) if root_dir else Path.cwd()
    if mode not in ("c", "i"):
        raise SystemExit(f"unknown project mode: {mode}")
    if mode == "i" and not i_entry:
        raise SystemExit("mode='i' requires i_entry.")

    project = Project(
        name=project_name,
        mode=mode,
        exe_name=exe_name,
        test_target=test_target,
        test_exe_name=test_exe_name,
        i_entry=Path(i_entry) if i_entry else None,
        i_compiler=Path(i_compiler) if i_compiler else None,
        i_compiler_build_command=tuple(i_compiler_build_command),
        i_compiler_build_cwd=Path(i_compiler_build_cwd) if i_compiler_build_cwd else None,
        i_gen_dir=Path(i_gen_dir),
        i_import_dirs=tuple(Path(path) for path in i_import_dirs),
        root_dir=root,
        generator=generator,
        c_compiler=c_compiler,
        debugger=tuple(debugger),
        configs=configs or default_configs(),
        hooks=hooks or {},
        extra_clean_paths=tuple(Path(path) for path in extra_clean_paths),
    )

    command_help = {
        "config": "configure the selected CMake build directory",
        "build": "configure, run build hooks, and build the project",
        "run": "build the project and run the executable",
        "debugexe": "build the project and launch it under the configured debugger",
        "test": "build and run the configured test target",
        "clean": "remove configured build directories",
    }
    config_names = ", ".join(sorted(project.configs.keys()))

    parser = BuildArgumentParser(
        prog="bunyan.py",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description=f"Build helper for {project.name}.",
        epilog=(
            "commands:\n"
            + "\n".join(f"  {name:<8} {description}" for name, description in command_help.items())
            + "\n\n"
            f"configs: {config_names}\n\n"
            "examples:\n"
            "  python bunyan.py build debug\n"
            "  python bunyan.py run debug -- arg0 arg1\n"
            "  python bunyan.py test debug\n"
            "  python bunyan.py clean"
        ),
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    for command_name in ("config", "build", "run", "debugexe", "test"):
        command_parser = subparsers.add_parser(command_name, help=command_help[command_name])
        command_parser.add_argument("config", choices=sorted(project.configs.keys()))
        command_parser.add_argument("extra_args", nargs=argparse.REMAINDER)

    subparsers.add_parser("clean", help=command_help["clean"])

    args = parser.parse_args()

    try:
        if args.command == "clean":
            command_clean(project)
            return

        config = project.configs[args.config]
        ctx = BuildContext(project, config, _normalize_extra_args(args.extra_args))

        commands = {
            "config": command_config,
            "build": command_build,
            "run": command_run,
            "debugexe": command_debugexe,
            "test": command_test,
        }
        commands[args.command](ctx)
    except subprocess.CalledProcessError as exc:
        raise SystemExit(exc.returncode) from exc


if __name__ == "__main__":
    init_parser = argparse.ArgumentParser(prog="scripts/bunyan.py")
    init_parser.add_argument("--init", action="store_true", help="create a default project bunyan.py/haikal setup")
    init_parser.add_argument("--project-name", help="project name to write into generated files")
    init_parser.add_argument("--force", action="store_true", help="overwrite files that already exist")
    init_args = init_parser.parse_args()

    if init_args.init:
        command_init(Path.cwd(), project_name=init_args.project_name, force=init_args.force)
    else:
        print("scripts/bunyan.py is a shared library. Use the project root bunyan.py, or run scripts/bunyan.py --init.")
        raise SystemExit(2)
