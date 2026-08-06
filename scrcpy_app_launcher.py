#!/usr/bin/env python3
"""Search and launch Android apps in scrcpy virtual displays.

Runtime dependencies:
  - Python 3 with curses (normally included on Linux)
  - adb
  - scrcpy with --list-apps support

Main controls:
  Search: type to filter; Down/Enter moves to the app list.
  List:   Enter launches, f focuses search, e edits the command,
          r renames, h hides/shows, Ctrl+H toggles hidden entries,
          p pins/unpins, R reloads apps, q/Esc quits.
"""

from __future__ import annotations

import argparse
import curses
import json
import locale
import os
import re
import shlex
import shutil
import subprocess
import sys
import time
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Optional

APP_ID = "adb-scrcpy-app-launcher"
VERSION = "0.1.0"
CONFIG_VERSION = 1

DEFAULT_SCRCPY_ARGS = (
    "--new-display=960x640/160 "
    "--flex-display "
    "--start-app={package} "
    "--no-vd-system-decorations "
    "--window-title={title} "
    "--window-borderless "
    "--max-fps=120 "
    "--video-codec=h265 "
    "--video-bit-rate=30M"
)

ANSI_RE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
PACKAGE_RE = re.compile(r"[A-Za-z0-9_]+(?:\.[A-Za-z0-9_]+)+")
LOG_PREFIX_RE = re.compile(
    r"^\s*(?:\[[^\]]+\]\s*)?(?:INFO|DEBUG|WARN|WARNING|ERROR|FATAL):\s*"
)


class LauncherError(RuntimeError):
    """A user-facing launcher error."""


@dataclass(frozen=True)
class DeviceApp:
    name: str
    package: str


@dataclass
class UiState:
    query: str = ""
    query_cursor: int = 0
    focus: str = "search"  # search | list
    selected: int = 0
    top: int = 0
    show_hidden: bool = False
    status: str = ""
    status_error: bool = False


@dataclass(frozen=True)
class Paths:
    config: Path
    log: Path


def xdg_paths() -> Paths:
    config_root = Path(
        os.environ.get("XDG_CONFIG_HOME", str(Path.home() / ".config"))
    )
    cache_root = Path(os.environ.get("XDG_CACHE_HOME", str(Path.home() / ".cache")))
    return Paths(
        config=config_root / APP_ID / "config.json",
        log=cache_root / APP_ID / "launch.log",
    )


def run_checked(
    argv: list[str], *, timeout: float = 60.0, combine_stderr: bool = False
) -> str:
    try:
        completed = subprocess.run(
            argv,
            check=False,
            text=True,
            encoding="utf-8",
            errors="replace",
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT if combine_stderr else subprocess.PIPE,
            timeout=timeout,
        )
    except FileNotFoundError as exc:
        raise LauncherError(f"Executable not found: {argv[0]}") from exc
    except subprocess.TimeoutExpired as exc:
        raise LauncherError(f"Command timed out: {shlex.join(argv)}") from exc

    output = completed.stdout or ""
    if not combine_stderr and completed.stderr:
        output += completed.stderr
    if completed.returncode != 0:
        tail = "\n".join(output.strip().splitlines()[-12:])
        detail = f"\n{tail}" if tail else ""
        raise LauncherError(
            f"Command exited with status {completed.returncode}: "
            f"{shlex.join(argv)}{detail}"
        )
    return output


def resolve_device_serial(adb: str, requested: Optional[str]) -> str:
    output = run_checked([adb, "devices"], timeout=15)
    devices: list[str] = []
    unavailable: list[str] = []

    for raw_line in output.splitlines()[1:]:
        line = raw_line.strip()
        if not line or line.startswith("*"):
            continue
        fields = line.split()
        if len(fields) < 2:
            continue
        serial, state = fields[0], fields[1]
        if state == "device":
            devices.append(serial)
        else:
            unavailable.append(f"{serial} ({state})")

    if requested:
        if requested not in devices:
            extra = f" Unavailable states: {', '.join(unavailable)}." if unavailable else ""
            raise LauncherError(
                f"Device {requested!r} is not connected and authorized.{extra}"
            )
        return requested

    env_serial = os.environ.get("ANDROID_SERIAL")
    if env_serial:
        if env_serial not in devices:
            raise LauncherError(
                f"ANDROID_SERIAL={env_serial!r}, but that device is not available."
            )
        return env_serial

    if not devices:
        extra = f" Detected: {', '.join(unavailable)}." if unavailable else ""
        raise LauncherError(f"No authorized adb device is connected.{extra}")
    if len(devices) > 1:
        joined = ", ".join(devices)
        raise LauncherError(
            f"Multiple adb devices are connected: {joined}. Run with --serial SERIAL."
        )
    return devices[0]


def strip_scrcpy_prefix(line: str) -> str:
    line = ANSI_RE.sub("", line).rstrip("\r\n")
    return LOG_PREFIX_RE.sub("", line)


def parse_scrcpy_apps(output: str) -> list[DeviceApp]:
    """Parse the stable output format of ``scrcpy --list-apps``.

    scrcpy marks system apps with ``*`` and non-system apps with ``-``.
    Long app names may occupy one line and leave the package on the next.
    """

    apps: list[DeviceApp] = []
    pending: Optional[tuple[str, str]] = None
    in_app_list = False

    for raw_line in output.splitlines():
        line = strip_scrcpy_prefix(raw_line)
        stripped = line.strip()

        if "List of apps:" in stripped:
            in_app_list = True
            pending = None
            continue
        if not in_app_list:
            continue

        marker_match = re.match(r"^\s*([*-])\s+(.*)$", line)
        if marker_match:
            marker, body = marker_match.groups()
            body = body.rstrip()

            # scrcpy reserves 30 columns for the app name and starts the
            # package at column 31. Names of 30+ characters put the package
            # on the following line.
            package = body[31:].strip() if len(body) > 31 else ""
            if package and PACKAGE_RE.fullmatch(package):
                name = body[:30].rstrip()
                if marker == "-" and name:
                    apps.append(DeviceApp(name=name, package=package))
                pending = None
            else:
                pending = (marker, body.strip())
            continue

        if pending and PACKAGE_RE.fullmatch(stripped):
            marker, name = pending
            if marker == "-" and name:
                apps.append(DeviceApp(name=name, package=stripped))
            pending = None
            continue

        # Once the list starts, unrelated log output cancels a pending entry.
        if stripped and not line.startswith(" "):
            pending = None

    # Remove duplicates while preserving the first label found.
    unique: dict[str, DeviceApp] = {}
    for app in apps:
        unique.setdefault(app.package, app)
    return list(unique.values())


def prettify_package(package: str) -> str:
    leaf = package.rsplit(".", 1)[-1]
    leaf = re.sub(r"[_-]+", " ", leaf).strip()
    return leaf.title() if leaf else package


def load_user_apps(scrcpy: str, adb: str, serial: str) -> tuple[list[DeviceApp], str]:
    warning = ""
    try:
        output = run_checked(
            [scrcpy, "--serial", serial, "--list-apps"],
            timeout=90,
            combine_stderr=True,
        )
        apps = parse_scrcpy_apps(output)
        if apps:
            return apps, warning
        warning = (
            "Could not parse labels from `scrcpy --list-apps`; "
            "showing names derived from package IDs."
        )
    except LauncherError as exc:
        warning = (
            "`scrcpy --list-apps` failed; showing names derived from package IDs. "
            f"Details: {first_line(str(exc))}"
        )

    # Fallback: sigue ofreciendo todas las apps de terceros, aunque sin label real.
    output = run_checked(
        [adb, "-s", serial, "shell", "pm", "list", "packages", "-3"],
        timeout=30,
    )
    packages = sorted(
        {
            line.removeprefix("package:").strip()
            for line in output.splitlines()
            if line.strip().startswith("package:")
        }
    )
    apps = [DeviceApp(name=prettify_package(pkg), package=pkg) for pkg in packages]
    if not apps:
        raise LauncherError("The device did not return any user-installed apps.")
    return apps, warning


def first_line(text: str) -> str:
    return text.strip().splitlines()[0] if text.strip() else "unknown error"


def empty_config() -> dict[str, Any]:
    return {"version": CONFIG_VERSION, "devices": {}}


def load_config(path: Path) -> tuple[dict[str, Any], str]:
    if not path.exists():
        return empty_config(), ""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return empty_config(), f"Could not read configuration: {first_line(str(exc))}"

    if not isinstance(data, dict):
        return empty_config(), "The configuration is not a JSON object and was ignored."
    data.setdefault("version", CONFIG_VERSION)
    devices = data.setdefault("devices", {})
    if not isinstance(devices, dict):
        data["devices"] = {}
        return data, "The devices section was invalid and has been reset."
    return data, ""


def device_prefs(config: dict[str, Any], serial: str) -> dict[str, Any]:
    devices = config.setdefault("devices", {})
    device = devices.setdefault(serial, {})
    if not isinstance(device, dict):
        device = {}
        devices[serial] = device
    apps = device.setdefault("apps", {})
    if not isinstance(apps, dict):
        device["apps"] = {}
    return device


def app_prefs(config: dict[str, Any], serial: str, package: str) -> dict[str, Any]:
    device = device_prefs(config, serial)
    apps = device.setdefault("apps", {})
    prefs = apps.setdefault(package, {})
    if not isinstance(prefs, dict):
        prefs = {}
        apps[package] = prefs
    return prefs


def save_config(config: dict[str, Any], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    payload = json.dumps(config, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    tmp.write_text(payload, encoding="utf-8")
    os.replace(tmp, path)


def normalize(text: str) -> str:
    decomposed = unicodedata.normalize("NFKD", text.casefold())
    return "".join(ch for ch in decomposed if not unicodedata.combining(ch))


def display_name(app: DeviceApp, prefs: dict[str, Any]) -> str:
    custom = prefs.get("name")
    return custom if isinstance(custom, str) and custom else app.name


def is_hidden(prefs: dict[str, Any]) -> bool:
    return prefs.get("hidden") is True


def is_pinned(prefs: dict[str, Any]) -> bool:
    return prefs.get("pinned") is True


def default_command(
    package: str, title: str, serial_arg: Optional[str]
) -> str:
    serial_part = f" --serial={shlex.quote(serial_arg)}" if serial_arg else ""
    return (
        f"scrcpy{serial_part} "
        + DEFAULT_SCRCPY_ARGS.format(
            package=shlex.quote(package),
            title=shlex.quote(title),
        )
    )


def command_for(
    app: DeviceApp, prefs: dict[str, Any], serial_arg: Optional[str]
) -> str:
    custom = prefs.get("command")
    if isinstance(custom, str) and custom.strip():
        return custom
    return default_command(app.package, display_name(app, prefs), serial_arg)


def command_with_window_title(command: str, title: str) -> str:
    """Force the current display name into a direct scrcpy command.

    This also replaces a stale ``--window-title`` stored in a custom command
    before an app was renamed.
    """
    try:
        argv = shlex.split(command)
    except ValueError as exc:
        raise LauncherError(f"Could not parse command: {exc}") from exc

    if not argv:
        raise LauncherError("The command is empty.")

    scrcpy_index: Optional[int] = None
    for index, token in enumerate(argv):
        if Path(token).name == "scrcpy":
            scrcpy_index = index
            break

    # Do not inject scrcpy-specific arguments if the user changed the command
    # to launch another program.
    if scrcpy_index is None:
        return command

    found = False
    index = scrcpy_index + 1
    while index < len(argv):
        token = argv[index]
        if token == "--window-title":
            found = True
            if index + 1 < len(argv):
                argv[index + 1] = title
                index += 2
            else:
                argv.append(title)
                index += 2
            continue
        if token.startswith("--window-title="):
            found = True
            argv[index] = f"--window-title={title}"
        index += 1

    if not found:
        argv.insert(scrcpy_index + 1, f"--window-title={title}")

    return shlex.join(argv)


def filtered_apps(
    apps: Iterable[DeviceApp],
    config: dict[str, Any],
    serial: str,
    query: str,
    show_hidden: bool,
) -> list[DeviceApp]:
    needle = normalize(query.strip())
    result: list[DeviceApp] = []

    for app in apps:
        prefs = app_prefs(config, serial, app.package)
        if is_hidden(prefs) and not show_hidden:
            continue
        name = display_name(app, prefs)
        if needle and needle not in normalize(name) and needle not in normalize(app.package):
            continue
        result.append(app)

    result.sort(
        key=lambda app: (
            not is_pinned(app_prefs(config, serial, app.package)),
            normalize(display_name(app, app_prefs(config, serial, app.package))),
            app.package.casefold(),
        )
    )
    return result


def safe_addstr(
    window: curses.window,
    y: int,
    x: int,
    text: str,
    attr: int = 0,
    max_chars: Optional[int] = None,
) -> None:
    try:
        if max_chars is None:
            window.addstr(y, x, text, attr)
        elif max_chars > 0:
            window.addnstr(y, x, text, max_chars, attr)
    except curses.error:
        pass


def clipped(text: str, width: int) -> str:
    if width <= 0:
        return ""
    if len(text) <= width:
        return text
    if width == 1:
        return "…"
    return text[: width - 1] + "…"


def search_view(query: str, cursor: int, width: int) -> tuple[str, int]:
    """Return visible text and the cursor position inside it."""
    if width <= 0:
        return "", 0
    cursor = max(0, min(cursor, len(query)))
    start = max(0, cursor - width + 1)
    if len(query) - start < width:
        start = max(0, len(query) - width)
    visible = query[start : start + width]
    return visible, cursor - start


def keep_selection(
    state: UiState,
    old_package: Optional[str],
    visible: list[DeviceApp],
) -> None:
    if not visible:
        state.selected = 0
        state.top = 0
        return
    if old_package:
        for index, app in enumerate(visible):
            if app.package == old_package:
                state.selected = index
                break
        else:
            state.selected = min(state.selected, len(visible) - 1)
    else:
        state.selected = min(state.selected, len(visible) - 1)
    state.selected = max(0, state.selected)
    state.top = max(0, min(state.top, state.selected))


def selected_package(visible: list[DeviceApp], state: UiState) -> Optional[str]:
    if not visible:
        return None
    index = max(0, min(state.selected, len(visible) - 1))
    return visible[index].package


def draw(
    stdscr: curses.window,
    apps: list[DeviceApp],
    config: dict[str, Any],
    serial: str,
    state: UiState,
) -> list[DeviceApp]:
    stdscr.erase()
    height, width = stdscr.getmaxyx()
    width = max(width, 1)
    visible = filtered_apps(apps, config, serial, state.query, state.show_hidden)
    keep_selection(state, selected_package(visible, state), visible)

    search_prefix = "Search: "
    search_attr = curses.A_BOLD
    if state.focus == "search":
        search_attr |= curses.A_REVERSE
    safe_addstr(stdscr, 0, 0, " " * max(0, width - 1), search_attr)
    safe_addstr(stdscr, 0, 0, search_prefix, search_attr, width - 1)
    query_width = max(0, width - len(search_prefix) - 1)
    q_view, q_cursor = search_view(state.query, state.query_cursor, query_width)
    safe_addstr(stdscr, 0, len(search_prefix), q_view, search_attr, query_width)

    if height > 1:
        separator = "─" * max(0, width - 1)
        safe_addstr(stdscr, 1, 0, separator, curses.A_DIM, width - 1)

    list_start = 2
    footer_lines = 2
    list_height = max(0, height - list_start - footer_lines)

    if visible:
        state.selected = max(0, min(state.selected, len(visible) - 1))
        if state.selected < state.top:
            state.top = state.selected
        elif list_height and state.selected >= state.top + list_height:
            state.top = state.selected - list_height + 1
        max_top = max(0, len(visible) - max(1, list_height))
        state.top = max(0, min(state.top, max_top))
    else:
        state.selected = 0
        state.top = 0

    for row in range(list_height):
        index = state.top + row
        y = list_start + row
        if index >= len(visible):
            break

        app = visible[index]
        prefs = app_prefs(config, serial, app.package)
        name = display_name(app, prefs)
        pinned = is_pinned(prefs)
        hidden = is_hidden(prefs)

        marker = "★" if pinned else " "
        hidden_badge = "  [hidden]" if hidden else ""
        left = f"{marker} {name}{hidden_badge}"

        if width >= 52:
            package_max = min(42, max(18, width // 3))
            package = clipped(app.package, package_max)
            gap = 2
            left_width = max(1, width - len(package) - gap - 1)
            line = f"{clipped(left, left_width):<{left_width}}{' ' * gap}{package}"
        else:
            line = clipped(left, width - 1)

        attr = 0
        if hidden:
            attr |= curses.A_DIM
        if pinned:
            attr |= curses.A_BOLD
        if state.focus == "list" and index == state.selected:
            attr |= curses.A_REVERSE

        safe_addstr(stdscr, y, 0, " " * max(0, width - 1), attr)
        safe_addstr(stdscr, y, 0, line, attr, width - 1)

    if not visible and list_height > 0:
        message = "No matching applications."
        safe_addstr(stdscr, list_start, 0, clipped(message, width - 1), curses.A_DIM)

    if height >= 2:
        status_y = height - 2
        hidden_mode = "hidden shown" if state.show_hidden else "hidden excluded"
        count = f"{len(visible)}/{len(apps)} apps · {hidden_mode} · {serial}"
        status = state.status or count
        attr = curses.A_BOLD if state.status_error else curses.A_DIM
        safe_addstr(stdscr, status_y, 0, " " * max(0, width - 1), attr)
        safe_addstr(stdscr, status_y, 0, clipped(status, width - 1), attr, width - 1)

    if height >= 1:
        help_y = height - 1
        if state.focus == "search":
            help_text = (
                "Type · ↑/↓/Enter list · Backspace delete · Ctrl+H hidden · Esc list"
            )
        else:
            help_text = (
                "Enter launch · f search · e command · r rename · h hide · "
                "Ctrl+H hidden · p pin · R reload · q quit"
            )
        safe_addstr(stdscr, help_y, 0, " " * max(0, width - 1), curses.A_REVERSE)
        safe_addstr(stdscr, help_y, 0, clipped(help_text, width - 1), curses.A_REVERSE)

    try:
        if state.focus == "search":
            curses.curs_set(1)
            cursor_x = min(width - 1, len(search_prefix) + q_cursor)
            stdscr.move(0, max(0, cursor_x))
        else:
            curses.curs_set(0)
    except curses.error:
        pass

    stdscr.refresh()
    return visible


def line_editor(
    stdscr: curses.window,
    title: str,
    initial: str,
    *,
    empty_hint: Optional[str] = None,
) -> Optional[str]:
    text = initial
    cursor = len(text)

    while True:
        stdscr.erase()
        height, width = stdscr.getmaxyx()
        width = max(width, 1)
        safe_addstr(stdscr, 0, 0, clipped(title, width - 1), curses.A_BOLD)

        input_y = 2 if height >= 4 else 1
        field_width = max(1, width - 1)
        visible, cursor_in_view = search_view(text, cursor, field_width)
        safe_addstr(stdscr, input_y, 0, " " * max(0, width - 1), curses.A_REVERSE)
        safe_addstr(stdscr, input_y, 0, visible, curses.A_REVERSE, field_width)

        help_parts = ["Enter save", "Esc discard", "Ctrl+U clear"]
        if empty_hint:
            help_parts.append(empty_hint)
        help_text = " · ".join(help_parts)
        if height >= 2:
            safe_addstr(
                stdscr,
                height - 1,
                0,
                " " * max(0, width - 1),
                curses.A_REVERSE,
            )
            safe_addstr(
                stdscr,
                height - 1,
                0,
                clipped(help_text, width - 1),
                curses.A_REVERSE,
            )

        try:
            curses.curs_set(1)
            stdscr.move(input_y, min(width - 1, cursor_in_view))
        except curses.error:
            pass
        stdscr.refresh()

        key = stdscr.get_wch()
        if key in ("\n", "\r") or key == curses.KEY_ENTER:
            return text
        if key == "\x1b":
            return None
        if key in (curses.KEY_LEFT,):
            cursor = max(0, cursor - 1)
        elif key in (curses.KEY_RIGHT,):
            cursor = min(len(text), cursor + 1)
        elif key in (curses.KEY_HOME, "\x01"):  # Home / Ctrl+A
            cursor = 0
        elif key in (curses.KEY_END, "\x05"):  # End / Ctrl+E
            cursor = len(text)
        elif key in (curses.KEY_BACKSPACE, "\x7f"):
            if cursor > 0:
                text = text[: cursor - 1] + text[cursor:]
                cursor -= 1
        elif key == curses.KEY_DC:
            if cursor < len(text):
                text = text[:cursor] + text[cursor + 1 :]
        elif key == "\x15":  # Ctrl+U
            text = ""
            cursor = 0
        elif isinstance(key, str) and key.isprintable():
            text = text[:cursor] + key + text[cursor:]
            cursor += len(key)


def launch_command(command: str, log_path: Path) -> None:
    try:
        argv = shlex.split(command)
    except ValueError as exc:
        raise LauncherError(f"Could not parse command: {exc}") from exc
    if not argv:
        raise LauncherError("The command is empty.")

    executable = argv[0]
    if "/" not in executable and shutil.which(executable) is None:
        raise LauncherError(f"Executable {executable!r} was not found in PATH.")

    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("ab", buffering=0) as log_file:
        stamp = time.strftime("\n\n=== %Y-%m-%d %H:%M:%S ===\n").encode()
        log_file.write(stamp)
        log_file.write(("$ " + shlex.join(argv) + "\n").encode("utf-8", "replace"))
        try:
            subprocess.Popen(
                argv,
                stdin=subprocess.DEVNULL,
                stdout=log_file,
                stderr=subprocess.STDOUT,
                start_new_session=True,
                close_fds=True,
            )
        except OSError as exc:
            raise LauncherError(f"Could not launch command: {exc}") from exc


def persist_or_status(
    config: dict[str, Any], path: Path, state: UiState, success: str
) -> None:
    try:
        save_config(config, path)
    except OSError as exc:
        state.status = f"Could not save: {first_line(str(exc))}"
        state.status_error = True
    else:
        state.status = success
        state.status_error = False


def tui(
    stdscr: curses.window,
    *,
    apps: list[DeviceApp],
    config: dict[str, Any],
    paths: Paths,
    serial: str,
    serial_in_launch_command: Optional[str],
    scrcpy: str,
    adb: str,
    initial_warning: str,
) -> None:
    stdscr.keypad(True)
    try:
        curses.use_default_colors()
    except curses.error:
        pass

    state = UiState(status=initial_warning, status_error=bool(initial_warning))

    while True:
        visible = draw(stdscr, apps, config, serial, state)
        key = stdscr.get_wch()

        if key == curses.KEY_RESIZE:
            continue

        # Ctrl+H globally toggles visibility of hidden apps.
        if key == "\x08":
            old_pkg = selected_package(visible, state)
            state.show_hidden = not state.show_hidden
            new_visible = filtered_apps(
                apps, config, serial, state.query, state.show_hidden
            )
            keep_selection(state, old_pkg, new_visible)
            state.status = (
                "Hidden entries are now visible."
                if state.show_hidden
                else "Hidden entries are now excluded."
            )
            state.status_error = False
            continue

        if state.focus == "search":
            if key in ("\n", "\r", curses.KEY_ENTER, curses.KEY_DOWN, "\t"):
                state.focus = "list"
                state.status = ""
                continue
            if key == curses.KEY_UP:
                state.focus = "list"
                if visible:
                    state.selected = len(visible) - 1
                state.status = ""
                continue
            if key == "\x1b":
                state.focus = "list"
                state.status = ""
                continue
            if key == curses.KEY_LEFT:
                state.query_cursor = max(0, state.query_cursor - 1)
                continue
            if key == curses.KEY_RIGHT:
                state.query_cursor = min(len(state.query), state.query_cursor + 1)
                continue
            if key in (curses.KEY_HOME, "\x01"):
                state.query_cursor = 0
                continue
            if key in (curses.KEY_END, "\x05"):
                state.query_cursor = len(state.query)
                continue
            if key in (curses.KEY_BACKSPACE, "\x7f"):
                if state.query_cursor > 0:
                    old_pkg = selected_package(visible, state)
                    pos = state.query_cursor
                    state.query = state.query[: pos - 1] + state.query[pos:]
                    state.query_cursor -= 1
                    new_visible = filtered_apps(
                        apps, config, serial, state.query, state.show_hidden
                    )
                    keep_selection(state, old_pkg, new_visible)
                continue
            if key == curses.KEY_DC:
                if state.query_cursor < len(state.query):
                    old_pkg = selected_package(visible, state)
                    pos = state.query_cursor
                    state.query = state.query[:pos] + state.query[pos + 1 :]
                    new_visible = filtered_apps(
                        apps, config, serial, state.query, state.show_hidden
                    )
                    keep_selection(state, old_pkg, new_visible)
                continue
            if key == "\x15":  # Ctrl+U
                old_pkg = selected_package(visible, state)
                state.query = ""
                state.query_cursor = 0
                new_visible = filtered_apps(
                    apps, config, serial, state.query, state.show_hidden
                )
                keep_selection(state, old_pkg, new_visible)
                continue
            if isinstance(key, str) and key.isprintable():
                old_pkg = selected_package(visible, state)
                pos = state.query_cursor
                state.query = state.query[:pos] + key + state.query[pos:]
                state.query_cursor += len(key)
                new_visible = filtered_apps(
                    apps, config, serial, state.query, state.show_hidden
                )
                keep_selection(state, old_pkg, new_visible)
                state.status = ""
                continue
            continue

        # Modo lista.
        if key in ("q", "Q", "\x1b"):
            return
        if key in ("f", "F", "/"):
            state.focus = "search"
            state.query_cursor = len(state.query)
            state.status = ""
            continue
        if key == curses.KEY_UP:
            if visible:
                state.selected = (state.selected - 1) % len(visible)
            continue
        if key == curses.KEY_DOWN:
            if visible:
                state.selected = (state.selected + 1) % len(visible)
            continue
        if key == curses.KEY_PPAGE:
            step = max(1, stdscr.getmaxyx()[0] - 4)
            state.selected = max(0, state.selected - step)
            continue
        if key == curses.KEY_NPAGE:
            step = max(1, stdscr.getmaxyx()[0] - 4)
            state.selected = min(max(0, len(visible) - 1), state.selected + step)
            continue
        if key == curses.KEY_HOME:
            state.selected = 0
            continue
        if key == curses.KEY_END:
            state.selected = max(0, len(visible) - 1)
            continue

        if key == "R":
            old_pkg = selected_package(visible, state)
            state.status = "Reloading applications from the device…"
            state.status_error = False
            draw(stdscr, apps, config, serial, state)
            try:
                refreshed, warning = load_user_apps(scrcpy, adb, serial)
            except LauncherError as exc:
                state.status = f"Could not reload: {first_line(str(exc))}"
                state.status_error = True
            else:
                apps[:] = refreshed
                new_visible = filtered_apps(
                    apps, config, serial, state.query, state.show_hidden
                )
                keep_selection(state, old_pkg, new_visible)
                state.status = warning or f"Reloaded {len(apps)} applications."
                state.status_error = bool(warning)
            continue

        if not visible:
            continue
        state.selected = max(0, min(state.selected, len(visible) - 1))
        app = visible[state.selected]
        prefs = app_prefs(config, serial, app.package)

        if key in ("\n", "\r") or key == curses.KEY_ENTER:
            command = command_for(app, prefs, serial_in_launch_command)
            try:
                command = command_with_window_title(
                    command, display_name(app, prefs)
                )
                launch_command(command, paths.log)
            except LauncherError as exc:
                state.status = first_line(str(exc))
                state.status_error = True
            else:
                state.status = f"Launched: {display_name(app, prefs)} · log: {paths.log}"
                state.status_error = False
            continue

        if key == "e":
            current = command_for(app, prefs, serial_in_launch_command)
            edited = line_editor(
                stdscr,
                f"Edit command for {display_name(app, prefs)}",
                current,
                empty_hint="empty = default",
            )
            if edited is None:
                state.status = "Edit discarded."
                state.status_error = False
            else:
                edited = edited.strip()
                if edited:
                    prefs["command"] = edited
                    message = "Command saved."
                else:
                    prefs.pop("command", None)
                    message = "Command reset to default."
                persist_or_status(config, paths.config, state, message)
            continue

        if key == "r":
            current = display_name(app, prefs)
            edited = line_editor(
                stdscr,
                f"Rename {app.name} ({app.package})",
                current,
                empty_hint="empty = original name",
            )
            if edited is None:
                state.status = "Rename discarded."
                state.status_error = False
            else:
                old_pkg = app.package
                edited = edited.strip()
                if edited and edited != app.name:
                    prefs["name"] = edited
                    message = f"Display name: {edited}"
                else:
                    prefs.pop("name", None)
                    message = "Original name restored."
                persist_or_status(config, paths.config, state, message)
                new_visible = filtered_apps(
                    apps, config, serial, state.query, state.show_hidden
                )
                keep_selection(state, old_pkg, new_visible)
            continue

        if key == "h":
            old_pkg = app.package
            new_value = not is_hidden(prefs)
            if new_value:
                prefs["hidden"] = True
                message = f"Hidden: {display_name(app, prefs)}"
            else:
                prefs.pop("hidden", None)
                message = f"Shown: {display_name(app, prefs)}"
            persist_or_status(config, paths.config, state, message)
            new_visible = filtered_apps(
                apps, config, serial, state.query, state.show_hidden
            )
            keep_selection(state, old_pkg if state.show_hidden else None, new_visible)
            continue

        if key == "p":
            old_pkg = app.package
            new_value = not is_pinned(prefs)
            if new_value:
                prefs["pinned"] = True
                message = f"Pinned: {display_name(app, prefs)}"
            else:
                prefs.pop("pinned", None)
                message = f"Unpinned: {display_name(app, prefs)}"
            persist_or_status(config, paths.config, state, message)
            new_visible = filtered_apps(
                apps, config, serial, state.query, state.show_hidden
            )
            keep_selection(state, old_pkg, new_visible)
            continue


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Search and launch Android apps with scrcpy from a terminal UI."
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"%(prog)s {VERSION}",
    )
    parser.add_argument(
        "-s",
        "--serial",
        help="adb serial when multiple devices are connected",
    )
    parser.add_argument(
        "--scrcpy",
        default="scrcpy",
        help="scrcpy executable (default: scrcpy)",
    )
    parser.add_argument(
        "--adb",
        default="adb",
        help="adb executable (default: adb)",
    )
    parser.add_argument(
        "--serial-in-command",
        dest="serial_in_command",
        action="store_true",
        help="include --serial in generated scrcpy commands",
    )
    return parser


def main(argv: Optional[list[str]] = None) -> int:
    locale.setlocale(locale.LC_ALL, "")
    args = build_parser().parse_args(argv)
    paths = xdg_paths()

    if shutil.which(args.adb) is None and "/" not in args.adb:
        print(f"Error: adb was not found in PATH: {args.adb}", file=sys.stderr)
        return 2
    if shutil.which(args.scrcpy) is None and "/" not in args.scrcpy:
        print(f"Error: scrcpy was not found in PATH: {args.scrcpy}", file=sys.stderr)
        return 2

    try:
        serial = resolve_device_serial(args.adb, args.serial)
        apps, apps_warning = load_user_apps(args.scrcpy, args.adb, serial)
    except LauncherError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2

    config, config_warning = load_config(paths.config)
    device_prefs(config, serial)
    warnings = " ".join(part for part in (apps_warning, config_warning) if part)

    serial_in_command = (
        serial
        if args.serial_in_command or args.serial or os.environ.get("ANDROID_SERIAL")
        else None
    )
    try:
        curses.wrapper(
            tui,
            apps=apps,
            config=config,
            paths=paths,
            serial=serial,
            serial_in_launch_command=serial_in_command,
            scrcpy=args.scrcpy,
            adb=args.adb,
            initial_warning=warnings,
        )
    except KeyboardInterrupt:
        return 130
    except curses.error as exc:
        print(f"Terminal/curses error: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
