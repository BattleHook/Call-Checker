from pathlib import Path

root = Path(SPECPATH).parent
assets = [(str(root / "Fonts"), "Fonts")]
assets += [(str(path), "Tools/Helpers") for path in (root / "Tools" / "Helpers").iterdir()
           if path.suffix in (".ps1", ".txt")]
assets += [(str(root / "Tools" / name), "Tools") for name in
           ("help.txt", "CALL_Checker_Sources_help.txt")]
assets += [(str(path), "Tools") for path in (Path(SPECPATH) / "Defaults").glob("*.txt")]
assets += [(str(Path(SPECPATH) / "Release_help.txt"), ".")]

gui = Analysis([str(root / "CALL_Checker.py")], pathex=[str(root)], datas=assets)
sources = Analysis([str(root / "Tools" / "CALL_Checker_Sources.py")],
                   pathex=[str(root / "Tools")])
gui_exe = EXE(PYZ(gui.pure), gui.scripts, [], exclude_binaries=True,
              name="CALL_Checker_v0.1", console=False, contents_directory=".", upx=False)
sources_exe = EXE(PYZ(sources.pure), sources.scripts, [], exclude_binaries=True,
                  name="CALL_Checker_Sources", console=True, contents_directory=".", upx=False)
COLLECT(gui_exe, sources_exe, gui.binaries, gui.datas, sources.binaries, sources.datas,
        name="CALL_Checker_v0.1", upx=False)
