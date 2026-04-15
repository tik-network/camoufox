#!/usr/bin/env python3
"""
Fetches MacOSX SDK from Apple's CDN and extracts it to ~/.mozbuild/MacOSX26.1.sdk/.
Used by CI to work around dead/expired URLs in Mozilla's macos-sdk.yml.

Dynamically queries Apple's Software Update Catalog to find the current
CLTools_macOSNMOS_SDK.pkg URL, so it keeps working even when Apple rotates CDN paths.

Usage: python3 scripts/fetch-macos-sdk.py [SRC_DIR]
  SRC_DIR defaults to ./camoufox-<version>-<release>/
"""

import gzip
import hashlib
import os
import plistlib
import re
import shutil
import stat
import sys
import tempfile
from io import BytesIO
from pathlib import Path
from urllib.request import Request, urlopen

# Apple Software Update Catalog URLs (try multiple in case one is stale)
CATALOG_URLS = [
    "https://swscan.apple.com/content/catalogs/others/"
    "index-15-14-13-12-11-10.16-10.15-10.14-10.13-10.12-10.11-10.10-10.9-"
    "mountainlion-lion-snowleopard-leopard.merged-1.sucatalog",
]

# Fallback URLs to try if catalog lookup fails (known URLs, may be dead)
FALLBACK_URLS = [
    "https://swcdn.apple.com/content/downloads/22/09/"
    "093-00219-A_WIA1LP39TY/evbam2mb02xqr05ju9ddb95y8qil8kz9tm/"
    "CLTools_macOSNMOS_SDK.pkg",
    "https://swcdn.apple.com/content/downloads/50/51/"
    "071-29699-A_YC8SX0OHH3/7479xojqghsvgtnt3dxjpnxuz9sjpmbmds/"
    "CLTools_macOSNMOS_SDK.pkg",
]

# Path inside the .pkg where the SDK lives
EXTRACT_PREFIX = "Library/Developer/CommandLineTools/SDKs/MacOSX26.1.sdk"

# Final destination (where mach expects it)
DEST_DIR_NAME = "MacOSX26.1.sdk"

# Minimum SDK version we need
MIN_SDK_VERSION = "26.1"

# HTTP headers to mimic macOS Software Update
HTTP_HEADERS = {
    "User-Agent": "Software%20Update (unknown version) CFNetwork/902.2 Darwin/17.7.0 (x86_64)",
    "Accept": "*/*",
}


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


def http_get(url: str) -> bytes:
    """Fetch a URL with proper headers, return raw bytes."""
    req = Request(url, headers=HTTP_HEADERS)
    with urlopen(req, timeout=120) as resp:
        return resp.read()


def find_sdk_urls_from_catalog() -> list[str]:
    """Query Apple's Software Update Catalog for CLTools_macOSNMOS_SDK.pkg URLs."""
    urls = []
    for catalog_url in CATALOG_URLS:
        try:
            print(f"Querying catalog: {catalog_url}", file=sys.stderr)
            data = http_get(catalog_url)
            # Catalog may be gzipped or plain XML plist
            if data[:2] == b'\x1f\x8b':
                data = gzip.decompress(data)
            # Parse as plist
            try:
                catalog = plistlib.loads(data)
                products = catalog.get("Products", {})
                for prod_id, prod in products.items():
                    pkgs = prod.get("Packages", [])
                    for pkg in pkgs:
                        pkg_url = pkg.get("URL", "")
                        if "CLTools_macOSNMOS_SDK.pkg" in pkg_url:
                            urls.append(pkg_url)
                            print(f"  Found: {pkg_url} (size: {pkg.get('Size', '?')})", file=sys.stderr)
            except Exception:
                # Fallback: regex search for URLs in the raw XML
                text = data.decode("utf-8", errors="replace")
                for m in re.finditer(r'https?://[^<"]+CLTools_macOSNMOS_SDK\.pkg', text):
                    url = m.group(0)
                    if url not in urls:
                        urls.append(url)
                        print(f"  Found (regex): {url}", file=sys.stderr)
        except Exception as e:
            print(f"  warning: catalog fetch failed: {e}", file=sys.stderr)
    return urls


def download_pkg(url: str, out_path: Path) -> str:
    """Download the SDK package with proper headers, return sha512 hex digest."""
    print(f"Downloading {url}", file=sys.stderr)
    req = Request(url, headers=HTTP_HEADERS)
    h = hashlib.sha512()
    with urlopen(req, timeout=600) as resp, open(out_path, "wb") as f:
        while True:
            buf = resp.read(1024 * 1024)
            if not buf:
                break
            h.update(buf)
            f.write(buf)
    print(f"  -> {out_path} ({out_path.stat().st_size} bytes)", file=sys.stderr)
    return h.hexdigest()


def try_download(urls: list[str], out_path: Path) -> tuple[str, str]:
    """Try downloading from multiple URLs, return (successful_url, sha512)."""
    errors = []
    for url in urls:
        try:
            sha512 = download_pkg(url, out_path)
            return url, sha512
        except Exception as e:
            errors.append((url, str(e)))
            print(f"  failed: {e}", file=sys.stderr)
    print("error: all download URLs failed:", file=sys.stderr)
    for url, err in errors:
        print(f"  {url}: {err}", file=sys.stderr)
    sys.exit(1)


def find_sdk_in_pkg(pkg_path: Path) -> str:
    """
    Inspect the .pkg to find which MacOSX SDK version it actually contains.
    Returns e.g. "MacOSX26.1.sdk" or "" on failure.
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

    # Build list of URLs to try: catalog results first, then fallbacks
    print("Searching for macOS SDK download URL...", file=sys.stderr)
    catalog_urls = find_sdk_urls_from_catalog()
    all_urls = catalog_urls + FALLBACK_URLS
    # Deduplicate while preserving order
    seen = set()
    urls = []
    for u in all_urls:
        if u not in seen:
            seen.add(u)
            urls.append(u)

    if not urls:
        print("error: no SDK URLs found from catalog or fallbacks", file=sys.stderr)
        sys.exit(1)

    print(f"Will try {len(urls)} URL(s)", file=sys.stderr)

    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        pkg_path = tmp_path / "sdk.pkg"
        url_used, sha512 = try_download(urls, pkg_path)
        print(f"  sha512 = {sha512}", file=sys.stderr)

        # Detect actual SDK version inside the pkg
        sdk_name = find_sdk_in_pkg(pkg_path)
        if sdk_name:
            print(f"  pkg contains: {sdk_name}", file=sys.stderr)
            extract_prefix = f"Library/Developer/CommandLineTools/SDKs/{sdk_name}"
            # Check if this SDK version is new enough
            version_match = re.search(r'MacOSX(\d+\.\d+)\.sdk', sdk_name)
            if version_match:
                pkg_version = version_match.group(1)
                if tuple(int(x) for x in pkg_version.split('.')) < tuple(int(x) for x in MIN_SDK_VERSION.split('.')):
                    print(f"  warning: SDK {pkg_version} is older than required {MIN_SDK_VERSION}", file=sys.stderr)
                    print(f"  from URL: {url_used}", file=sys.stderr)
                    # Try remaining URLs
                    remaining = [u for u in urls if u != url_used]
                    if remaining:
                        print(f"  Trying {len(remaining)} remaining URL(s)...", file=sys.stderr)
                        pkg_path2 = tmp_path / "sdk2.pkg"
                        try:
                            url_used2, sha512 = try_download(remaining, pkg_path2)
                            sdk_name2 = find_sdk_in_pkg(pkg_path2)
                            if sdk_name2:
                                v2 = re.search(r'MacOSX(\d+\.\d+)\.sdk', sdk_name2)
                                if v2 and tuple(int(x) for x in v2.group(1).split('.')) >= tuple(int(x) for x in MIN_SDK_VERSION.split('.')):
                                    pkg_path = pkg_path2
                                    sdk_name = sdk_name2
                                    sha512 = sha512
                                    extract_prefix = f"Library/Developer/CommandLineTools/SDKs/{sdk_name}"
                                    print(f"  Using {sdk_name} from {url_used2}", file=sys.stderr)
                                else:
                                    print(f"  SDK from fallback is also too old: {sdk_name2}", file=sys.stderr)
                        except SystemExit:
                            pass  # All remaining URLs also failed
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
                            for path, st_val, _ in uncpio(Pbzx(content)):
                                if path:
                                    p = path.decode()
                                    parts = p.split("/")
                                    if len(parts) >= 5:
                                        prefixes.add("/".join(parts[:5]))
                                    if len(prefixes) >= 20:
                                        break
                            break
                print("  paths found in pkg:", file=sys.stderr)
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
