#!/usr/bin/env python3
"""Basic health check for the voice stack on a target host.

Checks: ping (100% reply), HTTP 200 on known service ports, docker health.

Usage:
    python3 health_check.py <ip-or-host> [port ...]

Defaults to ports 8178 (whisper.cpp), 11434 (ollama), 9090 (whisperlive),
5000 (whisper-fastapi) when no ports are given.
"""

import shutil
import subprocess
import sys
import urllib.error
import urllib.request

DEFAULT_PORTS = (8178, 11434, 9090, 5000)

passed = 0
failed = 0


def ok(msg):
    global passed
    passed += 1
    print(f"  [OK]   {msg}")


def bad(msg):
    global failed
    failed += 1
    print(f"  [FAIL] {msg}")


def check_ping(target):
    """Require 100% replies from 4 pings."""
    if sys.platform == "win32":
        cmd = ["ping", "-n", "4", target]
    else:
        cmd = ["ping", "-c", "4", "-W", "2", target]
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
    except (OSError, subprocess.TimeoutExpired):
        bad(f"ping {target} (no replies)")
        return
    text = out.stdout + out.stderr
    # Linux: "4 received, 0% packet loss"; macOS/BSD: "4 packets transmitted, 4 packets received, 0.0% packet loss"
    import re
    recv = re.search(r"(\d+)\s+(?:packets?\s+)?received", text)
    loss = re.search(r"(\d+(?:\.\d+)?)%\s+packet\s+loss", text)
    if recv and recv.group(1) == "4" and (not loss or float(loss.group(1)) == 0.0):
        ok(f"ping {target} (4/4 replies)")
    else:
        bad(f"ping {target} (lossy/no replies)")


def check_http(target, port):
    url = f"http://{target}:{port}/"
    try:
        with urllib.request.urlopen(url, timeout=3) as resp:
            code = resp.status
    except urllib.error.HTTPError as e:
        code = e.code
    except Exception:
        code = 0
    if code == 200:
        ok(f"{url} -> 200")
    else:
        bad(f"{url} -> {code or 'unreachable'}")


def check_docker():
    """Container health — only meaningful on the local docker host."""
    if not shutil.which("docker"):
        print("  [SKIP] docker not installed")
        return
    try:
        out = subprocess.run(
            ["docker", "ps", "--format", "{{.Names}} {{.Status}}"],
            capture_output=True, text=True, timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired):
        print("  [SKIP] docker not reachable")
        return
    if out.returncode != 0:
        print("  [SKIP] docker daemon not running")
        return
    containers = [l for l in out.stdout.splitlines() if l.strip()]
    if not containers:
        print("  [SKIP] no running containers")
        return
    unhealthy = [c for c in containers if "unhealthy" in c.lower()
                 or ("Up" not in c and "healthy" not in c.lower())]
    if unhealthy:
        bad(f"docker: unhealthy -> {'; '.join(unhealthy)}")
    else:
        ok(f"docker: {len(containers)} container(s), all healthy/up")


def main():
    args = sys.argv[1:]
    if not args or args[0] in ("-h", "--help"):
        print(__doc__)
        sys.exit(1 if not args else 0)
    target = args[0]
    ports = [int(p) for p in args[1:]] or list(DEFAULT_PORTS)

    print(f"== Health check: {target} ==")
    check_ping(target)
    for port in ports:
        check_http(target, port)
    check_docker()

    print(f"== Result: {passed} passed, {failed} failed ==")
    sys.exit(0 if failed == 0 else 1)


if __name__ == "__main__":
    main()
