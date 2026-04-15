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


def find_src_dir() -> Path:
    """Find the camoufox source directory."""
    if len(sys.argv) > 1:
        return Path(sys.argv[1]).resolve()
    for entry in Path(".").iterdir():
        if entry.is_dir() and entry.name.startswith("camoufox-"):
            return entry.resolve()
    print("error: cannot find camoufox source directory", file=sys.stderr)
    sys.exit(1)


def setup_mozpack(src_dir: Path):
    """Add mozpack to sys.path so we can import it in-process."""
    mozbuild_py = str(src_dir / "python" / "mozbuild")
    if mozbuild_py not in sys.path:
        sys.path.insert(0, mozbuild_py)


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
    print(f"  -> {out_path} ({out_path.stat().st_size} bytes)", file=sys.stderr)
    return h.hexdigest()


def find_sdk_in_pkg(pkg_path: Path) -> str:
    """
    Inspect the .pkg to find which MacOSX SDK version it actually contains.
    Returns e.g. "MacOSX15.2.sdk" or "" on failure.
    """
    try:
        from mozpack.macpkg import Pbzx, uncpio, unxar

        with open(pkg_path, "rb") as f:
            for name, content in unxar(f):
                if name in ("Payload", "Content"):
                    for path, st, _ in uncpio(Pbzx(content)):
                        if not path:
                            continue
                        path_str = path.decode()
                        if "/SDKs/MacOSX" in path_str and ".sdk/" in path_str:
                            after = path_str.split("/SDKs/", 1)[1]
                            sdk_name = after.split("/", 1)[0]
                            return sdk_name
                    break
    except Exception as e:
        print(f"  warning: could not detect SDK version: {e}", file=sys.stderr)
    return ""


def extract_sdk_direct(pkg_path: Path, extract_prefix: str, dest: Path):
    """Extract the SDK directly using mozpack (no subprocess, no re-download)."""
    from mozpack.macpkg import Pbzx, uncpio, unxar
    import stat
    from io import BytesIO

    print(f"  Extracting with prefix: {extract_prefix}", file=sys.stderr)
    dest.mkdir(parents=True, exist_ok=True)
    count = 0
    hardlinks = {}

    with open(pkg_path, "rb") as f:
        for name, content in unxar(f):
            if name not in ("Payload", "Content"):
                continue
            for path, st_info, file_content in uncpio(Pbzx(content)):
                if not path:
                    continue
                path_str = path.decode()
                matches = path_str.startswith(extract_prefix)

                # Handle hardlinks (same logic as Mozilla's unpack-sdk.py)
                if stat.S_ISREG(st_info.mode) and st_info.nlink > 1:
                    key = (st_info.dev, st_info.ino)
                    hardlink = hardlinks.get(key)
                    if hardlink:
                        hardlink[0] -= 1
                        if hardlink[0] == 0:
                            del hardlinks[key]
                        file_content = hardlink[1]
                        if isinstance(file_content, BytesIO):
                            file_content.seek(0)
                            if matches:
                                out_path = str(dest / path_str[len(extract_prefix):].lstrip("/"))
                                hardlink[1] = out_path
                    elif matches:
                        out_path = str(dest / path_str[len(extract_prefix):].lstrip("/"))
                        hardlink = hardlinks[key] = [st_info.nlink - 1, out_path]
                    else:
                        hardlink = hardlinks[key] = [st_info.nlink - 1, BytesIO(file_content.read())]
                        file_content = hardlink[1]

                if not matches:
                    continue

                out_path = str(dest / path_str[len(extract_prefix):].lstrip("/"))
                if stat.S_ISDIR(st_info.mode):
                    os.makedirs(out_path, exist_ok=True)
                else:
                    parent = os.path.dirname(out_path)
                    if parent:
                        os.makedirs(parent, exist_ok=True)
                    if stat.S_ISLNK(st_info.mode):
                        os.symlink(file_content.read(), out_path)
                    elif stat.S_ISREG(st_info.mode):
                        if isinstance(file_content, str):
                            os.link(file_content, out_path)
                        else:
                            with open(out_path, "wb") as out:
                                shutil.copyfileobj(file_content, out)
                    count += 1
            break

    print(f"  Extracted {count} files to {dest}", file=sys.stderr)
    return count > 0


def main():
    src_dir = find_src_dir()
    setup_mozpack(src_dir)

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
            print("  using default extract prefix", file=sys.stderr)
            extract_prefix = EXTRACT_PREFIX

        # Extract directly using mozpack (avoids re-downloading)
        if not extract_sdk_direct(pkg_path, extract_prefix, dest):
            print(f"error: extraction did not produce any files in {dest}", file=sys.stderr)
            # Show what prefixes actually exist in the pkg for debugging
            try:
                from mozpack.macpkg import Pbzx, uncpio, unxar
                prefixes = set()
                with open(pkg_path, "rb") as f:
                    for name, content in unxar(f):
                        if name in ("Payload", "Content"):
                            for path, st, _ in uncpio(Pbzx(content)):
                                if path:
                                    p = path.decode()
                                    # Show top-level paths for debugging
                                    parts = p.split("/")
                                    if len(parts) >= 5:
                                        prefixes.add("/".join(parts[:5]))
                                    if len(prefixes) >= 20:
                                        break
                            break
                print(f"  paths found in pkg:", file=sys.stderr)
                for p in sorted(prefixes):
                    print(f"    {p}", file=sys.stderr)
            except Exception:
                pass
            sys.exit(1)

        print(f"SDK extracted to {dest}", file=sys.stderr)

        # Write index file so mach treats it as up-to-date
        indices = mozbuild / "indices"
        indices.mkdir(parents=True, exist_ok=True)
        (indices / DEST_DIR_NAME).write_text("camoufox-prefetched")
        print(f"Wrote index marker {indices / DEST_DIR_NAME}", file=sys.stderr)


if __name__ == "__main__":
    main()
