# -*- mode: python -*-
from PyInstaller.utils.hooks import collect_submodules

hiddenimports = (
    collect_submodules("keyring.backends")
    + ["win32timezone"]          # keyring/pywin32 quirk
)

a = Analysis(
    ["launcher.py"],
    pathex=["."],
    datas=[("timescribe/ui", "ui")],
    hiddenimports=hiddenimports,
    excludes=["tkinter", "matplotlib", "numpy"],
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz, a.scripts,
    exclude_binaries=True,
    name="TimeScribe",
    icon="pad.ico",
    console=False,
)
coll = COLLECT(exe, a.binaries, a.datas, name="TimeScribe")
