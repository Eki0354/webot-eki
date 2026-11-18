#!/usr/bin/env python3
"""Build the self-built wcdb_api.dll replacement (Windows only).

Why this exists
---------------
The bundled `native/windows/wcdb_api.dll` came from WeFlow and carries a
hard-coded licence expiry in `wcdb_init()`.  After 2026-09-30 23:59:59 UTC it
returns -1000 unconditionally, so webot cannot start:

    0x1800e4469  call _mktime64         ; deadline 2026-09-30 23:59:59 UTC
    0x1800e447e  call _time64           ; now
    0x1800e4484  cmp  rax, rbx
    0x1800e4487  jle  0x1800e45bd       ; not expired -> return 0
    0x1800e45b6  mov  eax, 0xfffffc18   ; return -1000 once expired

`native/src/wcdb_api_replacement.c` reimplements the small C ABI webot uses
(10 exports) on top of SQLCipher, with no expiry check.

Prerequisites
-------------
1. A C compiler on PATH.  Verified with Strawberry Perl's gcc 13.2:
       C:\\Strawberry\\c\\bin\\gcc.exe
   MSVC works too but this script drives gcc.
2. The SQLCipher amalgamation (`sqlite3.c` + `sqlite3.h`).  It ships inside the
   `libsqlite3-sys` crate; this script finds it in the cargo registry.  If it
   is missing, fetch it with:
       cargo fetch            # or: pip download / manual download
   and point SQLCIPHER_DIR at the directory holding sqlite3.c.
3. OpenSSL to link against.  The DLL deliberately links the *Python-shipped*
   `libcrypto-3.dll`, because the app always runs inside a Python process that
   already has it — no extra file to bundle.

Usage
-----
    python tools/build_wcdb_api.py              # build + install
    python tools/build_wcdb_api.py --out DIR    # build into DIR only
    python tools/build_wcdb_api.py --check      # verify an existing DLL

The build is offline: no downloads, no network.
"""

from __future__ import annotations

import argparse
import ctypes
import glob
import os
import shutil
import subprocess
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SOURCE = PROJECT_ROOT / "native" / "src" / "wcdb_api_replacement.c"
INSTALL_TO = PROJECT_ROOT / "native" / "windows" / "wcdb_api.dll"

# Exports the replacement must provide.  The first ten are what webot calls;
# the last two are lifecycle + the marker used to detect this build.
REQUIRED_EXPORTS = [
    "InitProtection",
    "wcdb_init",
    "wcdb_open_account",
    "wcdb_close_account",
    "wcdb_free_string",
    "wcdb_get_sessions",
    "wcdb_get_messages",
    "wcdb_get_display_names",
    "wcdb_get_contacts_compact",
    "wcdb_get_group_members",
    "wcdb_shutdown",
    "webot_wcdb_api_replacement",
]


def find_gcc() -> str | None:
    """Locate a usable gcc, preferring one already on PATH."""
    for name in ("gcc", "x86_64-w64-mingw32-gcc"):
        found = shutil.which(name)
        if found:
            return found
    for candidate in (
        r"C:\Strawberry\c\bin\gcc.exe",
        r"C:\msys64\mingw64\bin\gcc.exe",
        r"C:\mingw64\bin\gcc.exe",
    ):
        if Path(candidate).is_file():
            return candidate
    return None


def find_sqlcipher(explicit: str | None) -> Path | None:
    """Find a directory containing both sqlite3.c and sqlite3.h."""
    if explicit:
        d = Path(explicit)
        return d if (d / "sqlite3.c").is_file() else None

    patterns = [
        # cargo registry (libsqlite3-sys ships the SQLCipher amalgamation)
        str(Path.home() / ".cargo" / "registry" / "src" / "*" /
            "libsqlite3-sys-*" / "sqlcipher"),
        # anything the caller staged locally
        str(PROJECT_ROOT / "native" / "sqlcipher"),
    ]
    for pattern in patterns:
        for hit in sorted(glob.glob(pattern)):
            d = Path(hit)
            if (d / "sqlite3.c").is_file() and (d / "sqlite3.h").is_file():
                return d
    return None


def find_libcrypto() -> Path | None:
    """Find libcrypto-3.dll — the Python process always ships one."""
    candidates = [
        Path(sys.prefix) / "DLLs" / "libcrypto-3.dll",
        Path(sys.executable).resolve().parent / "libcrypto-3.dll",
    ]
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        candidates.insert(0, Path(meipass) / "libcrypto-3.dll")
    for c in candidates:
        if c.is_file():
            return c
    return None


def verify_dll(path: Path) -> bool:
    """Load the built DLL and check it answers as the replacement."""
    if not path.is_file():
        print(f"  MISSING  {path}")
        return False

    libcrypto = find_libcrypto()
    if libcrypto:
        try:
            ctypes.CDLL(str(libcrypto))
        except OSError as e:
            print(f"  WARN     could not preload {libcrypto}: {e}")

    try:
        dll = ctypes.CDLL(str(path))
    except OSError as e:
        print(f"  FAIL     cannot load {path.name}: {e}")
        return False

    missing = []
    for name in REQUIRED_EXPORTS:
        if not hasattr(dll, name):
            missing.append(name)
    if missing:
        print(f"  FAIL     missing exports: {', '.join(missing)}")
        return False

    dll.webot_wcdb_api_replacement.argtypes = []
    dll.webot_wcdb_api_replacement.restype = ctypes.c_char_p
    marker = dll.webot_wcdb_api_replacement()
    if not marker or b"webot-wcdb-api-replacement" not in marker:
        print(f"  FAIL     unexpected marker: {marker!r}")
        return False

    dll.wcdb_init.argtypes = []
    dll.wcdb_init.restype = ctypes.c_int32
    rc = dll.wcdb_init()
    if rc != 0:
        print(f"  FAIL     wcdb_init returned {rc} (expected 0)")
        return False

    dll.wcdb_shutdown.argtypes = []
    dll.wcdb_shutdown.restype = ctypes.c_int32
    dll.wcdb_shutdown()

    print(f"  OK       {path.name}: 12 exports, marker present, wcdb_init=0")
    print(f"           {marker.decode()}")
    return True


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", help="output directory (default: install into native/windows)")
    ap.add_argument("--sqlcipher-dir", help="directory holding sqlite3.c / sqlite3.h")
    ap.add_argument("--check", action="store_true",
                    help="only verify the currently installed DLL")
    args = ap.parse_args()

    if args.check:
        print("Verifying installed replacement DLL...")
        return 0 if verify_dll(INSTALL_TO) else 1

    if sys.platform != "win32":
        print("This script builds the Windows DLL only.", file=sys.stderr)
        return 2

    gcc = find_gcc()
    if not gcc:
        print("No gcc found. Install Strawberry Perl or MinGW, or use MSVC.\n"
              "  https://strawberryperl.com/", file=sys.stderr)
        return 2

    sqlcipher = find_sqlcipher(args.sqlcipher_dir)
    if not sqlcipher:
        print("SQLCipher amalgamation not found (need sqlite3.c + sqlite3.h).\n"
              "  Looked in the cargo registry for libsqlite3-sys/sqlcipher.\n"
              "  Fetch it with `cargo fetch`, or pass --sqlcipher-dir.", file=sys.stderr)
        return 2

    libcrypto = find_libcrypto()
    if not libcrypto:
        print("libcrypto-3.dll not found next to the interpreter.\n"
              "  The DLL links OpenSSL; Python normally ships it in <prefix>/DLLs.",
              file=sys.stderr)
        return 2

    out_dir = Path(args.out) if args.out else INSTALL_TO.parent
    out_dir.mkdir(parents=True, exist_ok=True)
    out_dll = out_dir / "wcdb_api.dll"

    print(f"  gcc        {gcc}")
    print(f"  sqlcipher  {sqlcipher}")
    print(f"  libcrypto  {libcrypto}")
    print(f"  source     {SOURCE}")
    print(f"  output     {out_dll}")
    print()

    obj = out_dir / "sqlite3.o"
    compile_cmd = [
        gcc, "-c", "-O2", "-DSQLITE_HAS_CODEC",
        "-DSQLITE_TEMP_STORE=2", "-DSQLCIPHER_CRYPTO_OPENSSL",
        "-o", str(obj), str(sqlcipher / "sqlite3.c"),
        "-I", str(sqlcipher),
    ]
    print("Compiling SQLCipher...")
    r = subprocess.run(compile_cmd)
    if r.returncode != 0:
        print("SQLCipher compile failed.", file=sys.stderr)
        return 1

    link_cmd = [
        gcc, "-shared", "-O2", "-o", str(out_dll),
        str(SOURCE), str(obj),
        "-I", str(sqlcipher),
        str(libcrypto),
        "-lws2_32", "-lcrypt32",
    ]
    print("Linking wcdb_api.dll...")
    r = subprocess.run(link_cmd)
    if r.returncode != 0:
        print("Link failed.", file=sys.stderr)
        return 1

    try:
        obj.unlink()
    except OSError:
        pass

    print()
    print("Verifying...")
    if not verify_dll(out_dll):
        return 1

    size = out_dll.stat().st_size
    print()
    print(f"Built {out_dll} ({size:,} bytes)")
    if out_dll != INSTALL_TO:
        print(f"Installed copy: {INSTALL_TO} — copy it there to use it.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
