"""`aether db-stats` — render the running process's database accounting.

The counters live in the server process: the CLI asks the panel's own
`/api/db/stats` route over localhost and prints the report. Nothing is read
from or written to the database, and no user content is involved — only query
counts, estimated response bytes, durations and call sites.

Exit codes: 0 on a report, 1 when the server or token is unavailable, 2 on a
bad argument. The output says plainly which numbers are estimates.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from typing import Any

_HOURS = 24


def _human_bytes(count: float) -> str:
    value = float(count)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < 1024 or unit == "TB":
            return f"{value:.1f} {unit}" if unit != "B" else f"{int(value)} B"
        value /= 1024
    return f"{value:.1f} TB"


def _human_seconds(seconds: float) -> str:
    if seconds < 1:
        return f"{seconds * 1000:.0f} ms"
    if seconds < 60:
        return f"{seconds:.1f} s"
    return f"{seconds / 60:.1f} min"


def fetch_stats(base_url: str, token: str, *, timeout: float = 5.0) -> dict[str, Any]:
    """The live snapshot from one running Aether, over its own panel route."""
    url = base_url.rstrip("/") + "/api/db/stats"
    request = urllib.request.Request(url, headers={"x-aether-token": token})
    with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310 — operator URL
        payload = json.loads(response.read().decode("utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("the server returned an unexpected stats shape")
    return payload


def render(payload: dict[str, Any], *, top: int = 15) -> str:
    """A plain terminal report. Every byte figure is labelled as an estimate."""
    totals = payload.get("totals") or {}
    lines = [
        "Database use since this process started",
        f"  queries:      {int(totals.get('calls', 0)):,}",
        f"  rows:         {int(totals.get('rows', 0)):,}",
        f"  response:     ~{_human_bytes(totals.get('bytes', 0))} (estimated)",
        f"  query time:   {_human_seconds(float(totals.get('seconds', 0.0)))}",
        f"  uptime:       {_human_seconds(float(payload.get('uptime_seconds', 0.0)))}",
    ]
    connection = payload.get("connections") or {}
    if connection:
        lines.append(
            "  connections:  "
            f"{int(connection.get('acquires', 0)):,} leases, "
            f"peak {int(connection.get('peak', 0))}, "
            f"open {int(connection.get('size', 0))}"
        )

    vectors = (payload.get("caches") or {}).get("vectors") or {}
    if vectors:
        suffix = " (capped, oldest chunks left out)" if vectors.get("capped") else ""
        lines.append(
            "  vector cache: "
            f"{int(vectors.get('chunks', 0)):,} chunks, "
            f"{int(vectors.get('dims', 0))} dims, "
            f"~{_human_bytes(vectors.get('bytes', 0))}{suffix}"
        )

    sites = payload.get("sites") or []
    if sites:
        lines += ["", f"Top readers by estimated response bytes (of {len(sites)} call sites)"]
        for site in sites[:top]:
            lines.append(
                f"  {site['site']}: {int(site['calls']):,} calls, "
                f"{int(site['rows']):,} rows, ~{_human_bytes(site['bytes'])}, "
                f"{_human_seconds(float(site.get('seconds', 0.0)))}"
            )

    tables = payload.get("tables") or []
    if tables:
        lines += ["", "Top tables by estimated response bytes"]
        for table in tables[:top]:
            lines.append(
                f"  {table['table']}: {int(table['calls']):,} calls, "
                f"{int(table['rows']):,} rows, ~{_human_bytes(table['bytes'])}"
            )

    hourly = payload.get("hourly") or []
    if hourly and sum(int(value) for value in hourly) > 0:
        lines += ["", "Queries by UTC hour (process lifetime)"]
        busiest = max(int(value) for value in hourly) or 1
        for hour, count in enumerate(hourly[:_HOURS]):
            bar = "#" * max(0, round(int(count) / busiest * 40))
            lines.append(f"  {hour:02d}:00  {int(count):6,d}  {bar}")

    lines += [
        "",
        "Every byte figure is an estimate from the returned records, not a "
        "reading of the provider's meter. The database was not queried.",
    ]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="aether db-stats",
        description="Show the running Aether's query counts, estimated response bytes and call sites.",
    )
    parser.add_argument(
        "--url",
        default=os.environ.get("AETHER_STATS_URL", ""),
        help="base URL of the running panel (default: http://127.0.0.1:$PORT)",
    )
    parser.add_argument("--token", default=os.environ.get("AETHER_TOKEN", ""))
    parser.add_argument("--json", action="store_true", help="print the raw snapshot as JSON")
    parser.add_argument("--top", type=int, default=15, help="rows per section (default: 15)")
    args = parser.parse_args(argv)

    base_url = args.url or f"http://127.0.0.1:{os.environ.get('PORT', '8000')}"
    if not args.token:
        print(
            "aether db-stats needs the panel token: set AETHER_TOKEN or pass --token.",
            file=sys.stderr,
        )
        return 1
    try:
        payload = fetch_stats(base_url, args.token)
    except (urllib.error.URLError, TimeoutError, ValueError) as exc:
        print(
            f"could not read the running panel at {base_url}: {exc}\n"
            "The counters live in the server process, so it has to be running.",
            file=sys.stderr,
        )
        return 1
    if args.json:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0
    print(render(payload, top=max(1, args.top)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
