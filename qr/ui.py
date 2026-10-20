"""Colorama terminal UI helpers (Windows + Linux), with a plain-text fallback.

Colorama is initialised lazily so the installer can run before dependencies
are installed and then switch colours on with :func:`enable_colors`.
"""
from __future__ import annotations

import os
import sys
from getpass import getpass

_ON = False
Fore = Style = None  # type: ignore[assignment]


class _Blank:
    def __getattr__(self, _name):
        return ""


def enable_colors() -> bool:
    """Initialise colorama if available. Safe to call repeatedly."""
    global _ON, Fore, Style
    if _ON:
        return True
    try:
        import colorama
        from colorama import Fore as _Fore, Style as _Style

        # just_fix_windows_console enables ANSI on Windows 10+ without
        # wrapping stdout twice; init(autoreset) keeps older consoles working.
        if hasattr(colorama, "just_fix_windows_console"):
            colorama.just_fix_windows_console()
        colorama.init(autoreset=True)
        Fore, Style = _Fore, _Style
        _ON = not os.environ.get("NO_COLOR")
    except Exception:  # noqa: BLE001 - colorama missing -> plain text
        Fore = Style = _Blank()  # type: ignore[assignment]
        _ON = False
    return _ON


enable_colors()

# Use ASCII fallbacks when the console cannot encode box/check characters.
_ENC = (getattr(sys.stdout, "encoding", None) or "ascii").lower()
_UNICODE = "utf" in _ENC
OK_MARK = "\u2713" if _UNICODE else "+"
ERR_MARK = "\u2717" if _UNICODE else "x"


def _w(code: str, text: str) -> str:
    return f"{code}{text}{Style.RESET_ALL}" if _ON else text


def bold(s: str) -> str:
    return _w(Style.BRIGHT, s)


def dim(s: str) -> str:
    return _w(Style.DIM, s)


def cyan(s: str) -> str:
    return _w(Fore.CYAN, s)


def green(s: str) -> str:
    return _w(Fore.GREEN, s)


def yellow(s: str) -> str:
    return _w(Fore.YELLOW, s)


def red(s: str) -> str:
    return _w(Fore.RED, s)


def magenta(s: str) -> str:
    return _w(Fore.MAGENTA, s)


def box(title: str, subtitle: str = "", width: int = 44) -> None:
    """Draw the QuickReply framed header."""
    if _UNICODE:
        tl, tr, bl, br, h, v = "\u2554", "\u2557", "\u255a", "\u255d", "\u2550", "\u2551"
    else:
        tl = tr = bl = br = "+"
        h, v = "=", "|"
    inner = width - 2
    print(cyan(tl + h * inner + tr))
    print(cyan(v) + bold(title.center(inner)) + cyan(v))
    if subtitle:
        print(cyan(v) + subtitle.center(inner) + cyan(v))
    print(cyan(bl + h * inner + br))


BANNER = r"""
  ___        _      _    ____            _
 / _ \ _   _(_) ___| | _|  _ \ ___ _ __ | |_   _
| | | | | | | |/ __| |/ / |_) / _ \ '_ \| | | | |
| |_| | |_| | | (__|   <|  _ <  __/ |_) | | |_| |
 \__\_\\__,_|_|\___|_|\_\_| \_\___| .__/|_|\__, |
                                  |_|      |___/
"""


def banner(subtitle: str = "") -> None:
    print(cyan(BANNER))
    if subtitle:
        print(bold("  " + subtitle))
        print()


def heading(text: str) -> None:
    print()
    print(magenta(bold("\u25b6 " + text if _UNICODE else "> " + text)))
    print(dim("-" * max(20, len(text) + 2)))


def rule(text: str) -> None:
    heading(text)


def info(text: str) -> None:
    print(cyan("[i] ") + text, flush=True)


def success(text: str) -> None:
    print(green(f"[{OK_MARK}] ") + text, flush=True)


def warn(text: str) -> None:
    print(yellow("[!] ") + text, flush=True)


def error(text: str) -> None:
    print(red(f"[{ERR_MARK}] ") + text, flush=True)


def step(index: int, total: int, text: str) -> None:
    print()
    print(bold(cyan(f"Step {index}/{total} ")) + bold(text))


def menu(options: list[str]) -> None:
    for number, label in enumerate(options, 1):
        print(f"  {cyan(f'[{number}]')} {label}")
    print()


def ask(prompt: str, default: str | None = None, secret: bool = False,
        validator=None, allow_empty: bool = False) -> str:
    """Prompt until a valid value is entered. Secrets are never echoed."""
    label = cyan("> ") + prompt
    if default and not secret:
        label += dim(f" [{default}]")
    label += ": "
    while True:
        raw = getpass(label) if secret else input(label)
        value = raw.strip()
        if not value and default is not None:
            value = default
        if not value and not allow_empty:
            warn("A value is required.")
            continue
        if validator is not None and value:
            problem = validator(value)
            if problem:
                error(problem)
                continue
        return value


def confirm(prompt: str, default: bool = False) -> bool:
    hint = "Y/n" if default else "y/N"
    while True:
        raw = input(cyan("? ") + f"{prompt} [{hint}]: ").strip().lower()
        if not raw:
            return default
        if raw in ("y", "yes"):
            return True
        if raw in ("n", "no"):
            return False
        warn("Please answer y or n.")


def choose(prompt: str, count: int) -> int:
    """Return a 1-based menu choice."""
    while True:
        raw = input(cyan("> ") + prompt + ": ").strip()
        if raw.isdigit() and 1 <= int(raw) <= count:
            return int(raw)
        warn(f"Enter a number between 1 and {count}.")


def status_line(label: str, ok: bool | None, detail: str = "") -> None:
    """Completion-report row: ok=True green, False red, None yellow."""
    text = f"{label}" + (f" - {detail}" if detail else "")
    if ok is True:
        success(text)
    elif ok is False:
        error(text)
    else:
        warn(text)
