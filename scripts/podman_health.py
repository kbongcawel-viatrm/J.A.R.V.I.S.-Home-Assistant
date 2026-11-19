#!/usr/bin/env python3
"""Podman container health agent — pushes status to the remote dashboard.

Runs on the container host (home-security / 192.168.1.58), polls
`podman ps` every INTERVAL seconds, and POSTs each container's health
to the dashboard's /ingest endpoint on the monitoring host.

Stdlib only. Safe for low-resource hosts.

Usage:
    python3 podman_health.py --target http://192.168.1.57:8080/ingest
    python3 podman_health.py --target http://192.168.1.57:8080/ingest \
        --interval 30 --filter vite-apps
"""

import argparse
import json
import subprocess
import sys
import time
import urllib.request


def podman_ps():
    """Return list of {name, status, up} for all containers."""
    try:
        out = subprocess.run(
            ["podman", "ps", "-a", "--format", "{{.Names}}|{{.Status}}"],
            capture_output=True, text=True, timeout=15)
    except (OSError, subprocess.TimeoutExpired):
        return []
    if out.returncode != 0:
        return []
    items = []
    for line in out.stdout.splitlines():
        line = line.strip()
        if "|" not in line:
            continue
        name, _, status = line.partition("|")
        status_l = status.lower()
        # negative states first: "Exited (137) ... (healthy)" still means DOWN
        # — the "(healthy)" suffix is the last health result, not current state
        if any(s in status_l for s in ("unhealthy", "exited", "dead")):
            up = False
        elif "up" in status_l or "healthy" in status_l:
            up = True
        else:
            up = None  # created/configured/etc -> unknown
        items.append({"name": name, "status": status.strip(), "up": up})
    return items


def post(target, payload):
    try:
        req = urllib.request.Request(
            target,
            data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"},
            method="POST")
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status == 200
    except Exception:
        return False


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--target", required=True,
                    help="dashboard ingest URL, e.g. http://192.168.1.57:8080/ingest")
    ap.add_argument("--interval", type=int, default=60)
    ap.add_argument("--filter", default="",
                    help="only report containers whose name contains this")
    ap.add_argument("--once", action="store_true",
                    help="run a single poll and exit (for testing/cron)")
    args = ap.parse_args()

    def cycle():
        items = podman_ps()
        ok = 0
        for c in items:
            if args.filter and args.filter not in c["name"]:
                continue
            payload = {
                "name": f"pod:{c['name']}",
                "up": c["up"],
                "ms": None,
                "extra": {"status": c["status"]},
            }
            if post(args.target, payload):
                ok += 1
        print(f"[{time.strftime('%H:%M:%S')}] reported {ok} container(s)",
              flush=True)

    while True:
        cycle()
        if args.once:
            break
        time.sleep(args.interval)


if __name__ == "__main__":
    main()
