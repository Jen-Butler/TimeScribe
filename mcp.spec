# -*- mode: python -*-
# Frozen stdio MCP server for the .mcpb bundle. Console app (stdio transport).
from PyInstaller.utils.hooks import collect_submodules

hiddenimports = (
    collect_submodules("keyring.backends")
    + collect_submodules("mcp.server")
    + collect_submodules("mcp.shared")
    + ["win32timezone"]
)

a = Analysis(
    ["mcp_launcher.py"],
    pathex=["."],
    hiddenimports=hiddenimports,
    excludes=["tkinter", "matplotlib", "numpy", "PIL", "pystray",
              "uvicorn", "fastapi", "anthropic", "openai"],
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz, a.scripts, a.binaries, a.datas,
    name="timescribe-mcp",
    icon="pad.ico",
    console=True,
    onefile=True,
)
