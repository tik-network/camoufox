#!/usr/bin/env python3
"""
Fetches MacOSX SDK from Apple's currently-active CDN URL and extracts it
to ~/.mozbuild/MacOSX26.1.sdk/. Used by CI to bypass the dead/expired URL
hardcoded in Mozilla's taskcluster/kinds/toolchain/macos-sdk.yml.

Usage: python3 scripts/fetch-macos-sdk.py [SRC_DIR]
  SRC_DIR defaults to ./camoufox-<version>-<release>/

The script reuses Mozilla's unpack-sdk.py (already in the source tree)
to do the actual extraction, but bypasses its SHA512 check by computing
the hash from the actual download.
"""

import hashlib
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from urllib.request import urlopen

# Working Apple SDK URL (from Apple's current SUSCatalog).
# The Mozilla-pinned URL in macos-sdk.yml is dead/expired.
# This is the largest/newest CLTools_macOSNMOS_SDK.pkg currently served.
SDK_URL = (
    "https://swcdn.apple.com/content/downloads/50/51/"
    "071-29699-A_YC8SX0OHH3/7479xojqghsvgtnt3dxjpnxuz9sjpmbmds/"
    "CLTools_macOSNMOS_SDK.pkg"
)

# Path inside the .pkg where the SDK lives
EXTRACT_PREFIX = "Library/Developer/CommandLineTools/SDKs/MacOSX26.1.sdk"

# Final destination (where mach expects it)
DEST_DIR_NAME = "MacOSX26.1.sdk"


def download_pkg(url: str, out_path: Path) -> str:
    """Download the SDK package and return its sha512 hex digest."""
    print(f"Downloading {url}", file=sys.stderr)
    h = hashlib.sha512()
    with urlopen(url) as resp, open(out_path, "wb") as f:
        while True:
            buf = resp.read(1024 * 1024)
            if not buf:
                break
            h.update(buf)
            f.write(buf)
    print(f"  → {out_path} ({out_path.stat().st_size} bytes)", file=sys.stderr)
    return h.hexdigest()


def find_sdk_in_pkg(pkg_path: Path) -> str:
    """
    Inspect the .pkg to find which MacOSX SDK version it actually contains.
    The Mozilla extract_prefix is hardcoded to MacOSX26.1.sdk, but the
    pkg from Apple's catalog might have a different version. We need to
    detect it and rename later.
    """
    # Use python's tarfile/xar approach via unpack-sdk's mozpack helpers
    # if available, otherwise fall back to shelling out.
    try:
        from mozpack.macpkg import unxar  # type: ignore
        with open(pkg_path, "rb") as f:
            for name, content in unxar(f):
                if name in ("Payload", "Content"):
                    # Inspect first few entries to find SDK name
                    from mozpack.macpkg import Pbzx, uncpio
                    for path, st, _ in uncpio(Pbzx(content)):
                        if not path:
                            continue
                        path_str = path.decode()
                        if "/SDKs/MacOSX" in path_str and ".sdk/" in path_str:
                            # Extract the SDK directory name
                            after = path_str.split("/SDKs/", 1)[1]
                            sdk_name = after.split("/", 1)[0]
                            return sdk_name
                    break
    except Exception as e:
        print(f"  warning: could not detect SDK version: {e}", file=sys.stderr)
    return ""


def main():
    src_dir = Path(sys.argv[1]) if len(sys.argv) > 1 else None
    if src_dir is None:
        # Auto-detect camoufox source dir
        for entry in Path(".").iterdir():
            if entry.is_dir() and entry.name.startswith("camoufox-"):
                src_dir = entry
                break
    if src_dir is None or not src_dir.exists():
        print("error: cannot find camoufox source directory", file=sys.stderr)
        sys.exit(1)

    unpack_script = src_dir / "taskcluster" / "scripts" / "misc" / "unpack-sdk.py"
    if not unpack_script.exists():
        print(f"error: {unpack_script} not found", file=sys.stderr)
        sys.exit(1)

    mozbuild = Path.home() / ".mozbuild"
    dest = mozbuild / DEST_DIR_NAME

    if dest.exists() and any(dest.iterdir()):
        print(f"SDK already exists at {dest}, skipping fetch.", file=sys.stderr)
        return

    mozbuild.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        pkg_path = tmp_path / "sdk.pkg"
        sha512 = download_pkg(SDK_URL, pkg_path)
        print(f"  sha512 = {sha512}", file=sys.stderr)

        # Detect actual SDK version inside the pkg
        sdk_name = find_sdk_in_pkg(pkg_path)
        if sdk_name:
            print(f"  pkg contains: {sdk_name}", file=sys.stderr)
            extract_prefix = f"Library/Developer/CommandLineTools/SDKs/{sdk_name}"
        else:
            extract_prefix = EXTRACT_PREFIX

        # Run mozilla's unpack-sdk.py with the working URL and computed hash
        env = os.environ.copy()
        # Make sure mozpack is on the path
        env["PYTHONPATH"] = str(src_dir / "python" / "mozbuild") + os.pathsep + env.get("PYTHONPATH", "")
        cmd = [
            sys.executable,
            str(unpack_script),
            SDK_URL,
            sha512,
            extract_prefix,
            DEST_DIR_NAME,
        ]
        print(f"Running: {' '.join(cmd)}", file=sys.stderr)
        result = subprocess.run(cmd, cwd=str(mozbuild), env=env)
        if result.returncode != 0:
            print(f"error: unpack-sdk.py exited with {result.returncode}", file=sys.stderr)
            sys.exit(result.returncode)

        # Verify
        if not dest.exists() or not any(dest.iterdir()):
            print(f"error: extraction did not produce {dest}", file=sys.stderr)
            sys.exit(1)
        print(f"SDK extracted to {dest}", file=sys.stderr)

        # Write index file so mach treats it as up-to-date
        indices = mozbuild / "indices"
        indices.mkdir(parents=True, exist_ok=True)
        (indices / DEST_DIR_NAME).write_text("camoufox-prefetched")
        print(f"Wrote index marker {indices / DEST_DIR_NAME}", file=sys.stderr)


if __name__ == "__main__":
    main()
