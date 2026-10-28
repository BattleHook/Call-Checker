# Call Checker

Call Checker is a Windows desktop utility for finding callsigns in a local
SQLite database. The source also includes tools for discovering and inspecting
candidate source databases and importing selected callsigns.

The current source version is 0.1. This source repository does not include a
downloadable application package or a populated callsign database.

Read the [online user guide](https://battlehook.github.io/Call-Checker/) or
continue with the source setup below.

## Features

- Search a locally supplied callsign table and display ordered suggestions.
- Match portable callsigns conservatively while preserving the full input for
  copying.
- Scan only operator-configured roots for candidate database files. Scans list
  paths; they do not read database contents.
- Inspect source table/column names read-only before choosing import sources.
- Import selected callsigns into an existing target, committing each completed
  source separately and recording a report.

## Important limitations and safe use

- The application is Windows-only. Its graphical interface uses Tkinter.
- The target database must contain `callsigns(callsign TEXT PRIMARY KEY)`.
  No real callsign data or station configuration is included. The source
  export includes a minimal `CALL_CHECKER.DB` starter with that schema and
  only the author's callsign, `PA1RGX`; no SCP database or bulk call list is
  included.
- Import updates the target database and does not create a backup. Keep a
  separate backup and review source paths/schema selections before importing.
- Source databases are opened read-only. A later source failure does not undo
  earlier per-source commits; the report records partial results.
- Scan-root restrictions are an application guard, not an operating-system
  security boundary. Configure only roots you intend to scan.
- Access `.mdb` support requires a compatible installed 32-bit provider and
  Windows PowerShell. ACCDB/ACE compatibility has not been validated.
- A source dataset may have its own license or redistribution restrictions.
  Review those terms before using or sharing it.
- The checker is not a substitute for verifying a callsign against an
  authoritative source.

## Run from source

Use 64-bit Windows with Python 3.14 and Tkinter. Create a private environment
and install the pinned build dependency:

```powershell
py -3.14 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r Build\requirements.txt
```

For source-mode use, initialize the operator-specific lists from the safe
templates, then edit them for your own data. The scan list is intentionally
empty until configured:

```powershell
Copy-Item .\Build\Defaults\source_list.txt .\Tools\source_list.txt
Copy-Item .\Build\Defaults\scan_list.txt .\Tools\scan_list.txt
Copy-Item .\Build\Defaults\source_filter.txt .\Tools\source_filter.txt
```

The included `CALL_CHECKER.DB` beside `CALL_Checker.py` lets the program start
without a missing-database error and includes the single seed call `PA1RGX`.
To add more suggestions, configure your source list deliberately, review your
selected sources, then use **Sources > Import callsigns** in the application.
Start the interface with:

```powershell
.\.venv\Scripts\python.exe .\CALL_Checker.py
```

Read [Tools/help.txt](Tools/help.txt) and
[Tools/CALL_Checker_Sources_help.txt](Tools/CALL_Checker_Sources_help.txt)
before using scan or import operations. Never use a real station database as a
test fixture.

## Build and tests

Build the Windows folder distribution from the project root:

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s Tests -v
.\.venv\Scripts\python.exe -m PyInstaller --clean --noconfirm --distpath .\dist --workpath .\build-work .\Build\CALL_Checker.spec
.\.venv\Scripts\python.exe .\Build\smoke_test.py .\dist\CALL_Checker_v0.1
```

Tests use synthetic data. The real Access-provider integration test is skipped
unless its 32-bit Windows PowerShell/Jet prerequisites are present. The smoke
test copies the build to a temporary directory, creates synthetic databases,
checks the import and GUI startup/close, and cleans up its own temporary data.
The full suite also starts Windows PowerShell helper scripts. If the machine's
execution policy blocks them, those cases cannot pass; do not weaken a
persistent policy solely for testing. Run the GUI-only checks separately with:

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s Tests -p test_call_checker.py -v
```

The build produces a local folder under `dist/`; it does not publish or upload
anything. Binary release packaging and release acceptance are separate work.

## Support

Use the repository's Issues page for
reproducible bugs or feature requests. Include the Windows/Python version and
minimal synthetic reproduction details. Review reports and paths for private
information; do not attach personal databases. The repository includes
separate bug-report and feature-request forms.

## Optional support

Call Checker is free to use. If it is useful to you, you can optionally
support its development with a one-time donation on
[Ko-fi](https://ko-fi.com/battlehook). You choose the amount; donations are
not required and do not unlock features.

[![ko-fi](https://ko-fi.com/img/githubbutton_sm.svg)](https://ko-fi.com/U7W728RRWG)

## License

Developed by **BattleHook (PA1RGX)**. Original Call Checker source and
documentation are released under the
[MIT License](LICENSE). Redistributed fonts retain their separate terms; see
[THIRD-PARTY-NOTICES.md](THIRD-PARTY-NOTICES.md).
