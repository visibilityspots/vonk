"""Command-line entry point (``vonk``).

Subcommands (serve, poll, dry-run, purge-own, clear-attention) register themselves on the
parser returned by ``build_parser()``; each sets ``func`` via ``set_defaults(func=...)``.
"""

from __future__ import annotations

import argparse
import logging
import os
from collections.abc import Sequence

from vonk import __version__, poll
from vonk.config import Settings


def _logging() -> None:
    if not logging.getLogger().handlers:
        logging.basicConfig(level=logging.INFO, format="VONK %(levelname)s %(name)s: %(message)s")


def cmd_serve(args: argparse.Namespace) -> int:
    from vonk.main import serve

    _logging()
    return serve(Settings.from_env(), args.host, args.port)


def cmd_purge_own(args: argparse.Namespace) -> int:
    """Delete every own (vonk-) event in ``--calendar``; foreign events are never touched."""
    from vonk.sinks.caldav_sink import CalDAVSink
    from vonk.store import Store

    _logging()
    settings = Settings.from_env()
    missing = [
        n for n in ("BAIKAL_URL", "BAIKAL_USER", "BAIKAL_PASSWORD") if n in
        settings.missing_secrets("serve")
    ]  # fmt: skip
    if missing:
        print(f"purge-own: missing settings: {', '.join(missing)}")
        return 2
    sink = CalDAVSink.from_settings(settings, calendar=args.calendar)
    results = sink.purge_own()
    store = Store(settings.vonk_db_path) if os.path.exists(settings.vonk_db_path) else None
    failed = 0
    for r in results:
        if r.ok and r.uid and store is not None:
            store.delete_identity(r.uid)
        failed += not r.ok
        print(f"{'deleted' if r.ok else 'FAILED '} {r.uid} {r.error or ''}".rstrip())
    print(f"purge-own {sink.url}: {len(results) - failed} deleted, {failed} failed")
    return 1 if failed else 0


def cmd_dry_run(args: argparse.Namespace) -> int:
    """Print the ChangeSet each stored scope would get now; writes nothing anywhere."""
    from vonk.api import Service
    from vonk.main import default_sink
    from vonk.metrics import Metrics
    from vonk.notify import NullNotifier
    from vonk.store import Store

    _logging()
    settings = Settings.from_env()
    sink = default_sink(settings)
    if sink is None or not os.path.exists(settings.vonk_db_path):
        print("dry-run: needs the Baikal settings and an existing VONK_DB_PATH")
        return 2
    service = Service(settings, Store(settings.vonk_db_path), sink, NullNotifier(), Metrics())
    scopes = args.scope or [s for s in service.store.desired_scopes() if service.follows(s)]
    for scope_key in scopes:
        cs = service.build(scope_key)
        print(f"{scope_key}: {len(cs.ops)} ops")
        for op in cs.ops:
            ev = op.after or op.before
            assert ev is not None
            flags = f" [{', '.join(op.flags)}]" if op.flags else ""
            print(f"  {op.kind} {ev.date} {ev.start or ''} {ev.title_activity}{flags}")
        for w in cs.warnings:
            print(f"  ! {w}")
    return 0


def cmd_clear_attention(args: argparse.Namespace) -> int:
    """Clear ``needs attention`` for a scope and forget its seen posts, so the next poll
    posts it again and a fresh ChangeSet is announced."""
    from vonk.store import Store

    settings = Settings.from_env()
    store = Store(settings.vonk_db_path)
    was_set = store.clear_attention(args.scope)
    forgotten = store.forget_seen_scope(args.scope)
    print(
        f"{args.scope}: {'cleared' if was_set else 'was not flagged'}; "
        f"{forgotten} post(s) will be re-posted by the next poll"
    )
    return 0


def build_parser() -> tuple[argparse.ArgumentParser, argparse._SubParsersAction]:
    """Return the top-level parser and its subparsers action."""
    parser = argparse.ArgumentParser(prog="vonk", description=__doc__.splitlines()[0])
    parser.add_argument("--version", action="version", version=f"vonk {__version__}")
    subparsers = parser.add_subparsers(dest="command", metavar="COMMAND")
    poll.register(subparsers)

    p = subparsers.add_parser("serve", help="run the approve page, internal API and metrics")
    p.add_argument("--host", default="0.0.0.0")
    p.add_argument("--port", type=int, default=8080)
    p.set_defaults(func=cmd_serve)

    p = subparsers.add_parser("purge-own", help="delete all own (vonk-) events of a calendar")
    p.add_argument("--calendar", required=True, help="calendar name, e.g. vonk-test")
    p.set_defaults(func=cmd_purge_own)

    p = subparsers.add_parser("dry-run", help="show the current diff per scope; no writes")
    p.add_argument("--scope", action="append", help="limit to this scope (repeatable)")
    p.set_defaults(func=cmd_dry_run)

    p = subparsers.add_parser("clear-attention", help="re-enable announcements for a scope")
    p.add_argument("scope", help="scope key, e.g. kabouters:2026-10")
    p.set_defaults(func=cmd_clear_attention)
    return parser, subparsers


def main(argv: Sequence[str] | None = None) -> int:
    """Parse ``argv`` and run the selected subcommand; returns the process exit code."""
    parser, _ = build_parser()
    args = parser.parse_args(argv)
    func = getattr(args, "func", None)
    if func is None:
        parser.print_help()
        return 2
    return int(func(args) or 0)
