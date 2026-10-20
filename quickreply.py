#!/usr/bin/env python3
"""QuickReply - start the userbot.

    python quickreply.py                 # default session (sessions/quickreply.session)
    python quickreply.py --session work  # another account (sessions/work.session)

First run? Use  python setup.py  to configure MONGO_URL / API_ID / API_HASH and
log in. Press Ctrl+C to stop.
"""
from __future__ import annotations

import argparse
import asyncio
import sys

from qr import ui
from qr.config.settings import DEFAULT_SESSION
from qr.config.validation import validate_session_name


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description="Run the QuickReply userbot.")
    parser.add_argument("--session", default=DEFAULT_SESSION,
                        help="session name under sessions/ (default: %(default)s)")
    args = parser.parse_args(argv)
    problem = validate_session_name(args.session)
    if problem:
        parser.error(problem)
    return args


def main(argv=None) -> int:
    args = parse_args(argv)
    try:
        from qr.runtime import run
    except ImportError as error:
        ui.error(f"Missing dependency ({error.name}). Run:  python setup.py")
        return 1
    try:
        asyncio.run(run(args.session))
    except KeyboardInterrupt:
        print()
        ui.info("Stopped.")
        return 130
    except EOFError:
        print()
        ui.error("Input closed while QuickReply was waiting for the Telegram login. "
                 "Run it in an interactive terminal (or log in with python setup.py).")
        return 1
    except SystemExit as exit_:
        if exit_.code not in (0, None) and isinstance(exit_.code, str):
            ui.error(exit_.code)
            return 1
        return int(exit_.code or 0)
    except Exception as error:  # noqa: BLE001
        ui.error(f"QuickReply stopped unexpectedly: {type(error).__name__}: {error}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
