"""``python -m clipdesk`` — the single command that starts everything.

Subcommands are kept minimal on purpose. The web UI is the product; the CLI
exists to start it, to provision dependencies without a browser, and to check
what is wrong when something will not start.
"""

from __future__ import annotations

import argparse
import sys
import threading
import webbrowser

from clipdesk import __version__
from clipdesk.bootstrap import ProvisionError, component_statuses, provision_all
from clipdesk.config import load_settings
from clipdesk.llm import all_statuses
from clipdesk.media.ffmpeg import find_tools


def _use_utf8_console() -> None:
    """Windows consoles still default to a legacy code page, which mangles the
    ellipses and box characters in progress output."""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass


def _print_status(settings) -> bool:
    ok = True
    print(f"ClipDesk {__version__}")
    print(f"  workspace : {settings.paths.workspace_dir}")
    print(f"  vendor    : {settings.paths.vendor_dir}")
    print()

    tools = find_tools(settings.paths.vendor_dir)
    if tools:
        print(f"  [ok]   ffmpeg   ({tools.source}) {tools.ffmpeg}")
    else:
        print("  [FAIL] ffmpeg   not installed — run: clipdesk bootstrap")
        ok = False

    for component in component_statuses(
        settings.paths.vendor_dir, settings.transcription.model
    ):
        mark = "ok  " if component.installed else "----"
        print(f"  [{mark}] {component.label}: {component.detail}")

    print()
    for status in all_statuses(settings.llm):
        active = " (active)" if status.key == settings.llm.provider else ""
        mark = "ok  " if status.available else "----"
        print(f"  [{mark}] {status.label}{active}: {status.detail}")
        if not status.available and status.setup_hint and status.key == settings.llm.provider:
            print(f"         {status.setup_hint}")
    return ok


def _serve(args) -> int:
    import uvicorn

    settings = load_settings(args.config)
    host = args.host or settings.server.host
    port = args.port or settings.server.port
    # The app builds its CORS and WebSocket allow-list from the configured port,
    # so an overridden one has to be written back or the UI is serving from an
    # origin the server does not recognise.
    settings.server.host = host
    settings.server.port = port

    if args.bootstrap:
        if not _bootstrap(settings, include_whisper=not args.no_whisper):
            return 1

    url = f"http://{'127.0.0.1' if host in {'0.0.0.0', '::'} else host}:{port}"
    print(f"\n  ClipDesk is running at {url}\n  Press Ctrl+C to stop.\n")
    if not args.no_browser:
        threading.Timer(1.2, lambda: webbrowser.open(url)).start()

    from clipdesk.server import create_app

    app = create_app(settings)
    config = uvicorn.Config(
        app,
        host=host,
        port=port,
        log_level=settings.logging.level.lower(),
        access_log=False,
    )
    server = uvicorn.Server(config)
    app.state.clipdesk.shutdown_callback = lambda: setattr(server, "should_exit", True)
    server.run()
    return 0


def _bootstrap(settings, *, include_whisper: bool) -> bool:
    last = {"message": ""}

    def report(fraction: float | None, message: str) -> None:
        if message == last["message"] and fraction is not None:
            bar = int((fraction or 0) * 30)
            sys.stdout.write(f"\r  [{'#' * bar}{'.' * (30 - bar)}] {message[:60]:<60}")
            sys.stdout.flush()
            return
        last["message"] = message
        sys.stdout.write(f"\n  {message}")
        sys.stdout.flush()

    print("Provisioning dependencies into", settings.paths.vendor_dir)
    try:
        provision_all(
            settings.paths.vendor_dir,
            settings.transcription.model,
            report,
            include_whisper=include_whisper,
        )
    except ProvisionError as exc:
        print(f"\n\n  Setup failed:\n  {exc}\n")
        return False
    print("\n\n  Everything is installed.\n")
    return True


def main(argv: list[str] | None = None) -> int:
    _use_utf8_console()
    parser = argparse.ArgumentParser(prog="clipdesk", description="ClipDesk video editor")
    parser.add_argument("--config", help="extra YAML config file to layer on top")
    subparsers = parser.add_subparsers(dest="command")

    serve = subparsers.add_parser("serve", help="start the web app (default)")
    serve.add_argument("--host")
    serve.add_argument("--port", type=int)
    serve.add_argument("--no-browser", action="store_true", help="do not open a browser")
    serve.add_argument(
        "--bootstrap", action="store_true", help="install missing dependencies first"
    )
    serve.add_argument(
        "--no-whisper", action="store_true", help="skip the speech-to-text model"
    )

    boot = subparsers.add_parser("bootstrap", help="download ffmpeg and the model")
    boot.add_argument("--no-whisper", action="store_true")

    subparsers.add_parser("doctor", help="report what is installed and what is missing")

    args = parser.parse_args(argv)
    command = args.command or "serve"

    if command == "doctor":
        return 0 if _print_status(load_settings(args.config)) else 1
    if command == "bootstrap":
        settings = load_settings(args.config)
        return 0 if _bootstrap(settings, include_whisper=not args.no_whisper) else 1

    if not hasattr(args, "host"):  # bare `clipdesk` with no subcommand
        args = parser.parse_args([*(argv or []), "serve"])
    return _serve(args)


if __name__ == "__main__":
    raise SystemExit(main())
