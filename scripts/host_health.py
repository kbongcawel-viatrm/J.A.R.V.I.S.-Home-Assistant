#!/usr/bin/env python3
"""Host/network health check — stdlib only, safe for low-RAM remotes.

Checks:
  1. Containers on a docker/podman host (all containers, incl. rootful)
  2. Ping targets (100% reply required)
  3. Memory + CPU of the target host (via SSH, if reachable)

Usage:
    python3 host_health.py --host 10.1.29.2 --ping 10.1.29.51 10.1.29.1
    python3 host_health.py --host 10.1.29.2 --ping 10.1.29.51 10.1.29.1 \
        --ssh user@10.1.29.2

Options:
    --host IP        Host to check containers/memory on (required)
    --ping IP [IP..] Hosts to ping (repeatable, at least one recommended)
    --ssh TARGET     SSH target for deep checks (e.g. user@10.1.29.2).
                     Runs: docker ps -a, podman ps -a, sudo -n podman ps -a,
                     free -m, uptime. Requires key auth or ssh-agent.
    --api-port N     Docker/Podman API port to probe (default: try 2375, 8080)
"""

import argparse
import json
import shutil
import subprocess
import sys
import urllib.request

passed = failed = skipped = 0


def ok(msg):
    global passed
    passed += 1
    print(f"  [OK]   {msg}")


def bad(msg):
    global failed
    failed += 1
    print(f"  [FAIL] {msg}")


def skip(msg):
    global skipped
    skipped += 1
    print(f"  [SKIP] {msg}")


# ---------------------------------------------------------------- ping

def tcp_alive(host, port=22, timeout=2):
    """Fallback liveness check when ping binary is unavailable."""
    import socket
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def check_ping(ip):
    if shutil.which("ping"):
        try:
            out = subprocess.run(
                ["ping", "-c", "4", "-W", "2", ip],
                capture_output=True, text=True, timeout=15)
            text = out.stdout + out.stderr
            import re
            recv = re.search(r"(\d+)\s+(?:packets?\s+)?received", text)
            loss = re.search(r"([\d.]+)%\s+packet\s+loss", text)
            if recv and recv.group(1) == "4" and (not loss or float(loss.group(1)) == 0.0):
                ok(f"ping {ip} (4/4 replies, 0% loss)")
                return
            bad(f"ping {ip} (lossy/unreachable)")
            return
        except (OSError, subprocess.TimeoutExpired):
            pass
    # fallback: TCP connect
    if tcp_alive(ip):
        ok(f"ping {ip} (ping unavailable; TCP/22 reachable)")
    else:
        bad(f"ping {ip} (unreachable)")


# ------------------------------------------------------- container APIs

def fetch_json(url, timeout=3):
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return json.loads(r.read().decode())
    except Exception:
        return None


def summarize_containers(items, source):
    """items: list of dicts from a containers/json endpoint."""
    if not items:
        skip(f"{source}: API reachable, no containers")
        return True
    running = sum(1 for c in items if c.get("State") == "running")
    unhealthy = []
    for c in items:
        status = c.get("Status") or ""
        name = (c.get("Names") or ["?"])[0].lstrip("/")
        if "unhealthy" in status.lower():
            unhealthy.append(name)
    for c in items:
        name = (c.get("Names") or ["?"])[0].lstrip("/")
        print(f"      - {name}: {c.get('State','?')} {c.get('Status','')}")
    if unhealthy:
        bad(f"{source}: {len(unhealthy)} unhealthy -> {', '.join(unhealthy)}")
    elif running == len(items):
        ok(f"{source}: {running}/{len(items)} containers running")
    else:
        bad(f"{source}: {running}/{len(items)} containers running")
    return True


def check_container_apis(host, ports):
    found = False
    for port, kind in ((2375, "docker"), (8080, "podman"), (2376, "docker-tls")):
        if port not in ports:
            continue
        url = f"http://{host}:{port}/containers/json?all=1"
        data = fetch_json(url)
        if data is not None:
            found = True
            print(f"    {kind} API on :{port}:")
            summarize_containers(data, f"{kind}:{port}")
    if not found:
        skip(f"containers on {host}: no docker/podman API reachable (tried {sorted(ports)})")


# ------------------------------------------------------------- SSH deep

def ssh_run(ssh_target, remote_cmd, timeout=15):
    try:
        out = subprocess.run(
            ["ssh", "-o", "ConnectTimeout=4", "-o", "BatchMode=yes",
             ssh_target, remote_cmd],
            capture_output=True, text=True, timeout=timeout)
        if out.returncode == 0:
            return out.stdout
        return None
    except (OSError, subprocess.TimeoutExpired):
        return None


def check_via_ssh(ssh_target):
    if not shutil.which("ssh"):
        skip("ssh not installed; deep host checks unavailable")
        return
    # Containers: docker + rootless podman + rootful podman (quadlet)
    cmd = (
        "echo '--- docker ---'; docker ps -a --format '{{.Names}}|{{.Status}}' 2>/dev/null; "
        "echo '--- podman ---'; podman ps -a --format '{{.Names}}|{{.Status}}' 2>/dev/null; "
        "echo '--- podman-root ---'; sudo -n podman ps -a --format '{{.Names}}|{{.Status}}' 2>/dev/null; "
        "echo '--- mem ---'; free -m | awk '/Mem:/{print $2,$3,$7}'; "
        "echo '--- load ---'; cat /proc/loadavg; nproc"
    )
    out = ssh_run(ssh_target, cmd)
    if out is None:
        skip(f"ssh {ssh_target}: unreachable or auth failed")
        return
    section = None
    containers = {"docker": [], "podman": [], "podman-root": []}
    mem_line = load_line = None
    for line in out.splitlines():
        line = line.strip()
        if line.startswith("---"):
            section = line.strip("-").strip()
            continue
        if not line:
            continue
        if section in containers and "|" in line:
            containers[section].append(line)
        elif section == "mem":
            mem_line = line
        elif section == "load":
            load_line = line
    any_cont = False
    for src, items in containers.items():
        if not items:
            continue
        any_cont = True
        print(f"    {src}:")
        bad_count = 0
        for it in items:
            name, _, status = it.partition("|")
            flag = "" if ("Up" in status or "healthy" in status.lower()) else "  <-- not up"
            if flag:
                bad_count += 1
            print(f"      - {name}: {status}{flag}")
        if bad_count:
            bad(f"{src}: {bad_count} container(s) not up/healthy")
        else:
            ok(f"{src}: {len(items)} container(s) up/healthy")
    if not any_cont:
        skip(f"ssh {ssh_target}: no containers listed by docker/podman")
    # memory + cpu
    if mem_line:
        parts = mem_line.split()
        if len(parts) >= 3:
            total, used, avail = int(parts[0]), int(parts[1]), int(parts[2])
            pct = used / total * 100 if total else 0
            status = "OK" if pct < 90 else "WARN"
            (ok if pct < 90 else bad)(
                f"memory: {used}MiB/{total}MiB used ({pct:.0f}%), {avail}MiB available [{status}]")
    if load_line:
        lp = load_line.split()
        try:
            l1 = float(lp[0]); ncpu = int(lp[-1])
            load_per_cpu = l1 / ncpu if ncpu else 0
            (ok if load_per_cpu < 0.9 else bad)(
                f"cpu: load1={l1:.2f} on {ncpu} cores ({load_per_cpu:.2f}/core)")
        except (ValueError, IndexError):
            pass


def main():
    ap = argparse.ArgumentParser(description="Host/network health check (stdlib only)")
    ap.add_argument("--host", required=True, help="docker/podman host IP")
    ap.add_argument("--ping", nargs="+", default=[], help="IPs to ping")
    ap.add_argument("--ssh", default=None, help="ssh target for deep checks")
    ap.add_argument("--api-ports", default="2375,8080",
                    help="comma-separated API ports to probe")
    args = ap.parse_args()

    ports = {int(p) for p in args.api_ports.split(",") if p.strip()}

    print(f"== Host health: {args.host} ==")
    print("-- containers --")
    check_container_apis(args.host, ports)
    print("-- ping --")
    for ip in args.ping:
        check_ping(ip)
    if args.ssh:
        print(f"-- deep checks via ssh {args.ssh} --")
        check_via_ssh(args.ssh)
    else:
        skip("deep host checks (memory/cpu) — pass --ssh to enable")

    print(f"== Result: {passed} ok, {failed} failed, {skipped} skipped ==")
    sys.exit(0 if failed == 0 else 1)


if __name__ == "__main__":
    main()
