from __future__ import annotations

import importlib.util
import sys
import unittest
from pathlib import Path

MODULE_PATH = Path(__file__).resolve().parents[1] / "scrcpy_app_launcher.py"
SPEC = importlib.util.spec_from_file_location("scrcpy_app_launcher", MODULE_PATH)
assert SPEC is not None and SPEC.loader is not None
launcher = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = launcher
SPEC.loader.exec_module(launcher)


class ParseScrcpyAppsTests(unittest.TestCase):
    def test_parses_user_apps_and_excludes_system_apps(self) -> None:
        output = """
INFO: List of apps:
 * Settings                       com.android.settings
 - Signal                         org.thoughtcrime.securesms
 - Firefox                        org.mozilla.firefox
"""
        apps = launcher.parse_scrcpy_apps(output)
        self.assertEqual(
            [(app.name, app.package) for app in apps],
            [
                ("Signal", "org.thoughtcrime.securesms"),
                ("Firefox", "org.mozilla.firefox"),
            ],
        )

    def test_parses_long_name_with_package_on_next_line(self) -> None:
        output = """
INFO: List of apps:
 - This application name is definitely longer than thirty columns
                                  com.example.longname
"""
        apps = launcher.parse_scrcpy_apps(output)
        self.assertEqual(len(apps), 1)
        self.assertEqual(apps[0].package, "com.example.longname")
        self.assertEqual(
            apps[0].name,
            "This application name is definitely longer than thirty columns",
        )

    def test_removes_duplicate_packages(self) -> None:
        output = """
INFO: List of apps:
 - First label                    com.example.same
 - Second label                   com.example.same
"""
        apps = launcher.parse_scrcpy_apps(output)
        self.assertEqual(len(apps), 1)
        self.assertEqual(apps[0].name, "First label")


class CommandTests(unittest.TestCase):
    def test_default_command_contains_package_and_title(self) -> None:
        command = launcher.default_command(
            "com.example.app", "Example App", "device-123"
        )
        argv = launcher.shlex.split(command)
        self.assertIn("--serial=device-123", argv)
        self.assertIn("--start-app=com.example.app", argv)
        self.assertIn("--window-title=Example App", argv)

    def test_replaces_existing_window_title(self) -> None:
        command = "scrcpy --window-title='Old title' --start-app=com.example.app"
        updated = launcher.command_with_window_title(command, "New title")
        argv = launcher.shlex.split(updated)
        self.assertIn("--window-title=New title", argv)
        self.assertNotIn("Old title", argv)

    def test_adds_window_title_to_scrcpy_command(self) -> None:
        updated = launcher.command_with_window_title(
            "scrcpy --start-app=com.example.app", "Example App"
        )
        self.assertIn("--window-title=Example App", launcher.shlex.split(updated))

    def test_does_not_modify_non_scrcpy_commands(self) -> None:
        command = "echo hello"
        self.assertEqual(
            launcher.command_with_window_title(command, "Ignored"), command
        )


if __name__ == "__main__":
    unittest.main()
