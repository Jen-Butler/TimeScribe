"""Build the .mcpb Desktop Extension bundle.

Steps:
  1. PyInstaller-freeze the stdio MCP server (mcp.spec -> onefile exe)
  2. Assemble bundle dir: manifest.json + server/timescribe-mcp.exe
  3. Zip as TimeScribeActivity-<version>.mcpb

Usage: python build_mcpb.py [--skip-freeze]
"""
from __future__ import annotations
import json
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

VERSION = "0.1.0"
HERE = Path(__file__).parent

MANIFEST = {
    "dxt_version": "0.1",
    "name": "timescribe-activity",
    "display_name": "TimeScribe Activity",
    "version": VERSION,
    "description": ("Read-only access to your local activity record: Edge browser "
                    "history across profiles, ActivityWatch window focus, inactivity "
                    "periods, and AI activity digests produced by the TimeScribe "
                    "desktop app."),
    "author": {"name": "Rising Tide Group"},
    "server": {
        "type": "binary",
        "entry_point": "server/timescribe-mcp.exe",
        "mcp_config": {
            "command": "${__dirname}/server/timescribe-mcp.exe",
            "args": [],
        },
    },
    "tools": [
        {"name": "get_browser_history",
         "description": "Edge browsing history for a day, across profiles"},
        {"name": "get_window_activity",
         "description": "Application window-focus events from ActivityWatch"},
        {"name": "get_inactivity_periods",
         "description": "AFK / asleep / locked periods for a day"},
        {"name": "get_activity_digest",
         "description": "Stored AI activity digest entries for a day"},
    ],
    "compatibility": {"platforms": ["win32"]},
}


def main():
    skip_freeze = "--skip-freeze" in sys.argv
    exe_src = HERE / "dist" / "timescribe-mcp.exe"

    if not skip_freeze:
        print("Freezing MCP server (PyInstaller)...")
        r = subprocess.run([sys.executable, "-m", "PyInstaller",
                            "mcp.spec", "--noconfirm"], cwd=HERE)
        if r.returncode != 0:
            sys.exit("PyInstaller failed")
    if not exe_src.exists():
        sys.exit(f"missing {exe_src} -- run without --skip-freeze")

    bundle = HERE / "mcpb_build"
    if bundle.exists():
        shutil.rmtree(bundle)
    (bundle / "server").mkdir(parents=True)
    (bundle / "manifest.json").write_text(
        json.dumps(MANIFEST, indent=2), encoding="utf-8")
    shutil.copy2(exe_src, bundle / "server" / "timescribe-mcp.exe")

    out = HERE / f"TimeScribeActivity-{VERSION}.mcpb"
    if out.exists():
        out.unlink()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        for p in bundle.rglob("*"):
            z.write(p, p.relative_to(bundle))
    size_mb = out.stat().st_size / 1e6
    print(f"\nBundle ready: {out}  ({size_mb:.1f} MB)")
    print("Install: Claude Desktop -> Settings -> Extensions -> drag this file in")


if __name__ == "__main__":
    main()
