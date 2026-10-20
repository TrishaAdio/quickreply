#!/usr/bin/env python3
"""QuickReply interactive installer / control panel.

    python setup.py            # menu (install, checks, run)
    python setup.py install    # run the guided installation directly

Safe to run repeatedly: existing .env values and Telegram sessions are kept
unless you explicitly confirm a change. Secrets are never printed.
"""
from __future__ import annotations

import asyncio
import sys

from qr import ui


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] in ("bdist_wheel", "egg_info", "sdist", "develop", "build"):
        print("setup.py is QuickReply's interactive installer, not a packaging script.")
        return 2
    from qr import installer

    try:
        if argv and argv[0] == "install":
            return asyncio.run(installer.run_install())
        return installer.run_menu()
    except KeyboardInterrupt:
        print()
        ui.warn("Cancelled - nothing else was changed. Re-run setup.py any time.")
        return 130
    except EOFError:
        print()
        ui.warn("Input closed; setup stopped.")
        return 1
    except Exception as error:  # noqa: BLE001
        ui.error(f"Setup failed unexpectedly: {type(error).__name__}: {error}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
