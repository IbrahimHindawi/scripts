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


ROOT_BUILD_TEMPLATE = """from __future__ import annotations

from scripts.build import BuildContext, cmake_build, cmake_configure, main, run_cmd


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
    run_cmd([haikal_build_dir / "haikal.exe"], cwd=ctx.root_dir)


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


HAIKAL_TOML_TEMPLATE = """[core]
mainpath = "src/main.c"
metapath = "extern/haikal/src/meta_arena/"
"""


@dataclass(frozen=True)
class BuildConfig:
    name: str
    cmake_build_type: str
    build_dir: Path


@dataclass(frozen=True)
class Project:
    name: str
    exe_name: str | None = None
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
    def generator(self) -> str:
        return self.project.generator

    @property
    def c_compiler(self) -> str:
        return self.project.c_compiler


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
    cwd: Path | str | None = None,
) -> None:
    run_cmd(
        [
            "cmake",
            "-S",
            source_dir,
            "-B",
            build_dir,
            "-G",
            generator,
            f"-DCMAKE_C_COMPILER={c_compiler}",
            f"-DCMAKE_BUILD_TYPE={build_type}",
        ],
        cwd=cwd,
    )


def cmake_build(build_dir: Path | str, *, cwd: Path | str | None = None) -> None:
    run_cmd(["cmake", "--build", build_dir], cwd=cwd)


def call_hook(ctx: BuildContext, name: str) -> None:
    hook = ctx.project.hooks.get(name)
    if hook:
        hook(ctx)


def command_config(ctx: BuildContext) -> None:
    call_hook(ctx, "pre_config")
    ctx.build_dir.mkdir(parents=True, exist_ok=True)
    cmake_configure(
        source_dir=ctx.root_dir,
        build_dir=ctx.build_dir,
        generator=ctx.generator,
        c_compiler=ctx.c_compiler,
        build_type=ctx.config.cmake_build_type,
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
        root / "build.py": ROOT_BUILD_TEMPLATE.format(project_name=name),
        root / "haikal.toml": HAIKAL_TOML_TEMPLATE,
    }

    for path, content in files.items():
        _write_init_file(path, content, force=force)

    print()
    print("next:")
    print("  git submodule add https://github.com/IbrahimHindawi/haikal extern/haikal")
    print("  git submodule update --init --recursive")
    print("  python build.py build debug")


def main(
    *,
    project_name: str,
    exe_name: str | None = None,
    root_dir: Path | str | None = None,
    generator: str = "Ninja",
    c_compiler: str = "clang-cl",
    debugger: Iterable[str] = ("devenv", "/debugexe"),
    configs: dict[str, BuildConfig] | None = None,
    hooks: dict[str, Hook] | None = None,
    extra_clean_paths: Iterable[Path | str] = (),
) -> None:
    root = Path(root_dir) if root_dir else Path.cwd()
    project = Project(
        name=project_name,
        exe_name=exe_name,
        root_dir=root,
        generator=generator,
        c_compiler=c_compiler,
        debugger=tuple(debugger),
        configs=configs or default_configs(),
        hooks=hooks or {},
        extra_clean_paths=tuple(Path(path) for path in extra_clean_paths),
    )

    parser = argparse.ArgumentParser(prog="build.py")
    subparsers = parser.add_subparsers(dest="command", required=True)

    for command_name in ("config", "build", "run", "debugexe"):
        command_parser = subparsers.add_parser(command_name)
        command_parser.add_argument("config", choices=sorted(project.configs.keys()))
        command_parser.add_argument("extra_args", nargs=argparse.REMAINDER)

    subparsers.add_parser("clean")

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
        }
        commands[args.command](ctx)
    except subprocess.CalledProcessError as exc:
        raise SystemExit(exc.returncode) from exc


if __name__ == "__main__":
    init_parser = argparse.ArgumentParser(prog="scripts/build.py")
    init_parser.add_argument("--init", action="store_true", help="create a default project build.py/haikal setup")
    init_parser.add_argument("--project-name", help="project name to write into generated files")
    init_parser.add_argument("--force", action="store_true", help="overwrite files that already exist")
    init_args = init_parser.parse_args()

    if init_args.init:
        command_init(Path.cwd(), project_name=init_args.project_name, force=init_args.force)
    else:
        print("scripts/build.py is a shared library. Use the project root build.py, or run scripts/build.py --init.")
        raise SystemExit(2)
