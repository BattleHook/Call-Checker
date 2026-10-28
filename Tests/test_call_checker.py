import runpy
import sqlite3
import sys
import tkinter as tk
from tkinter import font
import unittest
import tempfile
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch


class CallCheckerTests(unittest.TestCase):
    def setUp(self):
        root = Path(__file__).resolve().parents[1]
        with patch.object(sys, "path", [str(root), *sys.path]):
            self.namespace = runpy.run_path(str(root / "CALL_Checker.py"))
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.position_path = Path(temporary.name) / "report_window_position.json"
        self.namespace["SourceReportWindow"].__init__.__globals__["REPORT_POSITION_PATH"] = self.position_path
        self.root = tk.Tk()
        self.root.withdraw()
        self.addCleanup(self.root.destroy)
        self.clipboard_clear = patch.object(self.root, "clipboard_clear").start()
        self.clipboard_append = patch.object(self.root, "clipboard_append").start()
        self.addCleanup(patch.stopall)
        self.app = self.namespace["CALLCheckWindow"](self.root)
        self.rows = [("YT1E",), ("YT1A",), ("YT1AD",), ("AYT1",), ("YT2Z",)]

    def search(self, typed):
        connection = sqlite3.connect(":memory:")
        connection.execute("CREATE TABLE callsigns (callsign TEXT)")
        connection.executemany("INSERT INTO callsigns VALUES (?)", self.rows)
        self.app.update_entry_display(typed, f"1.{len(typed)}")
        with patch("sqlite3.connect", return_value=connection) as connect:
            self.app.search_and_highlight(typed, f"1.{len(typed)}")
            database = Path(__file__).resolve().parents[1] / "CALL_CHECKER.DB"
            self.assertEqual(connect.call_args.args[0], database.as_uri() + "?mode=ro")

    def test_frozen_paths_and_helper_do_not_relaunch_gui(self):
        folder = self.position_path.parent.resolve()
        with patch.object(sys, "frozen", True, create=True), \
                patch.object(sys, "executable", str(folder / "CALL_Checker_v0.1.exe")), \
                patch.object(sys, "path", [str(Path(__file__).resolve().parents[1]), *sys.path]):
            frozen = runpy.run_path(str(Path(__file__).resolve().parents[1] / "CALL_Checker.py"))
            self.assertEqual(frozen["DB_PATH"], folder / "CALL_CHECKER.DB")
            self.assertEqual(frozen["source_command"](["--import"]), [
                str(folder / "CALL_Checker_Sources.exe"), "--import",
                "--target", str(folder / "CALL_CHECKER.DB"),
            ])
        tools = Path(__file__).resolve().parents[1] / "Tools"
        with patch.object(sys, "frozen", True, create=True), \
                patch.object(sys, "executable", str(folder / "CALL_Checker_Sources.exe")), \
                patch.object(sys, "path", [str(tools), *sys.path]):
            sources = runpy.run_path(str(tools / "CALL_Checker_Sources.py"))
            self.assertEqual(sources["LIST_PATH"], folder / "Tools" / "source_list.txt")
            self.assertEqual(sources["TARGET_PATH"], folder / "CALL_CHECKER.DB")

    def test_sources_menu_scan_runs_tool_and_reports_completion(self):
        self.assertEqual(self.app.sources_menu.entrycget(0, "label"), "Scan sources")
        self.assertEqual(self.app.sources_menu.entrycget(1, "label"), "Import callsigns")
        with patch("threading.Thread") as thread, patch.object(self.root, "after"), \
                patch("subprocess.Popen") as run, patch("tkinter.messagebox.showinfo") as info, \
                patch.object(Path, "read_text", return_value="Scan date/time: test\n") as read:
            process = run.return_value.__enter__.return_value
            process.stdout = ["Source list updated: 2 new paths.\n", "Scan report replaced.\n"]
            process.wait.return_value = 0
            self.app.sources_menu.invoke(0)
            self.assertEqual(self.app.source_task, "Scan")
            self.assertEqual(self.app.sources_menu.entrycget(0, "state"), "disabled")
            self.app.scan_sources()
            self.assertEqual(thread.call_count, 1)
            thread.call_args.kwargs["target"]()
            command = run.call_args.args[0]
            self.assertEqual(Path(command[4]),
                             Path(__file__).resolve().parents[1] / "Tools" / "CALL_Checker_Sources.py")
            self.assertEqual(command[5:8],
                             ["--scan-listed", "--update-list", "--scan-result"])
            self.app.check_source_task()
            self.assertIsNone(self.app.source_task)
            self.assertEqual(self.app.sources_menu.entrycget(0, "state"), "normal")
            read.assert_called_once_with(encoding="utf-8")
            info.assert_not_called()
            self.assertEqual(self.app.report_dialog.text.get("1.0", "end-1c"),
                             "Scan date/time: test\n")

    def test_scan_report_is_separate_untruncated_scrollable_and_owned(self):
        contents = ("Scan date/time: test\n[FILTERED - filter: *copy*] C:\\verylong\\"
                    + "x" * 5000 + ".mdb\n[NEW] C:\\source.mdb\n")
        self.search("YT1")
        suggestions = self.app.text_display.get("1.0", "end-1c")
        ghost = self.app.ghost_call
        self.app.show_scan_results(contents)
        report = self.app.report_dialog
        self.assertEqual(report.text.get("1.0", "end-1c"), contents)
        self.assertEqual(report.text.cget("wrap"), "none")
        self.assertEqual(str(report.window.transient()), str(self.root))
        self.assertEqual([str(index) for index in report.text.tag_ranges("filtered")],
                         ["2.1", "2.9"])
        self.assertIn("bold", report.text.tag_cget("filtered", "font"))
        self.assertTrue(report.vertical.cget("command"))
        self.assertTrue(report.horizontal.cget("command"))
        self.assertEqual(int(report.vertical.grid_info()["column"]), 1)
        self.assertEqual(report.text.cget("background"), self.app.text_display.cget("background"))
        self.assertEqual(self.app.text_display.get("1.0", "end-1c"), suggestions)
        self.assertEqual(self.app.ghost_call, ghost)
        self.assertTrue(self.app.results_label.cget("text").startswith("Suggestions"))

    def test_scan_report_read_failure_is_visible(self):
        self.app.source_task = "Scan"
        self.app.source_result.put((0, "Scan completed", ""))
        with patch.object(Path, "read_text", side_effect=OSError("access denied")), \
                patch("tkinter.messagebox.showerror") as error:
            self.app.check_source_task()
            self.assertIn("access denied", error.call_args.args[1])
            self.assertIs(error.call_args.kwargs["parent"], self.root)

    def test_import_report_full_text_spacing_reuse_and_scroll(self):
        self.root.deiconify()
        output = (
            "Import progress: Reading database 1/2: first.mdb\n"
            + "first database details\n" * 250
            + "Import progress: Reading database 2/2: second.mdb\n"
            + "second database details\n" * 250 + "FINAL TOTAL: 123\n"
        )
        self.app.source_task = "Import"
        self.app.source_result.put((0, output, ""))
        self.app.check_source_task()
        self.root.update()
        report = self.app.report_dialog
        text = report.text.get("1.0", "end-1c")
        self.assertIn("first database details\n\nImport progress: Reading database 2/2:", text)
        self.assertIn("FINAL TOTAL: 123", text)
        self.assertGreater(len(text), 8000)
        self.assertEqual(report.text.cget("state"), "disabled")
        report.text.yview_moveto(1)
        self.root.update_idletasks()
        self.assertGreater(report.text.yview()[0], 0)
        self.app.show_scan_results("Scan report")
        self.assertIs(self.app.report_dialog, report)
        self.assertEqual(report.text.get("1.0", "end-1c"), "Scan report")

    def test_report_position_persists_and_restores(self):
        self.root.deiconify()
        self.app.show_scan_results("Scan report")
        self.root.update()
        report = self.app.report_dialog
        report.window.geometry("+100+100")
        self.root.update()
        expected = {"x": report.window.winfo_x(), "y": report.window.winfo_y()}
        self.assertTrue(report.close())
        self.assertEqual(json.loads(self.position_path.read_text()), expected)
        self.app.show_scan_results("New report")
        self.root.update()
        self.assertEqual(self.app.report_dialog.window.winfo_x(), expected["x"])
        self.assertEqual(self.app.report_dialog.window.winfo_y(), expected["y"])

    def test_report_position_invalid_minimized_and_save_failure(self):
        validator = self.namespace["valid_report_position"]
        self.assertFalse(validator(-32000, -32000))
        self.assertFalse(validator(100000, 100000))
        self.assertFalse(validator("100", 100))
        self.position_path.write_text('{"x": -32000, "y": -32000}')
        with patch("tkinter.messagebox.showwarning") as warning:
            self.app.show_scan_results("Report")
            warning.assert_called_once()
        report = self.app.report_dialog
        original = self.position_path.read_text()
        with patch.object(report.window, "state", return_value="iconic"):
            self.assertTrue(report.close())
        self.assertEqual(self.position_path.read_text(), original)
        self.position_path.unlink()
        self.root.deiconify()
        self.app.show_scan_results("Report")
        self.root.update()
        with patch.object(Path, "open", side_effect=OSError("not writable")), \
                patch("tkinter.messagebox.showerror") as error:
            self.assertFalse(self.app.report_dialog.close())
            self.assertIs(error.call_args.kwargs["parent"], self.app.report_dialog.window)

    def test_import_confirmation_and_refresh(self):
        with patch("tkinter.messagebox.askyesno", return_value=False) as confirm, \
                patch.object(self.app, "start_source_task") as start:
            self.app.import_sources()
            start.assert_not_called()
            self.assertIs(confirm.call_args.kwargs["parent"], self.root)
        with patch("tkinter.messagebox.askyesno", return_value=True), \
                patch.object(self.app, "start_source_task") as start:
            self.app.import_sources()
            start.assert_called_once_with("Import", ["--import"])
        self.app.update_entry_display("PA1", "1.3")
        self.app.source_task = "Import"
        self.app.source_result.put((0, "Import committed", ""))
        with patch.object(self.app, "search_and_highlight") as search, \
                patch("tkinter.messagebox.showinfo") as info:
            self.app.check_source_task()
            search.assert_called_once_with("PA1", "1.3")
            info.assert_not_called()
            self.assertIn("import_result.txt",
                          self.app.report_dialog.text.get("1.0", "end-1c"))

    def test_source_errors_and_close_guard(self):
        self.app.source_task = "Import"
        with patch.object(self.root, "destroy") as destroy, \
                patch("tkinter.messagebox.showwarning") as warning:
            self.app.close_window()
            destroy.assert_not_called()
            self.assertIs(warning.call_args.kwargs["parent"], self.root)
        self.app.source_result.put((1, "Import already committed", "Report append failed"))
        self.app.update_entry_display("PA1", "1.3")
        with patch.object(self.app, "show_source_report") as error, \
                patch.object(self.app, "search_and_highlight") as refresh:
            self.app.check_source_task()
            refresh.assert_called_once_with("PA1", "1.3")
            self.assertIn("already committed", error.call_args.args[1])
            self.assertIn("Report append failed", error.call_args.args[1])
        self.assertIsNone(self.app.source_task)
        with patch("threading.Thread") as thread, patch.object(self.root, "after"), \
                patch("subprocess.Popen", side_effect=OSError("Cannot launch helper")):
            self.app.scan_sources()
            thread.call_args.kwargs["target"]()
        with patch.object(self.app, "show_source_report") as error:
            self.app.check_source_task()
            self.assertIn("Cannot launch helper", error.call_args.args[1])

    def test_import_progress_streams_before_exit_and_survives_typing(self):
        messages = [
            "Import progress: Reading database 1/2: C:\\first.mdb\n",
            "Import progress: Reading database 2/2: C:\\second.db\n",
            "Import progress: Updating target database: C:\\target.db\n",
        ]
        with patch("threading.Thread") as thread, patch.object(self.root, "after"), \
                patch("subprocess.Popen") as popen:
            process = popen.return_value.__enter__.return_value

            def lines():
                for message in messages:
                    yield message
                    # The worker has queued progress before it reads another line
                    # or waits for process exit.
                    self.app.check_source_task()
                    self.assertIn(message.strip().removeprefix("Import progress: "),
                                  self.app.status.cget("text"))
                    self.assertEqual(self.app.source_task, "Import")
                    self.assertEqual(self.app.sources_menu.entrycget(1, "state"), "disabled")
                    process.wait.assert_not_called()

            process.stdout = lines()
            process.wait.return_value = 1
            self.app.start_source_task("Import", ["--import"])
            thread.call_args.kwargs["target"]()
            self.search("YT1")
            self.assertIn("Updating target database", self.app.status.cget("text"))
            self.app.entry_box.delete("1.0", "end")
            self.app.on_key_release(SimpleNamespace(keysym="BackSpace"))
            self.assertIn("Updating target database", self.app.status.cget("text"))
            with patch.object(self.app, "show_source_report") as error:
                self.app.check_source_task()
                for message in messages:
                    self.assertIn(message.strip(), error.call_args.args[1])
            self.assertIsNone(self.app.source_task)
            self.assertEqual(self.app.source_progress, "")

    def test_bundled_font_missing_or_unloadable_is_explicit(self):
        loader = self.namespace["load_callsign_fonts"].__wrapped__
        with patch.object(Path, "is_file", return_value=False):
            with self.assertRaisesRegex(FileNotFoundError, "font is missing"):
                loader()
        with patch("ctypes.WinDLL") as dll:
            dll.return_value.AddFontResourceExW.return_value = 0
            with self.assertRaisesRegex(OSError, "Cannot load bundled"):
                loader()
            self.assertEqual(dll.return_value.AddFontResourceExW.call_args.args[1], 0x10)

    def test_cascadia_display_preserves_ascii_zero_and_letter_o(self):
        for widget in (self.app.entry_box, self.app.ghost_label, self.app.text_display):
            self.assertEqual(font.Font(root=self.root, font=widget.cget("font")).actual("family"),
                             "Cascadia Mono")
        for tag in ("exact", "filtered"):
            self.assertEqual(font.Font(
                root=self.root, font=self.app.text_display.tag_cget(tag, "font")
            ).actual("family"), "Cascadia Mono")
        self.rows = [("DL100ANA",), ("DL1OANA",)]
        self.search("PA/DL1/P")
        self.assertIn("DL100ANA", self.app.text_display.get("1.0", "end-1c"))
        self.assertIn("DL1OANA", self.app.text_display.get("1.0", "end-1c"))
        self.app.move_suggestion(1)
        self.app.accept_ghost()
        self.clipboard_append.assert_called_with("PA/DL100ANA/P")

    def test_import_status_has_visible_space_with_long_path(self):
        self.root.deiconify()
        self.app.source_progress = (
            "Import running: Reading database 4/10: "
            r"C:\SyntheticData\Source.mdb"
        )
        self.app.status.configure(text=self.app.source_progress)
        self.root.update()
        self.assertTrue(self.app.status.winfo_ismapped())
        self.assertGreaterEqual(self.app.status.winfo_height(),
                                self.app.status.winfo_reqheight())
        self.assertLessEqual(self.app.status.winfo_y() + self.app.status.winfo_height(),
                             self.root.winfo_height())
        self.assertLessEqual(self.app.frame.winfo_y() + self.app.frame.winfo_height(),
                             self.app.status.winfo_y())
        self.search("YT1")
        self.assertEqual(self.app.status.cget("text"), self.app.source_progress)
        self.root.update_idletasks()
        self.assertGreaterEqual(self.app.status.winfo_height(),
                                self.app.status.winfo_reqheight())

    def test_alphabetical_callsigns_only_and_prefix_ghost(self):
        self.search("YT1")
        self.assertEqual(
            self.app.text_display.get("1.0", "end-1c"),
            "YT1A\nYT1AD\nYT1E\nAYT1\n",
        )
        self.assertEqual(self.app.ghost_call, "YT1A")
        self.assertEqual(self.app.entry_box.get("1.0", "end-1c"), "YT1")
        with patch.object(self.app.entry_box, "bbox", return_value=(30, 2, 10, 20)):
            self.app.refresh_ghost()
        self.assertEqual(self.app.ghost_label.cget("text"), "A")
        self.assertEqual(self.app.ghost_label.cget("background"), "#66EE33")
        self.assertTrue(self.app.ghost_label.place_info())

    def test_enter_accepts_completion_and_copies(self):
        self.assertTrue(self.app.entry_box.bind("<Return>"))
        self.assertTrue(self.app.entry_box.bind("<KP_Enter>"))
        self.assertFalse(self.app.entry_box.bind("<Tab>"))
        self.search("YT1")
        with patch.object(self.app, "search_and_highlight") as search:
            self.assertEqual(self.app.accept_ghost(), "break")
        self.assertEqual(self.app.entry_box.get("1.0", "end-1c"), "YT1A")
        self.assertEqual(self.app.current_typed, "YT1A")
        search.assert_called_once_with("YT1A", "1.4")
        self.clipboard_clear.assert_called_once_with()
        self.clipboard_append.assert_called_once_with("YT1A")

    def test_alphabetical_order_is_applied_before_result_limit(self):
        self.rows = [(f"YT1{i:02}",) for i in reversed(range(45))]
        self.search("YT1")
        expected = "".join(f"YT1{i:02}\n" for i in range(40))
        self.assertEqual(self.app.text_display.get("1.0", "end-1c"), expected)
        self.assertEqual(self.app.ghost_call, "YT100")

    def test_backspace_changes_only_actual_input(self):
        self.search("YT1")
        self.app.entry_box.delete("1.2", "1.3")
        self.app.on_key_release(SimpleNamespace(keysym="BackSpace"))
        self.assertEqual(self.app.entry_box.get("1.0", "end-1c"), "YT")
        self.assertEqual(self.app.ghost_call, "")
        self.assertFalse(self.app.ghost_label.place_info())
        self.assertEqual(self.app.status.cget("text"), "Type at least 3 characters to check...")

    def test_typing_clears_stale_ghost_and_debounces(self):
        self.search("YT1")
        self.app.entry_box.insert("end-1c", "E")
        self.app.on_key_release(SimpleNamespace(keysym="E"))
        self.assertEqual(self.app.current_typed, "YT1E")
        self.assertEqual(self.app.ghost_call, "")
        self.assertFalse(self.app.ghost_label.place_info())
        self.assertIsNotNone(self.app.search_after_id)
        self.root.after_cancel(self.app.search_after_id)

    def test_middle_cursor_hides_ghost_without_accepting(self):
        self.search("YT1")
        self.app.entry_box.mark_set(tk.INSERT, "1.1")
        self.app.refresh_ghost()
        self.assertFalse(self.app.ghost_label.place_info())
        with patch.object(self.app, "search_and_highlight"):
            self.assertEqual(self.app.accept_ghost(), "break")
        self.assertEqual(self.app.entry_box.get("1.0", "end-1c"), "YT1")
        self.clipboard_append.assert_called_once_with("YT1")

    def test_enter_without_ghost_copies_typed_text(self):
        self.search("ZZ9")
        with patch.object(self.app, "search_and_highlight"):
            self.assertEqual(self.app.accept_ghost(), "break")
        self.clipboard_append.assert_called_once_with("ZZ9")

    def test_enter_on_empty_input_does_not_change_clipboard(self):
        self.assertEqual(self.app.accept_ghost(), "break")
        self.clipboard_clear.assert_not_called()
        self.clipboard_append.assert_not_called()

    def test_no_prefix_or_no_matches_have_no_ghost(self):
        self.rows = [("AYT1",)]
        self.search("YT1")
        self.assertEqual(self.app.ghost_call, "")
        self.rows = []
        self.search("ZZ9")
        self.assertEqual(self.app.ghost_call, "")
        self.assertEqual(self.app.status.cget("text"), "No matches in database.")

    def test_database_error_is_explicit_and_clears_ghost(self):
        self.search("YT1")
        with patch("sqlite3.connect", side_effect=sqlite3.OperationalError("test failure")):
            self.app.search_and_highlight("YT1", "1.3")
        self.assertEqual(self.app.ghost_call, "")
        self.assertIn("test failure", self.app.status.cget("text"))

    def test_screenshot_master_matches_and_backgrounds(self):
        self.rows = [(call,) for call in ("YT1E", "YT1ET", "JT1E", "YT3E", "YT1A", "YT1Q")]
        self.search("YT1E")
        self.assertEqual(
            self.app.text_display.get("1.0", "end-1c"),
            "YT1E\nYT1ET\nYT1A\nYT1Q\nJT1E\nYT3E\n",
        )
        self.assertIn("red", self.app.text_display.tag_names("5.0"))
        self.assertIn("red", self.app.text_display.tag_names("3.3"))
        self.assertIn("exact", self.app.text_display.tag_names("1.0"))
        self.assertIn("green", self.app.text_display.tag_names("2.4"))
        self.assertIn("red", self.app.text_display.tag_names("6.2"))
        self.assertEqual(self.app.text_display.tag_cget("red", "background"), "#FF5050")
        self.assertEqual(self.app.text_display.tag_cget("green", "background"), "#66EE33")
        self.assertFalse(self.app.entry_box.tag_ranges("error"))
        self.assertEqual(self.app.ghost_call, "")

    def test_all_match_groups_and_alphabetical_ties(self):
        self.rows = [(call,) for call in (
            "YT1ZZ", "YT3E", "YT1Q", "XYT1E", "YT1ET", "YT1E",
            "JT1E", "YT1A", "AYT1E", "YT1EA", "YT1AA",
        )]
        self.search("YT1E")
        self.assertEqual(
            self.app.text_display.get("1.0", "end-1c"),
            "YT1E\nYT1EA\nYT1ET\nAYT1E\nXYT1E\nYT1A\nYT1Q\nJT1E\nYT3E\n",
        )
        # Five substring hits keep the existing broad-fallback gate closed.
        rank = self.namespace["match_group"]
        self.assertEqual(rank("YT1E", "YT1ZZ"), 5)
        prefix = self.namespace["callsign_prefix"]
        self.assertEqual([prefix(call) for call in ("YT1E", "PA3ABC", "2E0ABC")],
                         ["YT1", "PA3", "2E0"])

    def test_best_matches_survive_substring_limit(self):
        self.rows = [(f"A{i:02}YT1E",) for i in range(45)] + [("YT1E",), ("YT1ET",)]
        self.search("YT1E")
        lines = self.app.text_display.get("1.0", "end-1c").splitlines()
        self.assertEqual(lines[:2], ["YT1E", "YT1ET"])
        self.assertEqual(len(lines), 40)
        self.assertEqual(self.app.ghost_call, "")

    def test_broad_fallback_uses_full_prefix(self):
        self.rows = [("YT1ZZ",), ("YT1AA",), ("YT2ZZ",)]
        self.search("YT1E")
        self.assertEqual(self.app.text_display.get("1.0", "end-1c"), "YT1AA\nYT1ZZ\n")

    def test_first_character_substitution_without_shared_prefix(self):
        self.rows = [("JT1E",), ("AB9Z",)]
        self.search("YT1E")
        self.assertEqual(self.app.text_display.get("1.0", "end-1c"), "JT1E\n")
        self.assertIn("red", self.app.text_display.tag_names("1.0"))
        self.assertEqual(self.app.ghost_call, "")

    def test_substring_alignment_marks_only_added_characters(self):
        self.rows = [("XYT1EZ",)]
        self.search("YT1E")
        self.assertIn("green", self.app.text_display.tag_names("1.0"))
        self.assertIn("normal", self.app.text_display.tag_names("1.1"))
        self.assertIn("green", self.app.text_display.tag_names("1.5"))
        self.assertFalse(self.app.text_display.tag_ranges("red"))

    def test_input_errors_are_not_combined_across_different_candidates(self):
        self.rows = [("JT1E",), ("YT3E",)]
        self.search("YT1E")
        self.assertTrue(self.app.entry_box.tag_ranges("error"))

    def test_arrow_navigation_selects_scrolls_and_enter_copies(self):
        self.search("YT1")
        original = self.app.entry_box.get("1.0", "end-1c")
        with patch.object(self.app.text_display, "see") as see:
            self.assertEqual(self.app.move_suggestion(1), "break")
            self.assertEqual(self.app.selected_suggestion, 0)
            self.assertEqual(self.app.entry_box.get("1.0", "end-1c"), "YT1A")
            self.app.move_suggestion(1)
            self.assertEqual(self.app.selected_suggestion, 1)
            self.assertEqual(self.app.entry_box.get("1.0", "end-1c"), "YT1AD")
            self.clipboard_append.assert_not_called()
            see.assert_called_with("2.0")
            self.assertEqual([str(i) for i in self.app.text_display.tag_ranges("selected")],
                             ["2.0", "2.5"])
            self.app.move_suggestion(-1)
            self.app.move_suggestion(-1)
            self.assertIsNone(self.app.selected_suggestion)
            self.assertEqual(self.app.entry_box.get("1.0", "end-1c"), original)
            self.assertEqual(self.app.text_display.tag_ranges("selected"), ())
            self.assertEqual(self.app.current_typed, "")
            self.app.move_suggestion(1)
            self.app.accept_ghost()
            self.clipboard_append.assert_called_with("YT1A")
        self.assertIsNone(self.app.selected_suggestion)
        self.assertTrue(self.app.entry_box.bind("<Down>"))
        self.assertTrue(self.app.entry_box.bind("<Up>"))
        self.assertEqual(self.app.text_display.tag_cget("selected", "background"), "")
        self.assertEqual(int(self.app.text_display.tag_cget("selected", "underline")), 1)

    def test_escape_restores_original_cursor_errors_and_keeps_results(self):
        self.rows = [("DL1DL",), ("DL1DLA",)]
        self.search("PA/DL1DX/P")
        self.app.entry_box.mark_set(tk.INSERT, "1.5")
        original_results = self.app.text_display.get("1.0", "end-1c")
        original_errors = self.app.entry_box.tag_ranges("error")
        self.app.move_suggestion(1)
        self.assertEqual(self.app.entry_box.get("1.0", "end-1c"), "PA/DL1DL/P")
        self.assertEqual(self.app.cancel_suggestion(), "break")
        self.app.on_key_release(SimpleNamespace(keysym="Escape"))
        self.assertEqual(self.app.entry_box.get("1.0", "end-1c"), "PA/DL1DX/P")
        self.assertEqual(self.app.entry_box.index(tk.INSERT), "1.5")
        self.assertEqual(self.app.entry_box.tag_ranges("error"), original_errors)
        self.assertEqual(self.app.text_display.get("1.0", "end-1c"), original_results)
        self.assertIsNone(self.app.search_after_id)
        self.clipboard_append.assert_not_called()
        self.assertTrue(self.app.entry_box.bind("<Escape>"))

    def test_arrow_portable_selection_preserves_prefix_suffix(self):
        self.rows = [("DO1ABC",), ("DO1DEF",)]
        for typed in ("PA/DO1", "PA/DO1/P", "DO1/M"):
            with self.subTest(typed=typed):
                self.search(typed)
                self.app.move_suggestion(-1)
                self.app.move_suggestion(1)
                self.assertEqual(self.app.selected_suggestion, 1)
                self.assertEqual(self.app.entry_box.get("1.0", "end-1c"),
                                 typed.replace("DO1", "DO1DEF"))
                self.app.accept_ghost()
                self.clipboard_append.assert_called_with(typed.replace("DO1", "DO1DEF"))

    def test_arrow_selection_reset_on_edit_results_and_scan_report(self):
        self.search("YT1")
        self.app.move_suggestion(1)
        self.app.entry_box.insert("end-1c", "Z")
        self.app.on_key_release(SimpleNamespace(keysym="Z"))
        self.assertIsNone(self.app.selected_suggestion)
        self.assertEqual(self.app.text_display.tag_ranges("selected"), ())
        self.app.move_suggestion(1)
        self.assertIsNone(self.app.selected_suggestion)
        self.root.after_cancel(self.app.search_after_id)
        self.app.search_after_id = None
        self.search("YT1")
        self.app.move_suggestion(1)
        self.search("ZZ9")
        self.app.move_suggestion(1)
        self.assertIsNone(self.app.selected_suggestion)
        self.search("YT1")
        self.app.move_suggestion(1)
        self.app.show_scan_results("Scan results\n")
        self.app.move_suggestion(1)
        self.assertEqual(self.app.selected_suggestion, 1)

    def test_question_wildcards_match_exact_shape_without_fuzzy_fallback(self):
        self.rows = [(call,) for call in (
            "PA1FAQ", "PA3FBQ", "PA1FAAQ", "PA1FAX", "XP AFAQ", "DL1AB", "DL1ABC"
        )]
        self.search("PA?F?Q")
        self.assertEqual(self.app.text_display.get("1.0", "end-1c"),
                         "PA1FAQ\nPA3FBQ\n")
        self.assertEqual(self.app.entry_box.get("1.0", "end-1c"), "PA?F?Q")
        self.assertEqual(self.app.entry_box.tag_ranges("error"), ())
        self.assertEqual(self.app.ghost_call, "")
        for column in (2, 4):
            self.assertIn("green", self.app.text_display.tag_names(f"1.{column}"))
        self.app.move_suggestion(1)
        self.assertEqual(self.app.entry_box.get("1.0", "end-1c"), "PA1FAQ")
        self.app.accept_ghost()
        self.clipboard_append.assert_called_with("PA1FAQ")
        self.search("DL1??")
        self.assertEqual(self.app.text_display.get("1.0", "end-1c"), "DL1AB\n")
        self.search("ZZ?ZZ")
        self.assertEqual(self.app.status.cget("text"), "No matches in database.")

    def test_portable_wildcard_selection_preserves_modifiers_and_import_strictness(self):
        self.rows = [("DL1ABC",), ("DL2ABC",)]
        for typed in ("PA/DL1?BC/P", "PA/DL?ABC/P"):
            with self.subTest(typed=typed):
                self.search(typed)
                self.assertIn("DL1ABC", self.app.text_display.get("1.0", "end-1c"))
                self.assertEqual(self.app.entry_box.tag_ranges("error"), ())
                self.app.move_suggestion(1)
                self.assertEqual(self.app.entry_box.get("1.0", "end-1c"), "PA/DL1ABC/P")
                self.app.accept_ghost()
                self.clipboard_append.assert_called_with("PA/DL1ABC/P")
                self.assertIsNone(self.namespace["base_call_span"](typed))

    def test_portable_search_uses_base_preserves_input_and_clipboard(self):
        self.rows = [("DL1DL",), ("DL1DLA",)]
        for typed in ("PA/DL1DL/P", "DL1DL/M", "DL1DL/PA", "DL1DL/7"):
            with self.subTest(typed=typed):
                self.search(typed)
                self.assertEqual(self.app.entry_box.get("1.0", "end-1c"), typed)
                self.assertEqual(self.app.entry_box.tag_ranges("error"), ())
                self.assertIn("exact", self.app.text_display.tag_names("1.0"))
                self.assertEqual(self.app.ghost_call, "")
                self.app.accept_ghost()
                self.clipboard_append.assert_called_with(typed)

    def test_portable_ghost_completes_before_suffix_without_editing_input(self):
        self.rows = [("DL1DL",)]
        self.search("PA/DL1D")
        self.assertEqual(self.app.ghost_call, "PA/DL1DL")
        self.app.accept_ghost()
        self.clipboard_append.assert_called_with("PA/DL1DL")
        self.search("PA/DL1D/P")
        self.assertEqual(self.app.ghost_call, "PA/DL1DL/P")
        self.assertEqual(self.app.entry_box.get("1.0", "end-1c"), "PA/DL1D/P")
        with patch.object(self.app.entry_box, "bbox", return_value=(30, 2, 10, 20)):
            self.app.refresh_ghost()
        self.assertEqual(self.app.ghost_label.cget("text"), "L/P")
        self.assertEqual(self.app.ghost_suffix.cget("text"), "/P")
        self.assertTrue(self.app.ghost_suffix.place_info())
        self.app.accept_ghost()
        self.clipboard_append.assert_called_with("PA/DL1DL/P")

    def test_alphabetical_full_ghost_at_base_or_input_end(self):
        self.rows = [("DL1A",), ("DL100ANA",), ("DL100DM",)]
        typed = "PA/DL1/P"
        for position in ("1.6", "1.8"):
            with self.subTest(position=position):
                self.search(typed)
                self.app.entry_box.mark_set(tk.INSERT, position)
                with patch.object(self.app.entry_box, "bbox", return_value=(50, 2, 10, 20)):
                    self.app.refresh_ghost()
                self.assertEqual(self.app.ghost_call, "PA/DL100ANA/P")
                self.assertEqual(self.app.ghost_label.cget("text"), "00ANA/P")
                self.assertTrue(self.app.ghost_label.place_info())
                self.assertEqual(self.app.ghost_suffix.cget("text"), "/P")
                self.assertEqual(self.app.entry_box.get("1.0", "end-1c"), typed)
                self.assertEqual(self.app.text_display.get("1.0", "1.end"), "DL100ANA")
                self.app.accept_ghost()
                self.clipboard_append.assert_called_with("PA/DL100ANA/P")
        for position in ("1.4", "1.7"):
            with self.subTest(hidden_position=position):
                self.search(typed)
                self.app.entry_box.mark_set(tk.INSERT, position)
                self.app.refresh_ghost()
                self.assertFalse(self.app.ghost_label.place_info())
                with patch.object(self.app, "search_and_highlight"):
                    self.app.accept_ghost()
                self.clipboard_append.assert_called_with(typed)

    def test_selected_preview_copies_result_colors_only_to_base(self):
        self.rows = [("DL1AAH",), ("DL1AAO",), ("DL1AG",)]
        for typed in ("PA/DL1AA/P", "PA/DL1AX/P"):
            with self.subTest(typed=typed):
                self.search(typed)
                self.app.move_suggestion(1)
                preview = self.app.entry_box.get("1.0", "end-1c")
                base = self.app.text_display.get("1.0", "1.end")
                self.assertEqual(preview, "PA/" + base + "/P")
                for index in range(len(base)):
                    for tag in ("red", "green"):
                        self.assertEqual(
                            tag in self.app.entry_box.tag_names(f"1.{3 + index}"),
                            tag in self.app.text_display.tag_names(f"1.{index}"),
                        )
                for index in (0, 1, 2, len(preview) - 2, len(preview) - 1):
                    self.assertNotIn("green", self.app.entry_box.tag_names(f"1.{index}"))
                    self.assertNotIn("red", self.app.entry_box.tag_names(f"1.{index}"))
                self.app.cancel_suggestion()
                self.assertEqual(self.app.entry_box.tag_ranges("green"), ())
                self.assertEqual(self.app.entry_box.tag_ranges("red"), ())

    def test_portable_errors_only_mark_base_segment(self):
        self.rows = [("DL1DL",)]
        self.search("PA/DL1DX/P")
        self.assertIn("error", self.app.entry_box.tag_names("1.7"))
        self.assertNotIn("error", self.app.entry_box.tag_names("1.0"))
        self.assertNotIn("error", self.app.entry_box.tag_names("1.9"))
        self.rows = []
        self.search("PA/DL1DL/P")
        self.assertEqual([str(index) for index in self.app.entry_box.tag_ranges("error")],
                         ["1.3", "1.8"])

    def test_partial_portable_base_search_and_completion(self):
        self.rows = [("DO1ABC",), ("DO1DEF",)]
        self.search("DO1")
        expected = self.app.text_display.get("1.0", "end-1c")
        for typed in ("PA/DO1", "PA/DO1/P", "DO1/M"):
            with self.subTest(typed=typed):
                self.search(typed)
                self.assertEqual(self.app.text_display.get("1.0", "end-1c"), expected)
                self.assertEqual(self.app.entry_box.get("1.0", "end-1c"), typed)
                self.assertEqual(self.app.ghost_call,
                                typed.replace("DO1", "DO1ABC"))
                self.app.accept_ghost()
                self.clipboard_append.assert_called_with(
                    typed.replace("DO1", "DO1ABC"))
        self.search("PA/DL1")
        self.assertNotIn("incomplete or ambiguous", self.app.status.cget("text"))
        parser = self.namespace["base_call_span"]
        self.assertIsNone(parser("PA/DO1"))
        self.assertIsNone(parser("PA/DO1/P"))

    def test_ambiguous_or_incomplete_portable_never_queries_or_rewrites(self):
        self.app.update_entry_display("DL1DX", "1.5", has_errors=True,
                                      error_mask=[True] * 5)
        for typed in ("DL1DL/PA1ABC", "PA//DL1DL", "PA/DL1DL/UNKNOWN",
                      "PA/", "PA/DO", "KH6/DO1"):
            with self.subTest(typed=typed), \
                    patch("sqlite3.connect", side_effect=AssertionError("Unexpected search")):
                self.app.update_entry_display(typed, f"1.{len(typed)}")
                self.app.search_and_highlight(typed, f"1.{len(typed)}")
                self.assertEqual(self.app.ghost_call, "")
                self.assertEqual(self.app.entry_box.tag_ranges("error"), ())
                self.assertEqual(self.app.entry_box.get("1.0", "end-1c"), typed)
                self.assertIn("incomplete or ambiguous", self.app.status.cget("text"))
                self.app.accept_ghost()
                self.clipboard_append.assert_called_with(typed)


if __name__ == "__main__":
    unittest.main(verbosity=2)
