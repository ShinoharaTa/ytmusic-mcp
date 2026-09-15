"""Turn raw YouTube Music request headers into browser.json.

Usage:
    .venv/bin/python setup_auth.py headers_raw.txt

Write the request headers copied from your browser's devtools into
headers_raw.txt first (see README.md). This script converts them to the
credential file the MCP server reads, then shreds the raw file.
"""

import os
import sys
from pathlib import Path

import ytmusicapi

ROOT = Path(__file__).resolve().parent
OUT = ROOT / "browser.json"


def main() -> int:
    if len(sys.argv) != 2:
        print(__doc__)
        return 2

    raw_path = Path(sys.argv[1]).expanduser()
    if not raw_path.is_file():
        print(f"error: {raw_path} not found", file=sys.stderr)
        return 1

    raw = raw_path.read_text(encoding="utf-8")
    if "PASTE_" in raw:
        print("error: the file still contains placeholder text (PASTE_...)", file=sys.stderr)
        return 1

    if OUT.exists():
        backup = OUT.with_suffix(".json.bak")
        OUT.replace(backup)
        os.chmod(backup, 0o600)
        print(f"existing credentials moved to {backup.name}")

    try:
        ytmusicapi.setup(filepath=str(OUT), headers_raw=raw)
    except Exception as error:  # noqa: BLE001
        print(f"error: could not parse the headers ({type(error).__name__}: {error})", file=sys.stderr)
        return 1

    os.chmod(OUT, 0o600)
    raw_path.unlink()
    print(f"wrote {OUT} (mode 600) and deleted {raw_path}")

    from ytmusicapi import YTMusic

    try:
        info = YTMusic(str(OUT)).get_account_info()
    except Exception as error:  # noqa: BLE001
        print(f"warning: credentials written but the test call failed: {error}", file=sys.stderr)
        return 1

    print(f"authenticated as: {info.get('accountName')} ({info.get('channelHandle')})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
