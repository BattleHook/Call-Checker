# Third-party notices

Call Checker's original source and documentation are licensed under the MIT
License; see [LICENSE](LICENSE). Third-party components retain their own terms.

## Included in the source export

### Cascadia Mono

- The four unmodified Cascadia Mono font files in `Fonts/` are Copyright
  Microsoft Corporation and distributed under the SIL Open Font License 1.1.
- The complete upstream license is retained as `Fonts/LICENSE.txt`; see
  `Fonts/help.txt` for local font use and redistribution notes.
- Upstream: <https://github.com/microsoft/cascadia-code/releases/tag/v2407.24>.

## Build-only dependency

- **PyInstaller 6.22.3** is pinned in `Build/requirements.txt` for building
  the Windows executable. It is not included in this source export.
- Python and its standard library are not included in this source export.

No application release binaries or bundled Python runtime are included here.
If a binary release is prepared later, review and include the notices and
license texts required for every component in that separate package.
