import sqlite3
import json
import queue
import subprocess
import sys
import threading
import tkinter as tk
import ctypes
from ctypes import wintypes
from functools import cache
from pathlib import Path
from tkinter import font, messagebox, ttk
from Tools.callsign_rules import base_call_span

VERSION = "0.1"
APP_DIR = Path(sys.executable).resolve().parent if getattr(sys, "frozen", False) else Path(__file__).resolve().parent
DB_PATH = APP_DIR / "CALL_CHECKER.DB"
REPORT_POSITION_PATH = APP_DIR / "report_window_position.json"


def valid_report_position(x, y):
    if type(x) is not int or type(y) is not int or x < -10000 or y < -10000:
        return False
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    user32.MonitorFromRect.argtypes = [ctypes.POINTER(wintypes.RECT), wintypes.DWORD]
    user32.MonitorFromRect.restype = wintypes.HANDLE
    title = wintypes.RECT(x, y, x + 100, y + 30)
    return bool(user32.MonitorFromRect(ctypes.byref(title), 0))


def format_import_output(contents):
    lines = []
    for line in contents.splitlines():
        if line.startswith("Import progress: Reading database ") and lines and lines[-1]:
            lines.append("")
        lines.append(line)
    return "\n".join(lines)


class SourceReportWindow:
    def __init__(self, owner):
        self.owner = owner
        self.window = tk.Toplevel(owner)
        self.window.withdraw()
        self.window.transient(owner)
        self.window.configure(background="#505050")
        self.window.minsize(450, 250)
        x, y = owner.winfo_rootx() + 30, owner.winfo_rooty() + 30
        if not valid_report_position(x, y):
            x, y = 50, 50
        try:
            with REPORT_POSITION_PATH.open(encoding="utf-8") as saved:
                position = json.load(saved)
            if (not isinstance(position, dict)
                    or not valid_report_position(position.get("x"), position.get("y"))):
                raise ValueError("Saved report position is invalid or off-screen.")
            x, y = position["x"], position["y"]
        except FileNotFoundError:
            pass
        except (OSError, ValueError) as error:
            messagebox.showwarning("Report position", f"Cannot restore report position:\n{error}",
                                   parent=owner)
        self.window.geometry(f"860x540+{x}+{y}")
        buttons = ttk.Frame(self.window, style="HSC.TFrame")
        buttons.pack(side="bottom", fill="x", padx=10, pady=8)
        style = ttk.Style(owner)
        style.configure("HSC.TButton", background="#505050", foreground="#FFFFFF")
        style.map("HSC.TButton", background=[("active", "#606060")])
        ttk.Button(buttons, text="Close", style="HSC.TButton",
                   command=self.close).pack(side="right")
        frame = ttk.Frame(self.window, style="HSC.TFrame")
        frame.pack(fill="both", expand=True, padx=10, pady=(10, 0))
        self.text = tk.Text(
            frame, font=("Cascadia Mono", 11), background="#4A4A4A",
            foreground="#FFFFFF", insertbackground="#FFFFFF",
            selectbackground="#606060", selectforeground="#FFFFFF",
            highlightbackground="#444444", wrap="none", state="disabled",
        )
        self.vertical = ttk.Scrollbar(
            frame, orient="vertical", style="HSC.Vertical.TScrollbar", command=self.text.yview,
        )
        self.horizontal = ttk.Scrollbar(frame, orient="horizontal", command=self.text.xview)
        self.text.configure(yscrollcommand=self.vertical.set, xscrollcommand=self.horizontal.set)
        self.text.tag_config("filtered", font=("Cascadia Mono", 11, "bold"))
        frame.columnconfigure(0, weight=1)
        frame.rowconfigure(0, weight=1)
        self.text.grid(row=0, column=0, sticky="nsew")
        self.vertical.grid(row=0, column=1, sticky="ns")
        self.horizontal.grid(row=1, column=0, sticky="ew")
        self.window.protocol("WM_DELETE_WINDOW", self.close)
        self.window.bind("<Escape>", lambda event: self.close())

    def show(self, title, contents):
        self.window.title(title)
        self.text.configure(state="normal")
        self.text.delete("1.0", tk.END)
        self.text.insert("1.0", contents)
        for number, line in enumerate(contents.splitlines(), 1):
            if line.startswith("[FILTERED"):
                self.text.tag_add("filtered", f"{number}.1", f"{number}.9")
        self.text.configure(state="disabled")
        self.text.xview_moveto(0)
        self.text.yview_moveto(0)
        self.window.deiconify()
        self.window.lift()
        self.text.focus_set()

    def close(self):
        if self.window.state() != "iconic":
            x, y = self.window.winfo_x(), self.window.winfo_y()
            if valid_report_position(x, y):
                try:
                    with REPORT_POSITION_PATH.open("w", encoding="utf-8") as saved:
                        json.dump({"x": x, "y": y}, saved)
                        saved.write("\n")
                except OSError as error:
                    messagebox.showerror(
                        "Report position", f"Cannot save report position:\n{error}",
                        parent=self.window,
                    )
                    return False
        self.window.destroy()
        return True


@cache
def load_callsign_fonts():
    paths = [
        APP_DIR / "Fonts" / f"CascadiaMono-{style}.ttf"
        for style in ("Regular", "Bold", "Italic", "BoldItalic")
    ]
    for path in paths:
        if not path.is_file():
            raise FileNotFoundError(f"Bundled callsign font is missing: {path}")
    gdi = ctypes.WinDLL("gdi32", use_last_error=True)
    gdi.AddFontResourceExW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.LPVOID]
    gdi.AddFontResourceExW.restype = ctypes.c_int
    gdi.RemoveFontResourceExW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.LPVOID]
    gdi.RemoveFontResourceExW.restype = wintypes.BOOL
    loaded = []
    for path in paths:
        if not gdi.AddFontResourceExW(str(path), 0x10, None):  # FR_PRIVATE: this process only.
            for previous in loaded:
                gdi.RemoveFontResourceExW(str(previous), 0x10, None)
            raise OSError(f"Cannot load bundled callsign font: {path}")
        loaded.append(path)


def callsign_prefix(call):
    has_letter = False
    for index, char in enumerate(call):
        if char in "ABCDEFGHIJKLMNOPQRSTUVWXYZ":
            has_letter = True
        elif char in "0123456789" and has_letter:
            return call[:index + 1]
    return ""


def match_group(typed, call):
    if call == typed:
        return 0
    if call.startswith(typed):
        return 1
    if typed in call:
        return 2
    if len(call) == len(typed) and sum(a != b for a, b in zip(typed, call)) == 1:
        prefix = callsign_prefix(typed)
        return 3 if prefix and callsign_prefix(call) == prefix else 4
    return 5


def source_command(arguments):
    if getattr(sys, "frozen", False):
        command = [str(APP_DIR / "CALL_Checker_Sources.exe")]
    else:
        command = [sys.executable, "-B", "-X", "utf8",
                   str(APP_DIR / "Tools" / "CALL_Checker_Sources.py")]
    return [*command, *arguments, "--target", str(DB_PATH)]


class CALLCheckWindow:
    def __init__(self, root):
        load_callsign_fonts()
        self.root = root
        self.root.title(f"Standalone Callsign Check Window v{VERSION} (N1MM+ Style)")
        self.root.geometry("450x550")
        self.root.attributes("-topmost", True)  # Altijd op de voorgrond net als N1MM
        background = "#505050"
        panel = "#4A4A4A"
        border = "#444444"
        foreground = "#FFFFFF"
        error_color = "#FF5050"
        extra_color = "#66EE33"
        self.root.configure(background=background)
        
        self.style = ttk.Style(root)
        self.style.theme_use("clam")
        self.style.configure("HSC.TFrame", background=background)
        self.style.configure(
            "HSC.TLabel", background=background, foreground=foreground, font=("Segoe UI", 10)
        )
        self.style.configure(
            "HSC.Status.TLabel",
            background="#404040",
            foreground="#FFD000",
            font=("Segoe UI", 10),
        )
        self.style.configure(
            "HSC.Vertical.TScrollbar",
            background=border,
            troughcolor=panel,
            arrowcolor=foreground,
            bordercolor=background,
            lightcolor=border,
            darkcolor=border,
        )
        self.style.map(
            "HSC.Vertical.TScrollbar",
            background=[("pressed", "#404040"), ("active", "#606060")],
        )

        # Bovenpaneel voor invoer en foutindicatie
        self.input_frame = ttk.Frame(root, style="HSC.TFrame")
        self.input_frame.pack(pady=10, fill="x", padx=20)

        self.label = ttk.Label(self.input_frame, text="Callsign:", style="HSC.TLabel", font=("Segoe UI", 11, "bold"))
        self.label.pack(anchor="w")

        # Gebruik een Text widget voor het invoerveld om specifieke letters ROOD te kunnen maken
        self.entry_box = tk.Text(
            self.input_frame, font=("Cascadia Mono", 16, "bold"), height=1, width=20,
            background=panel, foreground=foreground, insertbackground=foreground,
            selectbackground="#606060", selectforeground=foreground,
            highlightbackground=border, highlightcolor="#FFD000",
        )
        self.entry_box.pack(pady=5, fill="x")
        self.entry_box.tag_config("error", foreground="#FFFFFF", background=error_color)
        self.entry_box.tag_config("green", foreground="#000000", background=extra_color)
        self.entry_box.tag_config("red", foreground="#FFFFFF", background=error_color)
        self.ghost_label = tk.Label(
            self.entry_box, font=("Cascadia Mono", 16, "bold"),
            background=extra_color, foreground="#000000",
            borderwidth=0, padx=0, pady=0, takefocus=False,
        )
        self.ghost_call = ""
        self.ghost_suffix = tk.Label(
            self.ghost_label, font=("Cascadia Mono", 16, "bold"),
            background=panel, foreground=foreground, borderwidth=0, padx=0, pady=0,
            takefocus=False,
        )
        self.entry_box.bind("<KeyRelease>", self.on_key_release)
        self.entry_box.bind("<Return>", self.accept_ghost)
        self.entry_box.bind("<KP_Enter>", self.accept_ghost)
        self.entry_box.bind("<Down>", lambda event: self.move_suggestion(1))
        self.entry_box.bind("<Up>", lambda event: self.move_suggestion(-1))
        self.entry_box.bind("<Escape>", self.cancel_suggestion)
        self.entry_box.bind("<ButtonRelease-1>", self.refresh_ghost)
        self.entry_box.bind("<Configure>", self.refresh_ghost)
        self.entry_box.focus_set()

        # Resultatenvenster (Text widget ipv Listbox voor kleurondersteuning per letter)
        self.results_label = ttk.Label(root, text="Suggestions (red = mismatch; green = extra characters):", style="HSC.TLabel", font=("Segoe UI", 10, "bold"), wraplength=410)
        self.results_label.pack(anchor="w", padx=20)

        self.frame = ttk.Frame(root, style="HSC.TFrame")
        self.frame.pack(pady=5, fill="both", expand=True, padx=20)
        
        self.scrollbar = ttk.Scrollbar(self.frame, orient="vertical", style="HSC.Vertical.TScrollbar")
        self.horizontal_scrollbar = ttk.Scrollbar(self.frame, orient="horizontal")
        self.text_display = tk.Text(
            self.frame, 
            font=("Cascadia Mono", 13),
            yscrollcommand=self.scrollbar.set,
            xscrollcommand=self.horizontal_scrollbar.set,
            background=panel,
            foreground=foreground,
            insertbackground=foreground,
            selectbackground="#606060",
            selectforeground=foreground,
            highlightbackground=border,
            highlightcolor=border,
            state="disabled",
            wrap="none"
        )
        self.scrollbar.config(command=self.text_display.yview)
        self.horizontal_scrollbar.config(command=self.text_display.xview)
        
        # Kleurtags configureren voor het resultatenvenster
        self.text_display.tag_config("red", foreground="#FFFFFF", background=error_color)
        self.text_display.tag_config("green", foreground="#000000", background=extra_color)
        self.text_display.tag_config("normal", foreground=foreground)
        self.text_display.tag_config("exact", font=("Cascadia Mono", 13, "bold", "italic"))
        self.text_display.tag_config("filtered", font=("Cascadia Mono", 13, "bold"))
        self.text_display.tag_config("selected", underline=True)
        self.suggestions = []
        self.suggestion_input = ""
        self.selected_suggestion = None
        self.suggestion_cursor = "1.0"
        self.suggestion_error_mask = []
        
        self.frame.columnconfigure(0, weight=1)
        self.frame.rowconfigure(0, weight=1)
        self.text_display.grid(row=0, column=0, sticky="nsew")
        self.scrollbar.grid(row=0, column=1, sticky="ns")
        self.horizontal_scrollbar.grid(row=1, column=0, sticky="ew")

        # Statusbalk
        self.status = ttk.Label(root, text="Type at least 3 characters to check...", style="HSC.Status.TLabel", borderwidth=1, relief="sunken", anchor="w", wraplength=430)
        self.status.pack(side="bottom", fill="x", before=self.frame)

        # Interne variabele om de 'schone' getypte tekst bij te houden
        self.current_typed = ""
        self.search_after_id = None
        self.source_task = None
        self.source_progress = ""
        self.source_result = queue.Queue()
        self.report_dialog = None
        self.menu_bar = tk.Menu(root)
        self.sources_menu = tk.Menu(self.menu_bar, tearoff=False)
        self.sources_menu.add_command(label="Scan sources", command=self.scan_sources)
        self.sources_menu.add_command(label="Import callsigns", command=self.import_sources)
        self.menu_bar.add_cascade(label="Sources", menu=self.sources_menu)
        self.root.configure(menu=self.menu_bar)
        self.root.protocol("WM_DELETE_WINDOW", self.close_window)

    def scan_sources(self):
        self.start_source_task("Scan", ["--scan-listed", "--update-list", "--scan-result"])

    def import_sources(self):
        if self.source_task is not None:
            return
        tools = APP_DIR / "Tools"
        if messagebox.askyesno(
            "Import callsigns",
            f"Import callsigns from the databases in:\n{tools / 'source_list.txt'}\n\n"
            f"Add missing calls to:\n{DB_PATH}\n\n"
            "Sources remain read-only. Review the source list before continuing.\n"
            "Each completed database is saved separately; earlier saves remain if a later one fails.\n"
            "No backup is created. Continue?",
            parent=self.root,
        ):
            self.start_source_task("Import", ["--import"])

    def start_source_task(self, action, arguments):
        if self.source_task is not None:
            return
        command = source_command(arguments)

        def run():
            lines = []
            try:
                with subprocess.Popen(
                    command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                    text=True, encoding="utf-8", errors="replace",
                    creationflags=subprocess.CREATE_NO_WINDOW,
                ) as process:
                    if process.stdout is None:
                        raise RuntimeError("Source tool output pipe is unavailable.")
                    for line in process.stdout:
                        lines.append(line)
                        if line.startswith("Import progress: "):
                            self.source_result.put((None, line.strip(), ""))
                    code = process.wait()
                self.source_result.put((code, "".join(lines), ""))
            except (OSError, UnicodeError, RuntimeError) as error:
                self.source_result.put((1, "".join(lines), str(error)))

        self.source_task = action
        self.source_progress = f"{action} running..."
        self.sources_menu.entryconfigure(0, state="disabled")
        self.sources_menu.entryconfigure(1, state="disabled")
        self.status.configure(text=self.source_progress)
        threading.Thread(target=run).start()
        self.root.after(100, self.check_source_task)

    def check_source_task(self):
        try:
            code, output, error = self.source_result.get_nowait()
        except queue.Empty:
            self.root.after(100, self.check_source_task)
            return
        action = self.source_task
        if code is None:
            self.source_progress = f"Import running: {output.removeprefix('Import progress: ')}"
            self.status.configure(text=self.source_progress)
            self.root.after(100, self.check_source_task)
            return
        self.source_task = None
        self.source_progress = ""
        self.sources_menu.entryconfigure(0, state="normal")
        self.sources_menu.entryconfigure(1, state="normal")
        details = (output + "\n" + error).strip()
        if action == "Import":
            typed = self.entry_box.get("1.0", "end-1c")
            if len(typed) >= 3:
                self.search_and_highlight(typed, self.entry_box.index(tk.INSERT))
        if code:
            self.status.configure(text=f"{action} failed. See error dialog.")
            self.show_source_report(f"{action} failed",
                                    format_import_output(details) or f"Exit code: {code}")
            return
        if action == "Scan":
            report = APP_DIR / "Tools" / "scan_result.txt"
            try:
                contents = report.read_text(encoding="utf-8")
            except OSError as error:
                self.status.configure(text="Scan completed; report could not be displayed.")
                messagebox.showerror(
                    "Scan report unavailable",
                    f"Scan completed, but reading the report failed:\n{report}\n{error}",
                    parent=self.root,
                )
                return
            self.show_scan_results(contents)
            return
        self.status.configure(text=f"{action} completed.")
        tools = APP_DIR / "Tools"
        report = tools / ("scan_result.txt" if action == "Scan" else "import_result.txt")
        self.show_source_report(f"{action} completed",
                                f"{format_import_output(details)}\n\nReport: {report}")

    def show_scan_results(self, contents):
        self.show_source_report("Scan completed (FILTERED = excluded)", contents)
        self.status.configure(text="Scan completed. See report window.")

    def show_source_report(self, title, contents):
        if self.report_dialog is None or not self.report_dialog.window.winfo_exists():
            self.report_dialog = SourceReportWindow(self.root)
        self.report_dialog.show(title, contents)

    def close_window(self):
        if self.source_task is not None:
            messagebox.showwarning("Action running", "Wait for the source action to finish.",
                                   parent=self.root)
            return
        if (self.report_dialog is not None and self.report_dialog.window.winfo_exists()
                and not self.report_dialog.close()):
            return
        self.root.destroy()

    def on_key_release(self, event):
        # Negeer navigatietoetsen (zoals pijltjestoetsen)
        if event.keysym in ["Left", "Right", "Up", "Down", "Shift_L", "Shift_R", "Control_L", "Control_R"]:
            self.refresh_ghost()
            return
        if event.keysym in ("Return", "KP_Enter", "Escape"):
            return
        self.selected_suggestion = None
        self.text_display.tag_remove("selected", "1.0", tk.END)
        self.ghost_call = ""
        self.ghost_label.place_forget()

        # Haal de tekst op uit de entry_box, zet om naar hoofdletters en strip witruimte
        raw_text = self.entry_box.get("1.0", "end-1c")
        cursor_pos = self.entry_box.index(tk.INSERT)
        
        # Filter vreemde tekens (backslashes/enters) en dwing hoofdletters af
        clean_text = "".join([c for c in raw_text if c.isalnum() or c in "?/*"]).upper()
        self.current_typed = clean_text

        # Als er minder dan 3 karakters zijn, reset alles naar zwart en wis resultaten
        if len(clean_text) < 3:
            if self.search_after_id is not None:
                self.root.after_cancel(self.search_after_id)
                self.search_after_id = None
            self.update_entry_display(clean_text, cursor_pos, has_errors=False)
            self.clear_results()
            self.status.config(text=self.source_progress or "Type at least 3 characters to check...")
            return

        if self.search_after_id is not None:
            self.root.after_cancel(self.search_after_id)
        self.search_after_id = self.root.after(150, self.run_scheduled_search, clean_text)

    def run_scheduled_search(self, typed):
        self.search_after_id = None
        self.search_and_highlight(typed, self.entry_box.index(tk.INSERT))

    def ghost_parts(self, typed):
        span = base_call_span(typed, allow_partial=True) if "/" in typed else (0, len(typed))
        if not typed or span is None:
            return None
        end = span[1]
        suffix = typed[end:]
        if (len(self.ghost_call) <= len(typed)
                or not self.ghost_call.startswith(typed[:end])
                or not self.ghost_call.endswith(suffix)):
            return None
        if (self.entry_box.compare(tk.INSERT, "!=", "end-1c")
                and self.entry_box.compare(tk.INSERT, "!=", f"1.{end}")):
            return None
        extra_end = len(self.ghost_call) - len(suffix)
        return end, self.ghost_call[end:extra_end], suffix

    def refresh_ghost(self, _event=None):
        self.ghost_label.place_forget()
        typed = self.entry_box.get("1.0", "end-1c")
        parts = self.ghost_parts(typed)
        if (
            parts is None
            or self.selected_suggestion is not None
        ):
            return
        end, extra, suffix = parts
        self.entry_box.update_idletasks()
        bounds = self.entry_box.bbox(f"1.{end}")
        if bounds is not None:
            x, y, _, height = bounds
            self.ghost_label.configure(text=extra + suffix)
            self.ghost_suffix.place_forget()
            if suffix:
                width = font.Font(root=self.root, font=self.ghost_label.cget("font")).measure(extra)
                self.ghost_suffix.configure(text=suffix)
                self.ghost_suffix.place(x=width, y=0, relheight=1)
            self.ghost_label.place(x=x, y=y, height=height)

    def move_suggestion(self, direction):
        typed = self.entry_box.get("1.0", "end-1c")
        expected = (self.suggestion_input if self.selected_suggestion is None
                    else self.suggestions[self.selected_suggestion])
        if not self.suggestions or typed != expected or self.search_after_id is not None:
            return "break"
        if self.selected_suggestion == 0 and direction < 0:
            return self.cancel_suggestion()
        if self.selected_suggestion is None:
            self.suggestion_cursor = self.entry_box.index(tk.INSERT)
            self.suggestion_error_mask = [
                "error" in self.entry_box.tag_names(f"1.{index}")
                for index in range(len(typed))
            ]
            self.selected_suggestion = 0 if direction > 0 else len(self.suggestions) - 1
        else:
            self.selected_suggestion = max(
                0, min(len(self.suggestions) - 1, self.selected_suggestion + direction)
            )
        line = self.selected_suggestion + 1
        self.text_display.tag_remove("selected", "1.0", tk.END)
        self.text_display.tag_add("selected", f"{line}.0", f"{line}.end")
        self.text_display.see(f"{line}.0")
        preview = self.suggestions[self.selected_suggestion]
        self.update_entry_display(preview, f"1.{len(preview)}")
        span = (base_call_span(self.suggestion_input, allow_partial=True, allow_wildcards=True)
                if "/" in self.suggestion_input else (0, len(self.suggestion_input)))
        base_start = span[0]
        for column in range(len(self.text_display.get(f"{line}.0", f"{line}.end"))):
            for tag in ("red", "green"):
                if tag in self.text_display.tag_names(f"{line}.{column}"):
                    self.entry_box.tag_add(tag, f"1.{base_start + column}",
                                           f"1.{base_start + column + 1}")
        self.ghost_label.place_forget()
        return "break"

    def cancel_suggestion(self, _event=None):
        if self.selected_suggestion is not None:
            self.selected_suggestion = None
            self.text_display.tag_remove("selected", "1.0", tk.END)
            self.update_entry_display(
                self.suggestion_input, self.suggestion_cursor,
                has_errors=True, error_mask=self.suggestion_error_mask,
            )
            self.entry_box.focus_set()
            self.refresh_ghost()
        return "break"

    def accept_ghost(self, _event=None):
        typed = self.entry_box.get("1.0", "end-1c")
        completed = typed
        if (
            self.selected_suggestion is not None
            and typed == self.suggestions[self.selected_suggestion]
            and self.search_after_id is None
        ):
            completed = self.suggestions[self.selected_suggestion]
        elif self.ghost_parts(typed) is not None:
            completed = self.ghost_call
        if completed:
            if self.search_after_id is not None:
                self.root.after_cancel(self.search_after_id)
                self.search_after_id = None
            self.current_typed = completed
            self.update_entry_display(completed, f"1.{len(completed)}")
            self.root.clipboard_clear()
            self.root.clipboard_append(completed)
            if len(completed) >= 3:
                self.search_and_highlight(completed, f"1.{len(completed)}")
        return "break"

    def update_entry_display(self, text, cursor_pos, has_errors=False, error_mask=None):
        """Hertekent het invoerveld zodat foute letters rood worden."""
        self.entry_box.delete("1.0", tk.END)
        self.entry_box.insert("1.0", text)
        
        if has_errors and error_mask:
            for i, is_error in enumerate(error_mask):
                if i < len(text):
                    if is_error:
                        self.entry_box.tag_add("error", f"1.{i}", f"1.{i+1}")
                    
        self.entry_box.mark_set(tk.INSERT, cursor_pos)

    def clear_results(self):
        self.suggestions = []
        self.suggestion_input = ""
        self.selected_suggestion = None
        self.results_label.configure(
            text="Suggestions (red = mismatch; green = extra characters):"
        )
        self.text_display.config(state="normal")
        self.text_display.delete("1.0", tk.END)
        self.text_display.config(state="disabled")
        self.text_display.xview_moveto(0)

    def search_and_highlight(self, typed, cursor_pos):
        original_typed = typed
        span = base_call_span(typed, allow_partial=True, allow_wildcards=True) if "/" in typed else (0, len(typed))
        if span is None:
            self.clear_results()
            self.ghost_call = ""
            self.ghost_label.place_forget()
            self.update_entry_display(original_typed, cursor_pos)
            self.status.configure(text=self.source_progress
                                  or "Slash call is incomplete or ambiguous; no base call selected.")
            return
        start, end = span
        typed = typed[start:end]
        all_db_calls = []
        errors = []

        conn = None
        try:
            conn = sqlite3.connect(f"{Path(DB_PATH).resolve().as_uri()}?mode=ro", uri=True)
            conn.create_function("match_rank", 1, lambda call: match_group(typed, call))
            cursor = conn.cursor()

            # Rank before limits so better matches cannot be displaced by alphabetic neighbors.
            wildcard = "?" in typed
            pattern = "".join("[A-Z0-9]" if char == "?" else
                              f"[{char}]" if char in "*[" else char for char in typed)
            cursor.execute(
                "SELECT callsign FROM callsigns WHERE callsign GLOB ? "
                "ORDER BY callsign COLLATE NOCASE, callsign LIMIT 100"
                if wildcard else
                "SELECT callsign FROM callsigns WHERE callsign LIKE ? ORDER BY match_rank(callsign), callsign COLLATE NOCASE, callsign LIMIT 40",
                (pattern if wildcard else f"%{typed}%",)
            )
            exact_substring_matches = cursor.fetchall()
            
            # Wildcard/Nabijheid zoeken als er weinig directe substring resultaten zijn
            if not wildcard and len(exact_substring_matches) < 5:
                prefix = callsign_prefix(typed) or typed[:2]
                cursor.execute(
                    "SELECT callsign FROM callsigns WHERE callsign LIKE ? ORDER BY match_rank(callsign), callsign COLLATE NOCASE, callsign LIMIT 100",
                    (f"{prefix}%",)
                )
                broad_matches = cursor.fetchall()
            else:
                broad_matches = []

            # SQLite GLOB's ? replaces exactly one character, including the first.
            escaped_chars = [f"[{char}]" if char in "?*" else char for char in typed]
            near_patterns = [
                "".join(escaped_chars[:index]) + "?" + "".join(escaped_chars[index + 1:])
                for index in range(len(typed)) if not wildcard
            ]
            near_matches = []
            if near_patterns:
                cursor.execute(
                    "SELECT callsign FROM callsigns WHERE length(callsign) = ? AND ("
                    + " OR ".join("callsign GLOB ?" for _ in near_patterns)
                    + ") ORDER BY match_rank(callsign), callsign COLLATE NOCASE, callsign LIMIT 100",
                    (len(typed), *near_patterns),
                )
                near_matches = cursor.fetchall()

            # Combineer en ontdubbel resultaten
            sqlite_matches = exact_substring_matches + [
                match for match in broad_matches if match not in exact_substring_matches
            ]
            all_db_calls.extend(sorted(
                set(sqlite_matches + near_matches),
                key=lambda row: (match_group(typed, row[0]), row[0].upper(), row[0]),
            ))
        except sqlite3.Error as e:
            errors.append(f"{DB_PATH}: {e}")
        finally:
            if conn is not None:
                conn.close()

        self.clear_results()
        self.text_display.config(state="normal")
        self.suggestion_input = original_typed
        self.suggestions = [
            original_typed[:start] + call + original_typed[end:]
            for (call,) in all_db_calls
        ]
        self.ghost_call = ""
        if (typed,) not in all_db_calls:
            self.ghost_call = next(
                (call for (call,) in all_db_calls if match_group(typed, call) == 1), ""
            )
        if "/" in original_typed:
            self.ghost_call = (original_typed[:start] + self.ghost_call + original_typed[end:]
                               if self.ghost_call else "")

        input_error_mask = [True] * len(typed)
        if all_db_calls:
            # Use one closest candidate; unrelated matches must not clear each other's errors.
            masks = [
                [False] * len(typed) if typed in call else [
                    index >= len(call) or (char != "?" and char != call[index])
                    for index, char in enumerate(typed)
                ]
                for (call,) in all_db_calls
            ]
            input_error_mask = min(masks, key=sum)

        if all_db_calls:
            for (db_call,) in all_db_calls:
                if db_call == typed:
                    self.text_display.insert(tk.END, db_call, ("normal", "exact"))
                elif typed in db_call:
                    match_start = db_call.index(typed)
                    for index, d_char in enumerate(db_call):
                        tag = "normal" if match_start <= index < match_start + len(typed) else "green"
                        self.text_display.insert(tk.END, d_char, tag)
                else:
                    for index, d_char in enumerate(db_call):
                        if index >= len(typed):
                            tag = "green"
                        elif typed[index] == "?":
                            tag = "green"
                        elif d_char == typed[index]:
                            tag = "normal"
                        else:
                            tag = "red"
                        self.text_display.insert(tk.END, d_char, tag)
                            
                self.text_display.insert(tk.END, "\n", "normal")

            if errors:
                status = f"{len(all_db_calls)} suggestions loaded; " + "; ".join(errors)
            else:
                status = f"{len(all_db_calls)} suggestions loaded."
            self.status.config(text=status)
                
            # Update het invoerveld links/boven met de rode foutindicaties
            self.update_entry_display(
                original_typed, cursor_pos, has_errors=True,
                error_mask=[False] * start + input_error_mask + [False] * (len(original_typed) - end),
            )
        else:
            if errors:
                self.status.config(text="; ".join(errors))
            else:
                self.status.config(text="No matches in database.")
            self.update_entry_display(
                original_typed, cursor_pos, has_errors=True,
                error_mask=[False] * start + [True] * len(typed)
                + [False] * (len(original_typed) - end),
            )

        self.text_display.config(state="disabled")
        self.refresh_ghost()
        if self.source_progress:
            self.status.configure(text=self.source_progress)

if __name__ == "__main__":
    root = tk.Tk()
    app = CALLCheckWindow(root)
    root.mainloop()
