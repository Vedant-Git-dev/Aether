#!/usr/bin/env python3
"""aether_snap — capture the screen and hand the image to a running Aether.

Wayland (grim) only. Perception is Aether-side: the screenshot is described
once by the vision model and stored as an encrypted memory event; nothing
here or there turns screen content into actions.

Usage:
  python aether_snap.py                          # local Aether, token from $AETHER_API_TOKEN
  python aether_snap.py --url https://my-aether.onrender.com --token ... --note "keep an eye on this"
  python aether_snap.py --poll                   # daemon: capture whenever the agent asks
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time

import httpx


def capture(output: str | None) -> bytes:
    """grim PNG on stdout — `-o` picks one Wayland output, none means all."""
    command = ["grim"]
    if output:
        command += ["-o", output]
    command.append("-")
    return subprocess.run(command, capture_output=True, check=True).stdout


def upload(client: httpx.Client, url: str, token: str, png: bytes, note: str) -> None:
    response = client.post(
        f"{url.rstrip('/')}/api/screen-capture",
        params={"token": token},
        files={"file": ("screen.png", png, "image/png")},
        data={"note": note},
        timeout=120,
    )
    if response.status_code == 202:
        print("captured:", response.json(), flush=True)
    else:
        print(f"Aether said {response.status_code}: {response.text}", file=sys.stderr, flush=True)


def main() -> int:
    parser = argparse.ArgumentParser(description="Capture the screen and send it to Aether.")
    parser.add_argument("--url", default=os.environ.get("AETHER_URL", "http://localhost:8000"))
    parser.add_argument("--token", default=os.environ.get("AETHER_API_TOKEN", ""))
    parser.add_argument("--note", default="", help="context to store alongside the capture")
    parser.add_argument("--output", default=None, help="Wayland output to capture (default: all)")
    parser.add_argument(
        "--poll",
        action="store_true",
        help="keep running: poll for the agent's capture requests and fulfill them",
    )
    parser.add_argument(
        "--poll-interval",
        type=float,
        default=30.0,
        help="seconds between polls in --poll mode (default 30)",
    )
    args = parser.parse_args()

    if not args.token:
        print("no token — pass --token or set AETHER_API_TOKEN", file=sys.stderr)
        return 2

    url = args.url.rstrip("/")
    try:
        if not args.poll:
            upload(httpx.Client(), url, args.token, capture(args.output), args.note)
            return 0

        # daemon mode: the agent's request_screen_capture tool drops a
        # request; we take it (one capture per ask) and upload
        print(f"polling {url} for capture requests every {args.poll_interval:.0f}s…", flush=True)
        with httpx.Client(timeout=30) as client:
            while True:
                try:
                    response = client.get(f"{url}/api/screen-request", params={"token": args.token})
                    if response.status_code == 200:
                        requested_note = str(response.json().get("note", ""))
                        print("capture requested", flush=True)
                        upload(client, url, args.token, capture(args.output), args.note or requested_note)
                    elif response.status_code == 401:
                        print("bad token", file=sys.stderr)
                        return 2
                except httpx.HTTPError as exc:
                    print(f"poll failed: {exc}", file=sys.stderr, flush=True)
                except (FileNotFoundError, subprocess.CalledProcessError) as exc:
                    print(f"grim failed: {exc}", file=sys.stderr, flush=True)
                time.sleep(args.poll_interval)
    except FileNotFoundError:
        print("grim not found — install it (Wayland screenshot tool)", file=sys.stderr)
        return 1
    except subprocess.CalledProcessError as exc:
        print(f"grim failed: {exc.stderr.decode(errors='replace').strip()}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("stopped")
        return 0


if __name__ == "__main__":
    sys.exit(main())
