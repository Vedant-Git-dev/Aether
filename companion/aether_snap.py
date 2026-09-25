#!/usr/bin/env python3
"""aether_snap — capture the screen and hand the image to a running Aether.

Wayland (grim) only. Perception is Aether-side: the screenshot is described
once by the vision model and stored as an encrypted memory event; nothing
here or there turns screen content into actions.

Usage:
  python aether_snap.py                          # local Aether, token from $AETHER_API_TOKEN
  python aether_snap.py --url https://my-aether.onrender.com --token ... --note "keep an eye on this"
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys

import httpx


def main() -> int:
    parser = argparse.ArgumentParser(description="Capture the screen and send it to Aether.")
    parser.add_argument("--url", default=os.environ.get("AETHER_URL", "http://localhost:8000"))
    parser.add_argument("--token", default=os.environ.get("AETHER_API_TOKEN", ""))
    parser.add_argument("--note", default="", help="context to store alongside the capture")
    parser.add_argument("--output", default=None, help="Wayland output to capture (default: all)")
    args = parser.parse_args()

    if not args.token:
        print("no token — pass --token or set AETHER_API_TOKEN", file=sys.stderr)
        return 2

    command = ["grim"]
    if args.output:
        command += ["-o", args.output]
    command.append("-")  # PNG on stdout

    try:
        png = subprocess.run(command, capture_output=True, check=True).stdout
    except FileNotFoundError:
        print("grim not found — install it (Wayland screenshot tool)", file=sys.stderr)
        return 1
    except subprocess.CalledProcessError as exc:
        print(f"grim failed: {exc.stderr.decode(errors='replace').strip()}", file=sys.stderr)
        return 1

    try:
        response = httpx.post(
            f"{args.url.rstrip('/')}/api/screen-capture",
            params={"token": args.token},
            files={"file": ("screen.png", png, "image/png")},
            data={"note": args.note},
            timeout=120,
        )
    except httpx.HTTPError as exc:
        print(f"could not reach Aether at {args.url}: {exc}", file=sys.stderr)
        return 1

    if response.status_code == 202:
        print("captured:", response.json())
        return 0
    print(f"Aether said {response.status_code}: {response.text}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
