"""Exercise a copied release with synthetic data only; never use station paths."""

import ctypes
from contextlib import closing
from ctypes import wintypes
import hashlib
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time


def main():
    release = Path(sys.argv[1]).resolve()
    assert not list(release.glob("*.db")), "Release must not ship a station database."
    with tempfile.TemporaryDirectory(prefix="callcheck-release-smoke-") as temporary:
        folder = Path(temporary)
        app = folder / "app"
        shutil.copytree(release, app)
        helper = app / "CALL_Checker_Sources.exe"
        environment = dict(os.environ, PATH=os.environ.get("WINDIR", r"C:\Windows"))

        def run(*arguments, expected=0):
            result = subprocess.run(
                [str(helper), *map(str, arguments)], cwd=folder, env=environment,
                capture_output=True, text=True, encoding="utf-8", timeout=60,
            )
            assert result.returncode == expected, result.stdout + result.stderr
            return result.stdout + result.stderr

        assert "--import" in run("/H")
        run()
        assert "ERROR" in run("--scan-listed", expected=1)
        source = folder / "synthetic.db"
        target = app / "CALL_CHECKER.DB"
        for path, calls in ((source, ["PA1ABC", "PA/DO1ABC/P"]), (target, ["DL1ABC"])):
            with closing(sqlite3.connect(path)) as connection:
                connection.execute("CREATE TABLE callsigns (callsign TEXT PRIMARY KEY)")
                connection.executemany("INSERT INTO callsigns VALUES (?)", [(c,) for c in calls])
                connection.commit()
        source_hash = hashlib.sha256(source.read_bytes()).digest()
        (app / "Tools" / "source_list.txt").write_text(str(source), encoding="utf-8")
        assert "callsign" in run("--inspect").lower()
        assert "Source 1/1 saved" in run("--import")
        assert "Source 1/1 saved" in run("--import")
        with closing(sqlite3.connect(target)) as connection:
            assert connection.execute("SELECT callsign FROM callsigns ORDER BY callsign").fetchall() == [
                ("DL1ABC",), ("DO1ABC",), ("PA1ABC",),
            ]
            assert connection.execute(
                "SELECT value FROM metadata WHERE key='callsigns'"
            ).fetchone() == ("3",)
        assert hashlib.sha256(source.read_bytes()).digest() == source_hash
        assert (app / "Tools" / "import_result.txt").is_file()
        print("PASS: compiled help/default paths/fail-closed scan/isolated import/idempotence.", flush=True)

        user32 = ctypes.WinDLL("user32", use_last_error=True)
        callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
        user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
        user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
        user32.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
        handles = []
        process = subprocess.Popen([str(app / "CALL_Checker_v0.1.exe")], cwd=folder, env=environment)
        try:
            @callback_type
            def inspect(handle, _):
                pid = wintypes.DWORD()
                user32.GetWindowThreadProcessId(handle, ctypes.byref(pid))
                title = ctypes.create_unicode_buffer(512)
                user32.GetWindowTextW(handle, title, len(title))
                if pid.value == process.pid and "Standalone Callsign Check Window v0.1" in title.value:
                    handles.append(handle)
                return True

            deadline = time.monotonic() + 20
            while not handles and time.monotonic() < deadline:
                assert process.poll() is None, "GUI exited before creating its main window."
                user32.EnumWindows(inspect, 0)
                time.sleep(0.1)
            assert handles, "Compiled GUI did not create its expected versioned window."
            time.sleep(1)
            assert process.poll() is None
            assert user32.PostMessageW(handles[0], 0x0010, 0, 0), "Cannot close test window."
            assert process.wait(timeout=10) == 0
            print("PASS: compiled GUI with bundled fonts/runtime starts and closes cleanly.", flush=True)
        finally:
            if process.poll() is None:
                process.terminate()
                process.wait(timeout=10)
    print("PASS: synthetic smoke files cleaned; release and station data untouched.")


if __name__ == "__main__":
    main()
