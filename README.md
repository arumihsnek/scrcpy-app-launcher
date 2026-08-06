# scrcpy-app-launcher

A fast, dependency-free terminal UI for searching and launching user-installed Android apps in dedicated scrcpy virtual displays.

It reads the app labels and package names from a device connected through ADB, lets you filter the list interactively, and launches the selected app with a configurable scrcpy command.

## Why

`scrcpy --list-apps` is useful, but repeatedly finding a package name and rebuilding a long virtual-display command is not. `scrcpy-app-launcher` turns that workflow into a small keyboard-driven launcher with persistent per-device preferences.

## Features

- Instant case- and accent-insensitive filtering by app label or package name
- User-installed apps only
- Keyboard navigation with a curses TUI
- Launches apps in a scrcpy virtual display
- Uses the current display name as the scrcpy window title
- Rename apps locally without modifying Android
- Pin favorite apps to the top
- Hide unwanted entries and toggle hidden-app visibility
- Edit and persist a custom launch command per app
- Separate configuration for every connected Android device
- Handles multiple ADB devices through `--serial`
- Falls back to package-derived names if `scrcpy --list-apps` cannot be parsed
- No third-party Python packages

## Default launch command

```sh
scrcpy \
  --new-display=960x640/160 \
  --flex-display \
  --start-app=PACKAGE_NAME \
  --no-vd-system-decorations \
  --window-title='APP_NAME' \
  --window-borderless \
  --max-fps=120 \
  --video-codec=h265 \
  --video-bit-rate=30M
```

The title is injected at launch time, so a renamed app keeps the correct window title even when it has a saved custom scrcpy command.

## Requirements

- Linux or another Unix-like system with Python 3 and `curses`
- `adb`
- A recent `scrcpy` build supporting the options used above, especially `--list-apps`, `--new-display`, and `--start-app`
- An Android device connected and authorized through ADB

Check the connection first:

```sh
adb devices
```

## Installation

Clone the repository and install the script in your user PATH:

```sh
git clone https://github.com/arumihsnek/scrcpy-app-launcher.git
cd scrcpy-app-launcher
install -Dm755 scrcpy_app_launcher.py ~/.local/bin/scrcpy-app-launcher
```

Make sure `~/.local/bin` is in your `PATH`.

Run it:

```sh
scrcpy-app-launcher
```

For multiple connected devices:

```sh
scrcpy-app-launcher --serial DEVICE_SERIAL
```

To explicitly include the selected serial in generated scrcpy commands:

```sh
scrcpy-app-launcher --serial DEVICE_SERIAL --serial-in-command
```

## Controls

### Search field

| Key | Action |
| --- | --- |
| Type | Filter by app name or package ID |
| `Down`, `Enter`, `Tab` | Move to the app list |
| `Up` | Move to the end of the app list |
| `Ctrl+H` | Show or exclude hidden apps |
| `Ctrl+U` | Clear the search |

### App list

| Key | Action |
| --- | --- |
| `Up` / `Down` | Select an app |
| `Enter` | Launch the selected app |
| `f`, `F`, `/` | Focus the search field |
| `e` | Edit the app's launch command |
| `r` | Rename the app locally |
| `h` | Hide or unhide the app |
| `Ctrl+H` | Show or exclude hidden apps globally |
| `p` | Pin or unpin the app |
| `R` | Reload apps from the Android device |
| `Page Up` / `Page Down` | Move by one page |
| `Home` / `End` | Jump to the first or last entry |
| `q`, `Esc` | Quit |

In an editor, `Enter` saves and `Esc` discards. Saving an empty command restores the default command; saving an empty name restores the original Android label.

## Configuration and logs

Preferences are stored per ADB serial in:

```text
${XDG_CONFIG_HOME:-~/.config}/adb-scrcpy-app-launcher/config.json
```

Launch output is written to:

```text
${XDG_CACHE_HOME:-~/.cache}/adb-scrcpy-app-launcher/launch.log
```

Existing configuration from earlier versions using the same application ID remains compatible.

## Command-line options

```text
usage: scrcpy_app_launcher.py [-h] [--version] [-s SERIAL]
                              [--scrcpy SCRCPY] [--adb ADB]
                              [--serial-in-command]
```

Use custom executable paths when needed:

```sh
scrcpy-app-launcher --adb /path/to/adb --scrcpy /path/to/scrcpy
```

## Window icons

scrcpy exposes a window-title option, but it does not currently provide a portable command-line option for assigning a different desktop window icon to each launched Android app. Desktop icon behavior also varies between X11 and Wayland compositors, so per-app icons are intentionally outside this project's current scope.

## Testing

```sh
python -m unittest discover -s tests -v
python -m py_compile scrcpy_app_launcher.py
```

## License

MIT. See [LICENSE](LICENSE).
