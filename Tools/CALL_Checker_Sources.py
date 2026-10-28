"""List, inspect and import callsigns from read-only SQLite/Access sources."""

import argparse
from datetime import datetime
import json
import os
import re
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
from callsign_rules import base_call_span


TOOLS_DIR = (Path(sys.executable).resolve().parent / "Tools"
             if getattr(sys, "frozen", False) else Path(__file__).resolve().parent)
LIST_PATH = TOOLS_DIR / "source_list.txt"
FILTER_PATH = TOOLS_DIR / "source_filter.txt"
SCAN_LIST_PATH = TOOLS_DIR / "scan_list.txt"
TARGET_PATH = TOOLS_DIR.parent / "CALL_CHECKER.DB"
DATABASE_EXTENSIONS = {".db", ".sqlite", ".sqlite3", ".mdb", ".accdb"}
CALL_COLUMNS = {"call", "callsign"}


def read_list(path):
    entries = []
    for number, line in enumerate(path.read_text(encoding="utf-8-sig").splitlines(), 1):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = [part.strip() for part in line.split("|")]
        if len(parts) not in (1, 3) or not all(parts):
            raise ValueError(f"{path}, line {number}: expected path or path|table|column")
        source = Path(parts[0])
        if not source.is_absolute():
            source = path.resolve().parent / source
        entries.append((source.resolve(), tuple(parts[1:])))
    return entries


def update_list(path, discovered, target):
    entries = read_list(path) if path.exists() else []
    seen = {str(source).casefold() for source, _ in entries}
    seen.add(str(target.resolve()).casefold())
    additions = []
    for source in discovered:
        key = str(source.resolve()).casefold()
        if key not in seen:
            additions.append(str(source.resolve()))
            seen.add(key)
    if not additions:
        return 0
    existing = path.read_text(encoding="utf-8-sig") if path.exists() else ""
    content = existing.rstrip("\r\n") + "\n" if existing.rstrip("\r\n") else ""
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=path.parent, delete=False
        ) as output:
            temporary = Path(output.name)
            output.write(content + "\n".join(additions) + "\n")
        os.replace(temporary, path)
    finally:
        if temporary is not None and temporary.exists():
            temporary.unlink()
    return len(additions)


def discover(root, allowed_roots=None):
    root = Path(root)
    if not root.is_dir():
        raise FileNotFoundError(f"Source folder does not exist: {root}")
    files = []
    for directory, children, names in os.walk(root, onerror=raise_walk_error):
        if allowed_roots is not None:
            for path in [Path(directory), *(Path(directory) / name for name in children),
                         *(Path(directory) / name for name in names)]:
                if not any(path.resolve().is_relative_to(allowed) for allowed in allowed_roots):
                    raise ValueError(f"Scan path resolves outside allowed roots: {path}")
        files.extend(
            Path(directory) / name for name in names
            if Path(name).suffix.lower() in DATABASE_EXTENSIONS
        )
    return sorted(files, key=lambda path: str(path).casefold())


def scan_locations(list_path, requested=None):
    roots = []
    for number, line in enumerate(list_path.read_text(encoding="utf-8-sig").splitlines(), 1):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if any(char in line for char in "*?|"):
            raise ValueError(f"{list_path}, line {number}: expected a directory, not a pattern")
        root = Path(line)
        if not root.is_absolute():
            root = list_path.resolve().parent / root
        root = root.resolve()
        if not root.is_dir():
            raise FileNotFoundError(f"Allowed scan folder does not exist: {root}")
        if root not in roots:
            roots.append(root)
    if not roots:
        raise ValueError(f"Allowed scan list is empty: {list_path}")
    if requested is not None:
        selected = requested.resolve()
        if not any(selected.is_relative_to(root) for root in roots):
            raise ValueError(f"Scan folder is outside allowed scan_list roots: {selected}")
        selected_roots = [selected]
    else:
        selected_roots = [
            root for root in roots
            if not any(root != other and root.is_relative_to(other) for other in roots)
        ]
    paths = {
        path.resolve() for root in selected_roots for path in discover(root, roots)
    }
    return selected_roots, sorted(paths, key=lambda path: str(path).casefold())


def raise_walk_error(error):
    raise error


def filter_sources(paths, filter_path):
    rules = []
    marker = "(only newest not filtered)"
    for number, line in enumerate(
        filter_path.read_text(encoding="utf-8-sig").splitlines(), 1
    ):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        newest = line.casefold().endswith(marker)
        pattern = line[:-len(marker)].rstrip() if newest else line
        if not pattern or ("(" in pattern and "only newest" in pattern.casefold()):
            raise ValueError(f"{filter_path}, line {number}: invalid filter rule")
        if newest and (pattern.count("*") != 1 or not Path(pattern).stem.endswith("*")):
            raise ValueError(f"{filter_path}, line {number}: newest rule must end in * before extension")
        rules.append((pattern, newest))

    def matches(path, pattern):
        value = str(path.resolve()) if "\\" in pattern or "/" in pattern else path.name
        expression = re.escape(pattern.replace("\\", "/").casefold())
        expression = expression.replace(r"\*", ".*").replace(r"\?", ".")
        return re.fullmatch(expression, value.replace("\\", "/").casefold()) is not None

    excluded = {}
    for path in paths:
        for pattern, newest in rules:
            if not newest and matches(path, pattern):
                excluded[path] = f"filter: {pattern}"
                break
    for pattern, newest in rules:
        if not newest:
            continue
        canonical = pattern.replace("*", "").rstrip()
        canonical_path = Path(canonical)
        canonical = str(canonical_path.with_name(
            canonical_path.stem.rstrip() + canonical_path.suffix
        ))
        candidates = [
            path for path in paths if path not in excluded
            and (matches(path, pattern) or matches(path, canonical))
        ]
        if not candidates:
            continue
        undated = [path for path in candidates if matches(path, canonical)]

        def version_key(path):
            return (tuple(int(value) for value in re.findall(r"\d+", path.stem)),
                    str(path.resolve()).casefold())

        selected = max(undated or candidates, key=version_key)
        for path in candidates:
            if path != selected:
                excluded[path] = f"older version; selected: {selected.resolve()}"
    return [path for path in paths if path not in excluded], excluded


def inspect_sqlite(path):
    connection = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)
    try:
        tables = []
        for (name,) in connection.execute(
            "SELECT name FROM sqlite_schema WHERE type = 'table' "
            "AND name NOT LIKE 'sqlite_%' ORDER BY name"
        ):
            quoted = '"' + name.replace('"', '""') + '"'
            columns = [row[1] for row in connection.execute(f"PRAGMA table_info({quoted})")]
            tables.append({"table": name, "columns": columns})
        return tables
    finally:
        connection.close()


def access_data(path, selection=()):
    powershell = Path(os.environ.get("WINDIR", r"C:\Windows")) / (
        r"SysWOW64\WindowsPowerShell\v1.0\powershell.exe"
    )
    helper = TOOLS_DIR / "Helpers" / "CALL_Checker_SourceSchema.ps1"
    command = [str(powershell), "-NoLogo", "-NoProfile", "-NonInteractive",
               "-File", str(helper), "-DatabasePath", str(path.resolve())]
    if selection:
        command.extend(["-ReadCalls", "-Table", selection[0], "-Column", selection[1]])
    result = subprocess.run(
        command,
        capture_output=True, text=True, encoding="utf-8",
        timeout=300 if selection else 30, check=False,
        creationflags=subprocess.CREATE_NO_WINDOW,
    )
    if result.returncode:
        raise RuntimeError(result.stderr.strip() or result.stdout.strip()
                           or f"Access helper exited with code {result.returncode}")
    return json.loads(result.stdout)


def inspect_access(path):
    tables = access_data(path)
    if not isinstance(tables, list) or any(
        not isinstance(table, dict)
        or not isinstance(table.get("table"), str)
        or not isinstance(table.get("columns"), list)
        or any(not isinstance(column, str) for column in table["columns"])
        for table in tables
    ):
        raise ValueError("Access helper returned invalid schema metadata.")
    return tables


def inspect_source(path):
    path = Path(path)
    if not path.is_file():
        raise FileNotFoundError(f"Source database does not exist: {path}")
    if path.suffix.lower() in {".mdb", ".accdb"}:
        return inspect_access(path)
    if path.suffix.lower() in DATABASE_EXTENSIONS:
        return inspect_sqlite(path)
    raise ValueError(f"Unsupported database extension: {path.suffix}")


def select_column(tables, selection):
    candidates = [
        (table["table"], column)
        for table in tables for column in table["columns"]
        if column.casefold() in CALL_COLUMNS
    ]
    if selection:
        candidates = [
            pair for pair in candidates
            if tuple(value.casefold() for value in pair)
            == tuple(value.casefold() for value in selection)
        ]
        if not candidates:
            raise ValueError("Selected CALL/CALLSIGN column was not found; check path|table|column.")
    if not candidates:
        return None
    if len(candidates) > 1:
        names = ", ".join(f"{table}.{column}" for table, column in candidates)
        raise ValueError(f"Ambiguous CALL/CALLSIGN columns ({names}); set path|table|column.")
    return candidates[0]


def read_calls(path, selection):
    if path.suffix.lower() in {".mdb", ".accdb"}:
        values = access_data(path, selection)
        if not isinstance(values, list) or any(
            value is not None and not isinstance(value, str) for value in values
        ):
            raise ValueError("Access helper returned invalid callsign data.")
        return values
    connection = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)
    try:
        table, column = ('"' + value.replace('"', '""') + '"' for value in selection)
        return [row[0] for row in connection.execute(f"SELECT {column} FROM {table}")]
    finally:
        connection.close()


def normalize_call(value):
    if not isinstance(value, str):
        return None
    call = value.strip().upper()
    if "/" in call:
        span = base_call_span(call)
        return call[slice(*span)] if span is not None else None
    if (re.fullmatch(r"[A-Z0-9/]{3,}", call)
            and any(char.isalpha() for char in call)
            and any(char.isdigit() for char in call)):
        return call
    return None


class ImportFailure(RuntimeError):
    def __init__(self, message, report):
        super().__init__(message)
        self.report = report


def update_target(target, calls=None):
    mode = "rw" if calls is not None else "ro"
    connection = sqlite3.connect(target.as_uri() + f"?mode={mode}", uri=True, timeout=5)
    try:
        if calls is not None:
            connection.execute("BEGIN IMMEDIATE")
        columns = connection.execute("PRAGMA table_info(callsigns)").fetchall()
        if len(columns) != 1 or columns[0][1].casefold() != "callsign" or columns[0][5] != 1:
            raise ValueError("Target must have callsigns(callsign PRIMARY KEY) only.")
        additions = []
        if calls is not None:
            existing = {
                call for (value,) in connection.execute("SELECT callsign FROM callsigns")
                if (call := normalize_call(value)) is not None and "/" not in value
            }
            additions = sorted(calls - existing)
            connection.executemany("INSERT INTO callsigns(callsign) VALUES (?)",
                                   ((call,) for call in additions))
        if connection.execute("PRAGMA integrity_check").fetchall() != [("ok",)]:
            raise RuntimeError("Target integrity check failed.")
        total_calls = connection.execute(
            "SELECT COUNT(DISTINCT callsign) FROM callsigns"
        ).fetchone()[0]
        if calls is not None:
            connection.execute(
                "CREATE TABLE IF NOT EXISTS metadata "
                "(key TEXT PRIMARY KEY, value TEXT NOT NULL, description TEXT)"
            )
            connection.execute(
                "INSERT INTO metadata(key, value, description) VALUES ('callsigns', ?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value, "
                "description=excluded.description",
                (str(total_calls), "Total distinct stored callsigns in this database"),
            )
        connection.commit()
        return len(additions), total_calls
    finally:
        if connection.in_transaction:
            connection.rollback()
        connection.close()


def import_report(results, target, total_calls, status):
    lines = [f"Import date/time: {datetime.now().astimezone().isoformat(timespec='seconds')}"]
    for result in results:
        lines.append(f"Database: {result['path']}")
        if "valid" in result:
            lines.append(
                f"  Unique valid calls: {result['valid']}; new calls added: {result['new']}; "
                f"invalid values skipped: {result['rejected']}"
            )
            lines.append(f"  Slash values skipped (ambiguous/unsupported): "
                         f"{result['slash_rejected']}")
        lines.append(f"  {result['status']}"
                     + (f": {result['reason']}" if "reason" in result else ""))
    lines.extend([
        f"Total databases inspected: {sum(result.get('inspected', False) for result in results)}",
        f"Total databases skipped: {sum(result['status'] == 'SKIPPED' for result in results)}",
        f"Total databases committed: {sum(result['status'] == 'COMMITTED' for result in results)}",
        f"Total databases read: {sum('valid' in result for result in results)}",
        f"Total new calls added: {sum(result['new'] for result in results)}",
        f"Target database: {target}",
        f"Total unique calls in target after import: "
        f"{total_calls if total_calls is not None else 'unavailable (target validation failed)'}",
        f"Status: {status}",
    ])
    return "\n".join(lines)


def import_calls(entries, target):
    if not entries:
        raise ValueError("Source list is empty.")
    target = target.resolve()
    for source, _ in entries:
        if source.resolve() == target or (source.exists() and target.exists()
                                          and source.samefile(target)):
            raise ValueError("Target database cannot also be a source.")
    results = [{"path": source, "status": "NOT PROCESSED", "new": 0} for source, _ in entries]
    try:
        _, total_calls = update_target(target)
    except (OSError, sqlite3.Error, ValueError, RuntimeError) as error:
        message = f"Target {target}: {error}; no source transactions committed."
        raise ImportFailure(message, import_report(results, target, None,
                                                    f"FAILED - {message}")) from error
    for number, ((source, selection), result) in enumerate(zip(entries, results), 1):
        try:
            print(f"Import progress: Reading database {number}/{len(entries)}: {source}",
                  flush=True)
            tables = inspect_source(source)
            result["inspected"] = True
            selected = select_column(tables, selection)
            if selected is None:
                result.update(status="SKIPPED", reason="No supported CALL/CALLSIGN column.")
                print(f"SKIPPED: {source}: {result['reason']}", flush=True)
                continue
            values = read_calls(source, selected)
            valid = {call for value in values if (call := normalize_call(value)) is not None}
            rejected = sum(normalize_call(value) is None for value in values)
            slash_rejected = sum(
                isinstance(value, str) and "/" in value and normalize_call(value) is None
                for value in values
            )
            result.update(valid=len(valid), rejected=rejected, slash_rejected=slash_rejected)
            print(f"{source}: {len(valid)} unique valid calls; {rejected} invalid values skipped.",
                  flush=True)
            if slash_rejected:
                print(f"Slash values skipped (ambiguous/unsupported): {slash_rejected}", flush=True)
            print(f"Import progress: Updating target database: {target} "
                  f"(source {number}/{len(entries)}: {source})", flush=True)
            added, total_calls = update_target(target, valid)
            result.update(status="COMMITTED", new=added)
            print(f"Import committed: {added} added; {len(valid) - added} already present. "
                  f"Source {number}/{len(entries)} saved; target total: {total_calls}.", flush=True)
        except (OSError, sqlite3.Error, subprocess.SubprocessError, ValueError, RuntimeError) as error:
            result.update(status="FAILED", reason=str(error))
            committed = sum(item["status"] == "COMMITTED" for item in results)
            message = (f"{source}: {error}; {committed} earlier source transaction(s) "
                       "remain saved. Import stopped.")
            status = "PARTIAL" if committed else "FAILED"
            raise ImportFailure(message, import_report(results, target, total_calls,
                                                        status)) from error
    status = ("COMMITTED" if any(result["status"] == "COMMITTED" for result in results)
              else "NO CHANGES (all sources skipped)")
    return import_report(results, target, total_calls, status)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, prefix_chars="-/")
    parser.add_argument("/H", "/h", "/?", action="help",
                        help="Show this help and exit (DOS-style aliases).")
    parser.add_argument("--scan", type=Path, metavar="FOLDER",
                        help="Scan an allowed folder/subfolder from scan_list.txt; filenames only.")
    parser.add_argument("--scan-listed", action="store_true",
                        help="Scan all allowed roots in scan_list.txt; filenames only.")
    parser.add_argument("--scan-list", type=Path, default=SCAN_LIST_PATH,
                        help="Allowed scan roots file (default: scan_list.txt beside this tool).")
    parser.add_argument("--source", action="append", type=Path,
                        help="Inspect this source instead of the editable default list; repeatable.")
    parser.add_argument("--inspect", action="store_true",
                        help="Open selected sources read-only and list schema candidates, not records.")
    parser.add_argument("--list", type=Path, default=LIST_PATH, dest="list_path",
                        help="Source list file (default: source_list.txt beside this script).")
    parser.add_argument("--import", action="store_true", dest="do_import",
                        help="Import unique calls into CALL_CHECKER.DB; sources are read-only.")
    parser.add_argument("--update-list", action="store_true",
                        help="Append newly scanned database paths to the list, without duplicates.")
    parser.add_argument("--scan-result", action="store_true",
                        help="Replace scan_result.txt beside the list with discovered paths.")
    parser.add_argument("--filter", type=Path, default=FILTER_PATH, dest="filter_path",
                        help="Scan exclusions/newest rules (default: source_filter.txt beside tool).")
    parser.add_argument("--target", type=Path, default=TARGET_PATH,
                        help="Import target; also excluded from scan/list additions.")
    args = parser.parse_args(argv)
    do_scan = args.scan is not None or args.scan_listed
    if args.scan is not None and args.scan_listed:
        parser.error("--scan and --scan-listed cannot be combined")
    if do_scan and args.source:
        parser.error("--scan and --source cannot be combined")
    if do_scan and args.inspect:
        parser.error("--scan lists filenames only; use --source with --inspect")
    if args.do_import and (do_scan or args.source or args.inspect or args.update_list
                           or args.scan_result):
        parser.error("--import reads only the source list; do not combine with other actions")
    if args.update_list and not do_scan:
        parser.error("--update-list requires --scan or --scan-listed")
    if args.scan_result and not do_scan:
        parser.error("--scan-result requires --scan or --scan-listed")
    try:
        if args.do_import:
            entries = read_list(args.list_path)
            report_path = args.list_path.resolve().with_name("import_result.txt")
            for protected in [args.list_path, args.scan_list, args.filter_path, args.target,
                              *(source for source, _ in entries)]:
                if (report_path == protected.resolve()
                        or (report_path.exists() and protected.exists()
                            and report_path.samefile(protected))):
                    raise ValueError("Import report may not overwrite the list or any database.")
            failure = None
            try:
                report = import_calls(entries, args.target)
            except ImportFailure as error:
                failure = error
                report = error.report
            try:
                with report_path.open("a", encoding="utf-8") as output:
                    output.write(report + "\n\n")
            except OSError as error:
                raise RuntimeError(
                    f"{failure or 'Import finished; completed source transactions remain saved'}; "
                    f"report append failed: {report_path}: {error}"
                ) from error
            print(f"Import report appended: {report_path}")
            if failure is not None:
                print(f"ERROR: {failure}", file=sys.stderr)
                return 1
            return 0
        if do_scan:
            scan_roots, paths = scan_locations(args.scan_list, args.scan)
        else:
            paths = args.source or [source for source, _ in read_list(args.list_path)]
        eligible, excluded = filter_sources(paths, args.filter_path) if do_scan else (paths, {})
        if do_scan:
            print(f"Scan filter: {len(excluded)} excluded; {len(eligible)} eligible.")
        new_paths = []
        if args.scan_result:
            scan_result = args.list_path.resolve().with_name("scan_result.txt")
            for protected in [args.list_path, args.scan_list, args.filter_path, args.target, *paths]:
                if (scan_result == protected.resolve()
                        or (scan_result.exists() and protected.exists()
                            and scan_result.samefile(protected))):
                    raise ValueError("Scan report may not overwrite the list or any database.")
            known = {
                str(source).casefold()
                for source, _ in (read_list(args.list_path) if args.list_path.exists() else [])
            }
            known.add(str(args.target.resolve()).casefold())
            new_paths = [path for path in eligible if str(path.resolve()).casefold() not in known]
        if args.update_list:
            for protected in [args.scan_list, args.filter_path]:
                if (args.list_path.resolve() == protected.resolve()
                        or (args.list_path.exists() and protected.exists()
                            and args.list_path.samefile(protected))):
                    raise ValueError("Source list may not overwrite the scan list or filter file.")
            count = update_list(args.list_path, eligible, args.target)
            print(f"Source list updated: {count} new paths. Review before importing.")
        if args.scan_result:
            new_keys = {str(path.resolve()).casefold() for path in new_paths}
            lines = [
                f"Scan date/time: {datetime.now().astimezone().isoformat(timespec='seconds')}",
                f"Allowed scan list: {args.scan_list.resolve()}",
                *(f"Scan folder: {root}" for root in scan_roots),
                f"Source list: {args.list_path.resolve()}",
                f"Source filter: {args.filter_path.resolve()}",
            ]
            for path in paths:
                key = str(path.resolve()).casefold()
                label = ("TARGET - excluded" if key == str(args.target.resolve()).casefold()
                         else (f"FILTERED - {excluded[path]}" if path in excluded
                               else ("NEW" if key in new_keys else "EXISTING")))
                lines.append(f"[{label}] {path.resolve()}")
            lines.extend([
                f"Total databases found: {len(paths)}",
                f"Filtered database paths: {len(excluded)}",
                f"New database paths: {len(new_keys)}",
                f"Paths added to source list: {count if args.update_list else 0}",
                "Filename scan only; no database records read or imported.",
            ])
            try:
                scan_result.write_text("\n".join(lines) + "\n", encoding="utf-8")
            except OSError as error:
                state = "Source list already updated; " if args.update_list else ""
                raise RuntimeError(f"{state}scan report write failed: {error}") from error
            print(f"Scan report replaced: {scan_result}")
    except (OSError, sqlite3.Error, subprocess.SubprocessError, ValueError, RuntimeError) as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 1
    failed = False
    print("READ-ONLY SOURCE INVENTORY - no records read or imported.")
    for path in paths:
        print(f"\n{path}")
        if not args.inspect:
            print(f"  FILTERED: {excluded[path]}" if path in excluded
                  else "  Listed only (schema not inspected).")
            continue
        try:
            tables = inspect_source(path)
            candidates = [
                (table["table"], column)
                for table in tables for column in table["columns"]
                if column.casefold() in CALL_COLUMNS
            ]
            for table, column in candidates:
                print(f"  Callsign candidate: [{table}].[{column}]")
            if not candidates:
                print("  No recognized callsign column; manual schema review required.")
                for table in tables:
                    print(f"  Table [{table['table']}]: {', '.join(table['columns'])}")
        except (OSError, sqlite3.Error, subprocess.SubprocessError, ValueError, RuntimeError) as error:
            print(f"  ERROR: {error}", file=sys.stderr)
            failed = True
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
