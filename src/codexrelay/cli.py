from __future__ import annotations

import argparse
import asyncio
import signal
from pathlib import Path

from codexrelay.codex.app_server import AppServerBackend
from codexrelay.core import RelayService
from codexrelay.database import Database
from codexrelay.paths import AppPaths
from codexrelay.projects import ProjectService
from codexrelay.runtime import CodexRelayRuntime
from codexrelay.sleep import SleepInhibitor


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="codexrelay")
    parser.add_argument("--data-dir", type=Path, help="override the application data directory")
    subcommands = parser.add_subparsers(dest="command", required=True)
    subcommands.add_parser("init", help="initialize the local database")

    projects = subcommands.add_parser("projects", help="manage authorized projects")
    project_commands = projects.add_subparsers(dest="project_command", required=True)
    project_commands.add_parser("list", help="list registered projects")

    add = project_commands.add_parser("add", help="register a project")
    add.add_argument("path", type=Path)
    add.add_argument("--name")

    discover = project_commands.add_parser("discover", help="find and optionally register projects")
    discover.add_argument("roots", type=Path, nargs="+")
    discover.add_argument("--max-depth", type=int, default=2)
    discover.add_argument("--register", action="store_true")

    switch = project_commands.add_parser("use", help="switch the current project")
    switch.add_argument("selector", help="project name or id")

    run_command = subcommands.add_parser("run", help="run a Codex turn in the current project")
    run_command.add_argument("prompt")
    run_command.add_argument("--image", type=Path, action="append", default=[])
    subcommands.add_parser("serve", help="run the Telegram-to-Codex service")
    return parser


async def run(args: argparse.Namespace) -> int:
    default_paths = AppPaths.default()
    paths = (
        AppPaths(data_dir=args.data_dir.expanduser(), log_dir=default_paths.log_dir)
        if args.data_dir
        else default_paths
    )
    paths.ensure()
    async with Database(paths.database) as database:
        service = ProjectService(database)
        if args.command == "init":
            print(f"initialized {paths.database}")
            return 0
        if args.command == "run":
            backend = AppServerBackend()
            inhibitor = SleepInhibitor()
            await backend.start()
            try:
                result = await RelayService(
                    database=database,
                    backend=backend,
                    sleep_inhibitor=inhibitor,
                ).run_current_project(text=args.prompt, image_paths=tuple(args.image))
            finally:
                await backend.stop()
                await inhibitor.close()
            print(result.final_text)
            return 0
        if args.command == "serve":
            stop = asyncio.Event()
            loop = asyncio.get_running_loop()
            for signum in (signal.SIGINT, signal.SIGTERM):
                loop.add_signal_handler(signum, stop.set)
            runtime = CodexRelayRuntime(paths)
            identity = await runtime.start()
            print(f"Telegram @{identity.bot_username} connected; press Ctrl-C to stop")
            await runtime.run(stop)
            return 0
        if args.project_command == "list":
            projects = await service.list_projects()
            if not projects:
                print("no registered projects")
                return 0
            for project in projects:
                marker = "*" if project.is_current else " "
                print(f"{marker} {project.name}\t{project.id}\t{project.path}")
            return 0
        if args.project_command == "add":
            project = await service.register(args.path, args.name)
            print(f"registered {project.name}: {project.path}")
            return 0
        if args.project_command == "discover":
            found = service.discover(args.roots, max_depth=args.max_depth)
            for path in found:
                if args.register:
                    project = await service.register(path)
                    print(f"registered {project.name}: {project.path}")
                else:
                    print(path)
            return 0
        if args.project_command == "use":
            project = await service.switch(args.selector)
            print(f"current project: {project.name} ({project.path})")
            return 0
    return 2


def main() -> None:
    args = build_parser().parse_args()
    raise SystemExit(asyncio.run(run(args)))
