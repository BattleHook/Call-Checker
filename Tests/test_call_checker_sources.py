import base64
import contextlib
import io
from pathlib import Path
import runpy
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import patch
import subprocess


ROOT = Path(__file__).resolve().parents[1]
with patch.object(sys, "path", [str(ROOT / "Tools"), *sys.path]):
    SOURCE = runpy.run_path(str(ROOT / "Tools" / "CALL_Checker_Sources.py"))


class SourceTests(unittest.TestCase):
    def test_sanitize_descriptions_preserves_calls_and_original_timestamp(self):
        maintenance = runpy.run_path(str(ROOT / "Tools" / "sanitize_descriptions.py"))
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "target.db"
            self.create_db(target, ["PA1ABC", "DL1ABC"])
            with contextlib.closing(sqlite3.connect(target)) as connection:
                connection.executescript(
                    "CREATE TABLE examples(name TEXT PRIMARY KEY, query TEXT NOT NULL, "
                    "description TEXT NOT NULL);"
                    "CREATE TABLE metadata(key TEXT PRIMARY KEY,value TEXT NOT NULL,description TEXT);"
                    "INSERT INTO examples VALUES ('bad','SELECT annual_rate FROM callsigns','old');"
                    "INSERT INTO metadata VALUES ('generated','original','old');"
                    "INSERT INTO metadata VALUES ('total_qsos','123','old');"
                )
                before = maintenance["fingerprint"](connection)
            self.assertEqual(maintenance["sanitize"](target), before)
            with contextlib.closing(sqlite3.connect(target)) as connection:
                self.assertEqual(maintenance["fingerprint"](connection), before)
                self.assertEqual(dict(connection.execute("SELECT key,value FROM metadata")), {
                    "callsigns": "2", "schema": "callsigns(callsign TEXT PRIMARY KEY)",
                    "generated": "original",
                })
                self.assertEqual(connection.execute("SELECT COUNT(*) FROM examples").fetchone(), (5,))

    def test_metadata_count_commits_with_calls_and_rolls_back_on_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory).resolve() / "target.db"
            self.create_db(target, ["DL1ABC"])
            self.assertEqual(SOURCE["update_target"](target, {"PA1ABC"}), (1, 2))
            with contextlib.closing(sqlite3.connect(target)) as connection:
                self.assertEqual(connection.execute(
                    "SELECT value FROM metadata WHERE key='callsigns'").fetchone(), ("2",))
                connection.execute("UPDATE metadata SET value='999' WHERE key='callsigns'")
                connection.commit()
            self.assertEqual(SOURCE["update_target"](target, {"PA1ABC"}), (0, 2))
            with contextlib.closing(sqlite3.connect(target)) as connection:
                self.assertEqual(connection.execute(
                    "SELECT value FROM metadata WHERE key='callsigns'").fetchone(), ("2",))
                connection.execute(
                    "CREATE TRIGGER reject_count BEFORE UPDATE ON metadata "
                    "BEGIN SELECT RAISE(ABORT, 'metadata blocked'); END"
                )
                connection.commit()
            with self.assertRaisesRegex(sqlite3.IntegrityError, "metadata blocked"):
                SOURCE["update_target"](target, {"DO1ABC"})
            with contextlib.closing(sqlite3.connect(target)) as connection:
                self.assertEqual(connection.execute(
                    "SELECT COUNT(*) FROM callsigns").fetchone(), (2,))
                self.assertEqual(connection.execute(
                    "SELECT value FROM metadata WHERE key='callsigns'").fetchone(), ("2",))

    def test_slash_normalization_conservative_base_selection(self):
        normalize = SOURCE["normalize_call"]
        for value, expected in [
            ("PA/DL1DL/P", "DL1DL"), ("DL1DL/PA", "DL1DL"), ("DL1DL/7", "DL1DL"),
            ("DL1DL/M", "DL1DL"), ("DL1DL/MM", "DL1DL"), ("DL1DL/AM", "DL1DL"),
            ("DL1DL/QRP", "DL1DL"), ("dl1dl/qrpp", "DL1DL"),
            ("F/2E0ABC/P", "2E0ABC"), ("KH6/PA1ABC", "PA1ABC"),
            ("PA/3DA0ABC/P", "3DA0ABC"), ("4U1UN/P", "4U1UN"),
            (" PA/DL1DL/P ", "DL1DL"),
        ]:
            with self.subTest(value=value):
                self.assertEqual(normalize(value), expected)
        for value in ("DL1DL/PA1ABC", "PA//DL1DL", "/DL1DL", "DL1DL/",
                      "PA/DL1DL/UNKNOWN", "PA/DL1", "PA/P", "PA1ABC/123",
                      "DL1DL/PA1ABC/P", "PA/DL1DL/!"):
            with self.subTest(value=value):
                self.assertIsNone(normalize(value))
        # Non-slash input retains the previous broad-format rule.
        self.assertEqual(normalize("YT100"), "YT100")

    def test_slash_import_deduplicates_base_reports_uncertainty_and_keeps_history(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source, target = root / "source.db", root / "target.db"
            self.create_db(source, [
                "PA/DL1DL/P", "DL1DL/P", "DL1DL", "DL1DL/7",
                "PA/2E0ABC/P", "DL1DL/PA1ABC", "DL1DL/UNKNOWN", None,
            ])
            self.create_db(target, ["DL1DL/P", "pa9zzz"])
            before = source.read_bytes()
            with contextlib.redirect_stdout(io.StringIO()):
                report = SOURCE["import_calls"]([(source, ())], target)
                repeated = SOURCE["import_calls"]([(source, ())], target)
            self.assertIn("Unique valid calls: 2; new calls added: 2; invalid values skipped: 3",
                          report)
            self.assertIn("Slash values skipped (ambiguous/unsupported): 2", report)
            self.assertIn("Total new calls added: 0", repeated)
            connection = sqlite3.connect(target)
            try:
                self.assertEqual(connection.execute(
                    "SELECT callsign FROM callsigns ORDER BY callsign"
                ).fetchall(), [("2E0ABC",), ("DL1DL",), ("DL1DL/P",), ("pa9zzz",)])
            finally:
                connection.close()
            self.assertEqual(source.read_bytes(), before)

    @unittest.skipUnless(
        Path(r"C:\Windows\SysWOW64\WindowsPowerShell\v1.0\powershell.exe").is_file(),
        "Requires installed Windows 32-bit PowerShell and Jet provider",
    )
    def test_real_access_helper_schema_calls_and_mixed_import(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "fixture.mdb"
            reference = root / "reference.mdb"
            script = r"""
$ErrorActionPreference = 'Stop'
$catalog = New-Object -ComObject ADOX.Catalog
$cs = 'Provider=Microsoft.Jet.OLEDB.4.0;Data Source=FIXTURE_PATH'
try { $null = $catalog.Create($cs); $catalog.ActiveConnection.Close() }
finally { [void][Runtime.InteropServices.Marshal]::FinalReleaseComObject($catalog) }
$connection = New-Object System.Data.OleDb.OleDbConnection($cs)
try {
    $connection.Open()
    $command = $connection.CreateCommand()
    try {
        foreach ($sql in @(
            'CREATE TABLE EmptyCalls ([Call] TEXT(32))',
            'CREATE TABLE SingleCall ([Callsign] TEXT(32))',
            'CREATE TABLE QSOs ([Call] TEXT(32))',
            "INSERT INTO SingleCall ([Callsign]) VALUES ('PA1ABC')",
            "INSERT INTO QSOs ([Call]) VALUES (' pa1abc ')",
            "INSERT INTO QSOs ([Call]) VALUES ('PA2DEF/P')",
            "INSERT INTO QSOs ([Call]) VALUES ('PA2DEF/P')",
            'INSERT INTO QSOs ([Call]) VALUES (NULL)'
        )) {
            $command.CommandText = $sql
            [void]$command.ExecuteNonQuery()
        }
    } finally { $command.Dispose() }
} finally { $connection.Dispose() }
$catalog = New-Object -ComObject ADOX.Catalog
try {
    $null = $catalog.Create('Provider=Microsoft.Jet.OLEDB.4.0;Data Source=REFERENCE_PATH')
    $null = $catalog.ActiveConnection.Execute('CREATE TABLE Parks ([Name] TEXT(32))')
    $catalog.ActiveConnection.Close()
} finally { [void][Runtime.InteropServices.Marshal]::FinalReleaseComObject($catalog) }
""".replace("FIXTURE_PATH", str(source).replace("'", "''")).replace(
                "REFERENCE_PATH", str(reference).replace("'", "''")
            )
            subprocess.run(
                [r"C:\Windows\SysWOW64\WindowsPowerShell\v1.0\powershell.exe",
                 "-NoProfile", "-NonInteractive", "-EncodedCommand",
                 base64.b64encode(script.encode("utf-16le")).decode("ascii")],
                capture_output=True, check=True, timeout=30,
            )
            before = source.read_bytes()
            reference_before = reference.read_bytes()
            tables = SOURCE["inspect_source"](source)
            self.assertEqual(
                {table["table"]: table["columns"] for table in tables},
                {"EmptyCalls": ["Call"], "SingleCall": ["Callsign"], "QSOs": ["Call"]},
            )
            self.assertEqual(SOURCE["read_calls"](source, ("EmptyCalls", "Call")), [])
            self.assertEqual(SOURCE["read_calls"](source, ("SingleCall", "Callsign")), ["PA1ABC"])
            self.assertEqual(set(SOURCE["read_calls"](source, ("QSOs", "Call"))),
                             {None, " pa1abc ", "PA2DEF/P"})
            sqlite_source, target = root / "source.db", root / "target.db"
            self.create_db(sqlite_source, ["PA1ABC", "PA3GHI"])
            self.create_db(target, ["PA9ZZZ"])
            with contextlib.redirect_stdout(io.StringIO()):
                report = SOURCE["import_calls"](
                    [(reference, ()), (source, ("QSOs", "Call")), (sqlite_source, ())], target
                )
            self.assertIn("SKIPPED: No supported CALL/CALLSIGN column.", report)
            self.assertIn("Total databases skipped: 1\nTotal databases committed: 2\n"
                          "Total databases read: 2", report)
            connection = sqlite3.connect(target)
            try:
                self.assertEqual(connection.execute(
                    "SELECT callsign FROM callsigns ORDER BY callsign"
                ).fetchall(), [("PA1ABC",), ("PA2DEF",), ("PA3GHI",), ("PA9ZZZ",)])
            finally:
                connection.close()
            self.assertEqual(source.read_bytes(), before)
            self.assertEqual(reference.read_bytes(), reference_before)

    def test_sqlite_metadata_only_and_file_unchanged(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "source.db"
            connection = sqlite3.connect(path)
            try:
                connection.execute('CREATE TABLE "odd""table" (Call TEXT)')
                connection.commit()
            finally:
                connection.close()
            before = path.read_bytes()
            real_connect = sqlite3.connect
            with patch("sqlite3.connect", wraps=real_connect) as connect:
                self.assertEqual(SOURCE["inspect_source"](path), [
                    {"table": 'odd"table', "columns": ["Call"]}
                ])
                self.assertEqual(connect.call_args.args[0], path.resolve().as_uri() + "?mode=ro")
                self.assertEqual(connect.call_args.kwargs, {"uri": True})
            self.assertEqual(path.read_bytes(), before)

    def test_discovery_and_missing_database(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "nested"
            path.mkdir()
            (path / "sample.MDB").touch()
            (path / "ignored.txt").touch()
            self.assertEqual(SOURCE["discover"](directory), [path / "sample.MDB"])
            with self.assertRaises(FileNotFoundError):
                SOURCE["inspect_source"](path / "absent.db")
            self.assertFalse((path / "absent.db").exists())

    def test_scan_list_multiple_roots_relative_nested_and_duplicates(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            first, second = root / "first", root / "second"
            nested = first / "nested"
            nested.mkdir(parents=True)
            second.mkdir()
            (first / "a.mdb").touch()
            (nested / "b.db").touch()
            (second / "c.db").touch()
            listing = root / "scan_list.txt"
            listing.write_text("# allowed\nfirst\nfirst\\nested\n\nsecond\nfirst\n",
                               encoding="utf-8")
            roots, paths = SOURCE["scan_locations"](listing)
            self.assertEqual(roots, [first.resolve(), second.resolve()])
            self.assertEqual(paths, sorted(
                [path.resolve() for path in [first / "a.mdb", nested / "b.db", second / "c.db"]],
                key=lambda path: str(path).casefold()
            ))
            roots, paths = SOURCE["scan_locations"](listing, nested)
            self.assertEqual(roots, [nested.resolve()])
            self.assertEqual(paths, [(nested / "b.db").resolve()])

    def test_outside_scan_root_rejected_before_discovery_or_list_write(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            allowed, outside = root / "allowed", root / "allowed-copy"
            allowed.mkdir()
            outside.mkdir()
            scan_list = root / "scan_list.txt"
            scan_list.write_text(str(allowed.resolve()), encoding="utf-8")
            listing, report = root / "source_list.txt", root / "scan_result.txt"
            listing.write_text("# keep", encoding="utf-8")
            report.write_text("previous report", encoding="utf-8")
            with patch("os.walk", side_effect=AssertionError("Unauthorized traversal")), \
                    contextlib.redirect_stdout(io.StringIO()), \
                    contextlib.redirect_stderr(io.StringIO()) as errors:
                self.assertEqual(SOURCE["main"]([
                    "--scan", str(outside), "--scan-list", str(scan_list),
                    "--list", str(listing), "--update-list", "--scan-result",
                ]), 1)
            self.assertIn("outside allowed", errors.getvalue())
            self.assertEqual(listing.read_text(), "# keep")
            self.assertEqual(report.read_text(), "previous report")

    def test_invalid_scan_lists_fail_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            scan_list = root / "scan_list.txt"
            for text in (None, "# empty\n", "*.mdb", "missing-folder", "path|table|column"):
                with self.subTest(text=text):
                    if text is not None:
                        scan_list.write_text(text, encoding="utf-8")
                    with patch("os.walk", side_effect=AssertionError("Unexpected traversal")), \
                            contextlib.redirect_stdout(io.StringIO()), \
                            contextlib.redirect_stderr(io.StringIO()) as errors:
                        self.assertEqual(SOURCE["main"]([
                            "--scan-listed", "--scan-list", str(scan_list),
                            "--list", str(root / "source_list.txt"), "--update-list",
                            "--scan-result",
                        ]), 1)
                    self.assertIn("ERROR:", errors.getvalue())
                    self.assertFalse((root / "source_list.txt").exists())
                    self.assertFalse((root / "scan_result.txt").exists())

    def test_resolved_outside_path_is_refused(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            with patch("os.walk", return_value=[(str(root), [".."], [])]):
                with self.assertRaisesRegex(ValueError, "resolves outside allowed roots"):
                    SOURCE["discover"](root, [root])

    def test_scan_listed_cli_reports_all_roots_without_database_access(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            first, second = root / "first", root / "second"
            first.mkdir()
            second.mkdir()
            (first / "source.mdb").touch()
            (second / "other.db").touch()
            scan_list = root / "scan_list.txt"
            scan_list.write_text("first\nsecond\n", encoding="utf-8")
            listing = root / "source_list.txt"
            with patch("sqlite3.connect", side_effect=AssertionError("Unexpected read")), \
                    patch("subprocess.run", side_effect=AssertionError("Unexpected Access read")), \
                    contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(SOURCE["main"]([
                    "--scan-listed", "--scan-list", str(scan_list),
                    "--list", str(listing), "--update-list", "--scan-result",
                ]), 0)
            self.assertEqual(len(SOURCE["read_list"](listing)), 2)
            report = (root / "scan_result.txt").read_text(encoding="utf-8")
            self.assertIn(f"Allowed scan list: {scan_list.resolve()}", report)
            self.assertIn(f"Scan folder: {first.resolve()}", report)
            self.assertIn(f"Scan folder: {second.resolve()}", report)
            self.assertIn("Paths added to source list: 2", report)

    def test_access_uses_schema_helper_and_checks_errors(self):
        completed = subprocess.CompletedProcess([], 0, '[{"table":"QSOs","columns":["Call"]}]', "")
        with patch("subprocess.run", return_value=completed) as run:
            result = SOURCE["inspect_access"](Path("example.mdb"))
            self.assertEqual(result[0]["table"], "QSOs")
            self.assertIn("SysWOW64", run.call_args.args[0][0])
            self.assertEqual(
                Path(run.call_args.args[0][5]),
                ROOT / "Tools" / "Helpers" / "CALL_Checker_SourceSchema.ps1",
            )
            self.assertTrue(Path(run.call_args.args[0][5]).is_file())
        with patch("subprocess.run", return_value=subprocess.CompletedProcess([], 1, "", "denied")):
            with self.assertRaisesRegex(RuntimeError, "denied"):
                SOURCE["inspect_access"](Path("example.mdb"))
        with patch("subprocess.run", return_value=subprocess.CompletedProcess([], 0, "{}", "")):
            with self.assertRaises(ValueError):
                SOURCE["inspect_access"](Path("example.mdb"))

    def test_default_only_lists_without_opening(self):
        with patch("sqlite3.connect", side_effect=AssertionError("Unexpected read")), \
                patch("subprocess.run", side_effect=AssertionError("Unexpected helper")), \
                contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(SOURCE["main"]([]), 0)

    def test_inspection_failure_is_visible_and_returns_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "missing.mdb"
            with contextlib.redirect_stdout(io.StringIO()), \
                    contextlib.redirect_stderr(io.StringIO()) as errors:
                self.assertEqual(SOURCE["main"](["--source", str(path), "--inspect"]), 1)
            self.assertIn("ERROR", errors.getvalue())
            self.assertIn("does not exist", errors.getvalue())

    def create_db(self, path, rows):
        connection = sqlite3.connect(path)
        try:
            connection.execute("CREATE TABLE callsigns(callsign TEXT PRIMARY KEY)")
            connection.executemany("INSERT INTO callsigns VALUES (?)", ((value,) for value in rows))
            connection.commit()
        finally:
            connection.close()

    def scan_list_args(self, root):
        path = root / "scan_list.txt"
        path.write_text(str(root.resolve()) + "\n", encoding="utf-8")
        return ["--scan-list", str(path)]

    def test_import_unique_normalized_calls_and_idempotence(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source, target = root / "source.db", root / "target.db"
            self.create_db(source, [" pa1abc ", "PA1ABC", "PA2DEF/P", None, "???", "ABC"])
            self.create_db(target, ["pa1abc"])
            before = source.read_bytes()
            listing = root / "source_list.txt"
            listing.write_text("# source\nsource.db\n", encoding="utf-8")
            with contextlib.redirect_stdout(io.StringIO()) as output:
                self.assertEqual(SOURCE["main"]([
                    "--import", "--list", str(listing), "--target", str(target)
                ]), 0)
                self.assertEqual(SOURCE["main"]([
                    "--import", "--list", str(listing), "--target", str(target)
                ]), 0)
            self.assertIn("3 invalid values skipped", output.getvalue())
            self.assertIn("0 added", output.getvalue())
            self.assertIn(f"Import progress: Reading database 1/1: {source.resolve()}",
                          output.getvalue())
            self.assertIn(f"Import progress: Updating target database: {target.resolve()}",
                          output.getvalue())
            report = (root / "import_result.txt").read_text(encoding="utf-8")
            self.assertEqual(report.count("Import date/time:"), 2)
            self.assertEqual(report.count("Status: COMMITTED"), 2)
            self.assertIn("Total new calls added: 1", report)
            self.assertIn("Total new calls added: 0", report)
            self.assertEqual(report.count("Total unique calls in target after import: 2"), 2)
            connection = sqlite3.connect(target)
            try:
                self.assertEqual(connection.execute(
                    "SELECT callsign FROM callsigns ORDER BY callsign"
                ).fetchall(), [("PA2DEF",), ("pa1abc",)])
            finally:
                connection.close()
            self.assertEqual(source.read_bytes(), before)

    def test_progress_is_flushed_before_reading_each_source_and_target(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            first, second, target = root / "first.db", root / "second.db", root / "target.db"
            self.create_db(first, ["PA1ABC"])
            self.create_db(second, ["PA2DEF"])
            self.create_db(target, [])
            inspect = SOURCE["inspect_source"]
            with patch("builtins.print") as printed:
                def inspect_with_progress(path):
                    printed.assert_called_with(
                        f"Import progress: Reading database "
                        f"{1 if path == first else 2}/2: {path}", flush=True
                    )
                    return inspect(path)

                with patch.dict(SOURCE["import_calls"].__globals__,
                                inspect_source=inspect_with_progress):
                    SOURCE["import_calls"]([(first, ()), (second, ())], target)
                printed.assert_any_call(
                    f"Import progress: Updating target database: {target.resolve()} "
                    f"(source 2/2: {second})", flush=True
                )

    def test_no_calls_mdb_is_skipped_and_later_sqlite_imports(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            parks, valid, target = root / "POTA.mdb", root / "valid.db", root / "target.db"
            parks.touch()
            self.create_db(valid, ["PA1ABC"])
            self.create_db(target, ["PA9ZZZ"])
            listing = root / "source_list.txt"
            listing.write_text("POTA.mdb\nvalid.db\n", encoding="utf-8")
            before = parks.read_bytes()
            metadata = [{"table": "Parks", "columns": ["Reference", "Name", "Grid"]}]
            with patch.dict(SOURCE["import_calls"].__globals__,
                            inspect_access=lambda path: metadata), \
                    contextlib.redirect_stdout(io.StringIO()) as output:
                self.assertEqual(SOURCE["main"]([
                    "--import", "--list", str(listing), "--target", str(target)
                ]), 0)
            self.assertIn("SKIPPED:", output.getvalue())
            self.assertIn("Reading database 2/2:", output.getvalue())
            report = (root / "import_result.txt").read_text(encoding="utf-8")
            self.assertIn("SKIPPED: No supported CALL/CALLSIGN column.", report)
            self.assertIn("Total databases inspected: 2\nTotal databases skipped: 1\n"
                          "Total databases committed: 1\nTotal databases read: 1", report)
            self.assertIn("Total new calls added: 1", report)
            self.assertIn("Total unique calls in target after import: 2", report)
            connection = sqlite3.connect(target)
            try:
                self.assertEqual(connection.execute("SELECT callsign FROM callsigns "
                                                    "ORDER BY callsign").fetchall(),
                                 [("PA1ABC",), ("PA9ZZZ",)])
            finally:
                connection.close()
            self.assertEqual(parks.read_bytes(), before)

    def test_all_unsupported_sqlite_sources_leave_target_read_only(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source, target = root / "reference.db", root / "target.db"
            connection = sqlite3.connect(source)
            try:
                connection.execute("CREATE TABLE Parks(Name TEXT)")
                connection.commit()
            finally:
                connection.close()
            self.create_db(target, ["PA9ZZZ"])
            before = target.read_bytes()
            real_connect = sqlite3.connect
            with patch("sqlite3.connect", wraps=real_connect) as connect, \
                    contextlib.redirect_stdout(io.StringIO()):
                report = SOURCE["import_calls"]([(source, ())], target)
            self.assertIn(target.resolve().as_uri() + "?mode=ro",
                          [call.args[0] for call in connect.call_args_list])
            self.assertTrue(all(call.args[0].endswith("?mode=ro")
                                for call in connect.call_args_list))
            self.assertIn("Total databases skipped: 1", report)
            self.assertIn("Total databases read: 0", report)
            self.assertIn("Total new calls added: 0", report)
            self.assertIn("Total unique calls in target after import: 1", report)
            self.assertIn("Status: NO CHANGES (all sources skipped)", report)
            self.assertNotIn("COMMITTED", report)
            self.assertEqual(target.read_bytes(), before)

    def test_ambiguous_and_bad_explicit_selection_still_abort_without_writes(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source, target = root / "source.db", root / "target.db"
            self.create_db(source, ["PA1ABC"])
            self.create_db(target, ["PA9ZZZ"])
            connection = sqlite3.connect(source)
            try:
                connection.execute("CREATE TABLE Queue(Call TEXT)")
                connection.commit()
            finally:
                connection.close()
            before = target.read_bytes()
            for selection, message in [((), "Ambiguous"), (("missing", "Call"), "not found")]:
                with self.subTest(selection=selection), \
                        contextlib.redirect_stdout(io.StringIO()), \
                        self.assertRaisesRegex(RuntimeError, message):
                    SOURCE["import_calls"]([(source, selection)], target)
                self.assertEqual(target.read_bytes(), before)
            self.assertIsNone(SOURCE["select_column"](
                [{"table": "Summits", "columns": ["ActivationCall"]}], ()
            ))

    def test_packaged_pota_versions_are_filtered_by_defaults(self):
        paths = [Path(r"C:\SyntheticData") / name
                 for name in ["POTA.mdb", "POTA 20261020.mdb", "SOTA.mdb",
                              "SOTA 20261020.mdb", "RDA.mdb"]]
        defaults = ROOT / "Build" / "Defaults" / "source_filter.txt"
        eligible, excluded = SOURCE["filter_sources"](paths, defaults)
        self.assertEqual(eligible, [paths[4]])
        self.assertEqual(len(excluded), 4)

    def test_dos_help_and_no_argument_helpers_do_not_access_databases(self):
        tool = ROOT / "Tools" / "CALL_Checker_Sources.py"
        for flag in ("/H", "/h", "/?", "--help"):
            with self.subTest(flag=flag):
                result = subprocess.run(
                    [sys.executable, "-B", str(tool), flag],
                    capture_output=True, text=True, timeout=30,
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn("--import", result.stdout)
                self.assertNotIn("NameError", result.stderr)
        powershell = Path(r"C:\Windows\SysWOW64\WindowsPowerShell\v1.0\powershell.exe")
        if not powershell.is_file():
            self.skipTest("Windows 32-bit PowerShell required")
        for name in ("CALL_Checker_MDB", "CALL_Checker_SourceSchema"):
            for flags in ([], ["/H"], ["/?"], ["-Help"], ["-H"]):
                with self.subTest(helper=name, flags=flags):
                    result = subprocess.run(
                        [str(powershell), "-NoProfile", "-NonInteractive", "-File",
                         str(ROOT / "Tools" / "Helpers" / f"{name}.ps1"), *flags],
                        capture_output=True, text=True, timeout=30,
                    )
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertIn("Without arguments:", result.stdout)
                    self.assertIn("-DatabasePath", result.stdout)

    def test_report_attributes_overlap_once_and_preserves_previous_blocks(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            first, second, target = root / "first.db", root / "second.db", root / "target.db"
            self.create_db(first, [" pa1abc ", "PA1ABC", "PA9ZZZ", "???"])
            self.create_db(second, ["PA1ABC", "PA2DEF"])
            self.create_db(target, ["PA9ZZZ", "PA0OLD"])
            listing, report_path = root / "source_list.txt", root / "import_result.txt"
            listing.write_text("first.db\nsecond.db\n", encoding="utf-8")
            report_path.write_text("Previous import\n\n", encoding="utf-8")
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(SOURCE["main"]([
                    "--import", "--list", str(listing), "--target", str(target)
                ]), 0)
            report = report_path.read_text(encoding="utf-8")
            self.assertTrue(report.startswith("Previous import\n\nImport date/time: "))
            self.assertRegex(report.splitlines()[2],
                             r"^Import date/time: \d{4}-\d{2}-\d{2}T.*[+-]\d{2}:\d{2}$")
            self.assertIn(f"Database: {first.resolve()}\n"
                          "  Unique valid calls: 2; new calls added: 1; invalid values skipped: 1",
                          report)
            self.assertIn(f"Database: {second.resolve()}\n"
                          "  Unique valid calls: 2; new calls added: 1; invalid values skipped: 0",
                          report)
            self.assertIn("Total databases read: 2\nTotal new calls added: 2", report)
            self.assertIn("Total unique calls in target after import: 4", report)
            self.assertNotIn("PA1ABC", report)
            listing.write_text("missing.db\n", encoding="utf-8")
            with contextlib.redirect_stdout(io.StringIO()), \
                    contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(SOURCE["main"]([
                    "--import", "--list", str(listing), "--target", str(target)
                ]), 1)
            updated = report_path.read_text(encoding="utf-8")
            self.assertTrue(updated.startswith(report))
            self.assertIn("Status: FAILED", updated)
            self.assertIn("missing.db", updated)

    def test_report_append_failure_explicitly_reports_committed_import(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source, target = root / "source.db", root / "target.db"
            self.create_db(source, ["PA1ABC"])
            self.create_db(target, [])
            listing = root / "source_list.txt"
            listing.write_text("source.db\n", encoding="utf-8")
            (root / "import_result.txt").mkdir()
            with contextlib.redirect_stdout(io.StringIO()), \
                    contextlib.redirect_stderr(io.StringIO()) as errors:
                self.assertEqual(SOURCE["main"]([
                    "--import", "--list", str(listing), "--target", str(target)
                ]), 1)
            self.assertIn("completed source transactions remain saved", errors.getvalue())
            self.assertIn("report append failed", errors.getvalue())
            connection = sqlite3.connect(target)
            try:
                self.assertEqual(connection.execute("SELECT callsign FROM callsigns").fetchall(),
                                 [("PA1ABC",)])
            finally:
                connection.close()

    def test_report_path_may_not_be_target(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source, target = root / "source.db", root / "import_result.txt"
            self.create_db(source, ["PA1ABC"])
            self.create_db(target, [])
            before = target.read_bytes()
            listing = root / "source_list.txt"
            listing.write_text("source.db\n", encoding="utf-8")
            with contextlib.redirect_stdout(io.StringIO()), \
                    contextlib.redirect_stderr(io.StringIO()) as errors:
                self.assertEqual(SOURCE["main"]([
                    "--import", "--list", str(listing), "--target", str(target)
                ]), 1)
            self.assertIn("Import report may not overwrite", errors.getvalue())
            self.assertEqual(target.read_bytes(), before)

    def test_later_failure_preserves_completed_source(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source, target = root / "source.db", root / "target.db"
            self.create_db(source, ["PA1ABC"])
            self.create_db(target, ["PA9ZZZ"])
            with contextlib.redirect_stdout(io.StringIO()):
                with self.assertRaises(SOURCE["ImportFailure"]) as failure:
                    SOURCE["import_calls"]([(source, ()), (root / "missing.db", ())], target)
            self.assertIn("Status: PARTIAL", failure.exception.report)
            self.assertIn("Total new calls added: 1", failure.exception.report)
            connection = sqlite3.connect(target)
            try:
                self.assertEqual(connection.execute("SELECT * FROM callsigns "
                                                    "ORDER BY callsign").fetchall(),
                                 [("PA1ABC",), ("PA9ZZZ",)])
            finally:
                connection.close()
            with self.assertRaises(ValueError):
                SOURCE["import_calls"]([(target, ())], target)
            missing = root / "new.db"
            with contextlib.redirect_stdout(io.StringIO()):
                with self.assertRaises(SOURCE["ImportFailure"]):
                    SOURCE["import_calls"]([(source, ())], missing)
            self.assertFalse(missing.exists())

    def test_per_source_commit_persists_before_next_read_and_partial_report(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            first, second, third, target = (root / name for name in
                                             ["first.db", "second.db", "third.db", "target.db"])
            self.create_db(first, ["PA1ABC"])
            self.create_db(second, ["PA2DEF", "PA3GHI"])
            self.create_db(third, ["PA4JKL"])
            self.create_db(target, ["PA9ZZZ"])
            snapshots = {path: path.read_bytes() for path in (first, second, third)}
            connection = sqlite3.connect(target)
            try:
                connection.execute(
                    "CREATE TRIGGER reject_second BEFORE INSERT ON callsigns "
                    "WHEN NEW.callsign = 'PA3GHI' BEGIN SELECT RAISE(ABORT, 'second refused'); END"
                )
                connection.commit()
            finally:
                connection.close()
            listing = root / "source_list.txt"
            listing.write_text("first.db\nsecond.db\nthird.db\n", encoding="utf-8")
            inspected = []
            inspect = SOURCE["inspect_source"]

            def inspect_after_commit(path):
                inspected.append(path.name)
                if path.name == "second.db":
                    reader = sqlite3.connect(target.resolve().as_uri() + "?mode=ro", uri=True)
                    try:
                        self.assertEqual(reader.execute(
                            "SELECT callsign FROM callsigns ORDER BY callsign"
                        ).fetchall(), [("PA1ABC",), ("PA9ZZZ",)])
                    finally:
                        reader.close()
                return inspect(path)

            with patch.dict(SOURCE["import_calls"].__globals__,
                            inspect_source=inspect_after_commit), \
                    contextlib.redirect_stdout(io.StringIO()), \
                    contextlib.redirect_stderr(io.StringIO()) as errors:
                self.assertEqual(SOURCE["main"]([
                    "--import", "--list", str(listing), "--target", str(target)
                ]), 1)
            self.assertIn("1 earlier source transaction(s) remain saved", errors.getvalue())
            self.assertEqual(inspected, ["first.db", "second.db"])
            report_path = root / "import_result.txt"
            report = report_path.read_text(encoding="utf-8")
            self.assertIn("Status: PARTIAL", report)
            self.assertIn("Total databases committed: 1", report)
            self.assertIn("Total new calls added: 1", report)
            self.assertIn("Total unique calls in target after import: 2", report)
            self.assertIn("FAILED: second refused", report)
            self.assertIn(f"Database: {third.resolve()}\n  NOT PROCESSED", report)
            connection = sqlite3.connect(target)
            try:
                self.assertEqual(connection.execute(
                    "SELECT callsign FROM callsigns ORDER BY callsign"
                ).fetchall(), [("PA1ABC",), ("PA9ZZZ",)])
                connection.execute("DROP TRIGGER reject_second")
                connection.commit()
            finally:
                connection.close()
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(SOURCE["main"]([
                    "--import", "--list", str(listing), "--target", str(target)
                ]), 0)
            updated = report_path.read_text(encoding="utf-8")
            self.assertTrue(updated.startswith(report))
            latest = updated[len(report):]
            self.assertIn("Status: COMMITTED", latest)
            self.assertIn("Unique valid calls: 1; new calls added: 0", latest)
            self.assertIn("Total new calls added: 3", latest)
            self.assertIn("Total unique calls in target after import: 5", latest)
            for path, data in snapshots.items():
                self.assertEqual(path.read_bytes(), data)

    def test_write_failure_rolls_back_all_insertions(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source, target = root / "source.db", root / "target.db"
            self.create_db(source, ["PA1ABC", "PA2DEF"])
            self.create_db(target, ["PA9ZZZ"])
            connection = sqlite3.connect(target)
            try:
                connection.execute(
                    "CREATE TRIGGER reject_call BEFORE INSERT ON callsigns "
                    "WHEN NEW.callsign = 'PA2DEF' BEGIN SELECT RAISE(ABORT, 'fixture refusal'); END"
                )
                connection.commit()
            finally:
                connection.close()
            with contextlib.redirect_stdout(io.StringIO()):
                with self.assertRaises(SOURCE["ImportFailure"]):
                    SOURCE["import_calls"]([(source, ())], target)
            listing = root / "source_list.txt"
            listing.write_text("source.db\n", encoding="utf-8")
            with contextlib.redirect_stdout(io.StringIO()), \
                    contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(SOURCE["main"]([
                    "--import", "--list", str(listing), "--target", str(target)
                ]), 1)
            report = (root / "import_result.txt").read_text(encoding="utf-8")
            self.assertIn("Status: FAILED", report)
            self.assertIn("fixture refusal", report)
            self.assertIn("Total new calls added: 0", report)
            connection = sqlite3.connect(target)
            try:
                self.assertEqual(connection.execute("SELECT * FROM callsigns").fetchall(),
                                 [("PA9ZZZ",)])
            finally:
                connection.close()

    def test_selection_refuses_ambiguity_and_recognizes_case(self):
        tables = [{"table": "QSOs", "columns": ["Call"]},
                  {"table": "Queue", "columns": ["CALLSIGN"]}]
        with self.assertRaises(ValueError):
            SOURCE["select_column"](tables, ())
        self.assertEqual(SOURCE["select_column"](tables, ("qsos", "CALL")), ("QSOs", "Call"))
        with self.assertRaises(ValueError):
            SOURCE["select_column"](tables, ("QSOs", "wrong"))

    def test_list_update_preserves_entries_and_deduplicates(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            listing = root / "source_list.txt"
            listing.write_text("# chosen source\nsource.db|QSOs|Call\n", encoding="utf-8")
            count = SOURCE["update_list"](
                listing, [root / "source.db", root / "new.mdb", root / "new.mdb",
                          root / "target.db"], root / "target.db"
            )
            self.assertEqual(count, 1)
            self.assertTrue(listing.read_text().startswith("# chosen source\nsource.db|QSOs|Call\n"))
            entries = SOURCE["read_list"](listing)
            self.assertEqual(entries[0], ((root / "source.db").resolve(), ("QSOs", "Call")))
            self.assertEqual(entries[1], ((root / "new.mdb").resolve(), ()))
            self.assertEqual(SOURCE["update_list"](listing, [root / "new.mdb"], root / "target.db"), 0)

    def test_access_call_protocol(self):
        result = subprocess.CompletedProcess([], 0, '["PA1ABC",null]', "")
        with patch("subprocess.run", return_value=result) as run:
            values = SOURCE["read_calls"](Path("fixture.mdb"), ("QSOs", "Call"))
            self.assertEqual(values, ["PA1ABC", None])
            self.assertEqual(run.call_args.args[0][-5:],
                             ["-ReadCalls", "-Table", "QSOs", "-Column", "Call"])
        with patch("subprocess.run", return_value=subprocess.CompletedProcess([], 0, "[3]", "")):
            with self.assertRaises(ValueError):
                SOURCE["read_calls"](Path("fixture.mdb"), ("QSOs", "Call"))

    def test_list_append_removes_only_trailing_blank_lines(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            listing = root / "source_list.txt"
            listing.write_text("# comment\n\nsource.db|QSOs|Call\n\n\n", encoding="utf-8")
            self.assertEqual(SOURCE["update_list"](
                listing, [root / "new.mdb"], root / "target.db"
            ), 1)
            self.assertEqual(listing.read_text(encoding="utf-8"),
                             "# comment\n\nsource.db|QSOs|Call\n"
                             + str((root / "new.mdb").resolve()) + "\n")

    def test_cli_scan_updates_list_without_opening_databases(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            listing, target = root / "source_list.txt", root / "target.db"
            (root / "source.mdb").touch()
            target.touch()
            with patch("sqlite3.connect", side_effect=AssertionError("Unexpected read")), \
                    patch("subprocess.run", side_effect=AssertionError("Unexpected helper")), \
                    contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(SOURCE["main"]([
                    "--scan", str(root), "--update-list",
                    *self.scan_list_args(root),
                    "--list", str(listing), "--target", str(target)
                ]), 0)
            self.assertEqual(SOURCE["read_list"](listing),
                             [((root / "source.mdb").resolve(), ())])

    def test_scan_report_replaces_and_marks_new_paths_without_reading_databases(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            listing, target = root / "source_list.txt", root / "target.db"
            old, new = root / "existing.mdb", root / "new.db"
            for path in (old, new, target):
                path.touch()
            listing.write_text("existing.mdb|QSOs|Call\n", encoding="utf-8")
            report = root / "scan_result.txt"
            report.write_text("Old report to replace\n", encoding="utf-8")
            arguments = ["--scan", str(root), "--update-list", "--scan-result",
                         *self.scan_list_args(root),
                         "--list", str(listing), "--target", str(target)]
            with patch("sqlite3.connect", side_effect=AssertionError("Unexpected read")), \
                    patch("subprocess.run", side_effect=AssertionError("Unexpected helper")), \
                    contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(SOURCE["main"](arguments), 0)
                first = report.read_text(encoding="utf-8")
                self.assertIn(f"[EXISTING] {old.resolve()}", first)
                self.assertIn(f"[NEW] {new.resolve()}", first)
                self.assertIn(f"[TARGET - excluded] {target.resolve()}", first)
                self.assertIn("Total databases found: 3", first)
                self.assertIn("New database paths: 1\nPaths added to source list: 1", first)
                self.assertNotIn("Old report", first)
                self.assertEqual(SOURCE["main"](arguments), 0)
            second = report.read_text(encoding="utf-8")
            self.assertEqual(second.count("Scan date/time:"), 1)
            self.assertNotIn("[NEW]", second)
            self.assertIn("Paths added to source list: 0", second)
            self.assertEqual(len(SOURCE["read_list"](listing)), 2)

    def test_scan_report_failure_reports_list_already_updated(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "new.mdb").touch()
            (root / "scan_result.txt").mkdir()
            listing = root / "source_list.txt"
            with contextlib.redirect_stdout(io.StringIO()), \
                    contextlib.redirect_stderr(io.StringIO()) as errors:
                self.assertEqual(SOURCE["main"]([
                    "--scan", str(root), "--update-list", "--scan-result",
                    *self.scan_list_args(root),
                    "--list", str(listing), "--target", str(root / "target.db")
                ]), 1)
            self.assertIn("Source list already updated", errors.getvalue())
            self.assertEqual(len(SOURCE["read_list"](listing)), 1)

    def test_filter_wildcards_case_and_literal_brackets(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            rules = root / "filter.txt"
            rules.write_text(
                "# exclusions\n*DXCC*\n*kopie*\n*copy*\n*abandon*\n*subdivision*\n"
                "log[1].mdb\n" + str(root.resolve() / "reference?.mdb") + "\n",
                encoding="utf-8",
            )
            names = ["DXCC.db", "gridDXCC123.mdb", "log - KOPIE (2).mdb",
                     "Copy log.mdb", "Log_ABANDONED_123.mdb",
                     "PrimarySubdivisionDatabase.mdb", "log[1].mdb",
                     "reference1.mdb", "log_Abandonned_123.mdb", "log1.mdb", "QSOs.mdb"]
            paths = [root / name for name in names]
            eligible, excluded = SOURCE["filter_sources"](paths, rules)
            self.assertEqual(eligible, paths[-2:])
            self.assertEqual(len(excluded), 9)

    def test_newest_prefers_undated_then_numeric_filename_not_mtime(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            rules = root / "filter.txt"
            rules.write_text(
                "LotW *.mdb (only newest not filtered)\n"
                "POTA *.mdb (only newest not filtered)\n"
                "*copy*\n", encoding="utf-8",
            )
            paths = [root / name for name in [
                "LotW 20260101.mdb", "LotW.mdb", "LotW 20261231.mdb",
                "POTA 9.mdb", "POTA 10.mdb", "POTA copy 999.mdb",
            ]]
            # Selection uses names only, even when these paths do not exist.
            eligible, excluded = SOURCE["filter_sources"](paths, rules)
            self.assertEqual(eligible, [paths[1], paths[4]])
            self.assertIn("selected:", excluded[paths[0]])
            self.assertIn("filter:", excluded[paths[-1]])
            dated = [root / "POTA 2026-09-30.mdb", root / "POTA 2026-10-01.mdb"]
            self.assertEqual(SOURCE["filter_sources"](dated, rules)[0], [dated[1]])

    def test_filtered_scan_only_adds_selected_paths_and_reports_exclusions(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            rules, listing = root / "filter.txt", root / "source_list.txt"
            rules.write_text("*DXCC*\nLotW *.mdb (only newest not filtered)\n",
                             encoding="utf-8")
            listing.write_text("# preserve existing exclusions\nDXCC.mdb|QSOs|Call\n",
                               encoding="utf-8")
            for name in ["DXCC.mdb", "LotW.mdb", "LotW 20261001.mdb", "QSOs.mdb"]:
                (root / name).touch()
            with patch("sqlite3.connect", side_effect=AssertionError("Unexpected read")), \
                    patch("subprocess.run", side_effect=AssertionError("Unexpected helper")), \
                    contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(SOURCE["main"]([
                    "--scan", str(root), "--update-list", "--scan-result",
                    *self.scan_list_args(root),
                    "--list", str(listing), "--filter", str(rules),
                    "--target", str(root / "target.db"),
                ]), 0)
            entries = SOURCE["read_list"](listing)
            self.assertEqual([path.name for path, _ in entries],
                             ["DXCC.mdb", "LotW.mdb", "QSOs.mdb"])
            self.assertEqual(entries[0][1], ("QSOs", "Call"))
            report = (root / "scan_result.txt").read_text(encoding="utf-8")
            self.assertIn("[FILTERED - filter: *DXCC*]", report)
            self.assertIn("[FILTERED - older version; selected:", report)
            self.assertIn(f"[NEW] {root.resolve() / 'LotW.mdb'}", report)
            self.assertIn("Filtered database paths: 2", report)
            self.assertIn("Paths added to source list: 2", report)

    def test_missing_or_invalid_filter_never_changes_list_or_report(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            listing, rules, report = (root / name for name in
                                      ["source_list.txt", "filter.txt", "scan_result.txt"])
            listing.write_text("# keep\n", encoding="utf-8")
            report.write_text("previous", encoding="utf-8")
            for content in [None, "LotW **.mdb (only newest not filtered)"]:
                if content is not None:
                    rules.write_text(content, encoding="utf-8")
                with contextlib.redirect_stdout(io.StringIO()), \
                        contextlib.redirect_stderr(io.StringIO()) as errors:
                    self.assertEqual(SOURCE["main"]([
                        "--scan", str(root), "--update-list", "--scan-result",
                        *self.scan_list_args(root),
                        "--list", str(listing), "--filter", str(rules),
                    ]), 1)
                self.assertIn("ERROR:", errors.getvalue())
                self.assertEqual(listing.read_text(), "# keep\n")
                self.assertEqual(report.read_text(), "previous")

    def test_malformed_list_and_invalid_target_are_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            listing = root / "source_list.txt"
            for text in ("source.db|QSOs", "source.db||Call", "source.db|QSOs|Call|extra"):
                listing.write_text(text, encoding="utf-8")
                with self.subTest(text=text), self.assertRaisesRegex(ValueError, "line 1"):
                    SOURCE["read_list"](listing)
            source, target = root / "source.db", root / "target.db"
            self.create_db(source, ["PA1ABC"])
            connection = sqlite3.connect(target)
            try:
                connection.execute("CREATE TABLE callsigns(callsign TEXT PRIMARY KEY, extra TEXT)")
                connection.commit()
            finally:
                connection.close()
            before = target.read_bytes()
            with contextlib.redirect_stdout(io.StringIO()), self.assertRaises(SOURCE["ImportFailure"]):
                SOURCE["import_calls"]([(source, ())], target)
            self.assertEqual(target.read_bytes(), before)


if __name__ == "__main__":
    unittest.main(verbosity=2)
