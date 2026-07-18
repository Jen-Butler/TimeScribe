"""Build helper: download the ActivityWatch portable zip and extract it to
aw_dist/activitywatch/ so PyInstaller + Inno can bundle it.

Usage:  python fetch_aw.py [version]
Default version pinned below; pass e.g. `python fetch_aw.py v0.13.2` to bump.

ActivityWatch is MPL-2.0; bundling the unmodified official build with
attribution is permitted. See https://activitywatch.net
"""
from __future__ import annotations
import io
import sys
import zipfile
from pathlib import Path

import httpx

PINNED = "v0.13.2"


def main():
    version = sys.argv[1] if len(sys.argv) > 1 else PINNED
    url = (f"https://github.com/ActivityWatch/activitywatch/releases/download/"
           f"{version}/activitywatch-{version}-windows-x86_64.zip")
    dest = Path(__file__).parent / "aw_dist"
    if (dest / "activitywatch" / "aw-qt.exe").exists():
        print(f"aw_dist already populated; delete {dest} to re-fetch")
        return
    print(f"Downloading {url} (100+ MB, be patient)...")
    with httpx.stream("GET", url, follow_redirects=True, timeout=600) as r:
        r.raise_for_status()
        buf = io.BytesIO()
        for chunk in r.iter_bytes():
            buf.write(chunk)
    print("Extracting...")
    dest.mkdir(exist_ok=True)
    with zipfile.ZipFile(buf) as z:
        z.extractall(dest)
    exe = dest / "activitywatch" / "aw-qt.exe"
    print(f"Done: {exe} exists = {exe.exists()}")


if __name__ == "__main__":
    main()
