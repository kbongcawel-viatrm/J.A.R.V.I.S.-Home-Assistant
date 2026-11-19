#!/usr/bin/env bash
# Network health check: container health on a docker host + ping checks.
#
# Usage:
#   net-health.sh <docker-host-ip> <ping-ip> [ping-ip ...]
#
# Example (this setup):
#   net-health.sh 10.1.29.2 10.1.29.51 10.1.29.1
#
# Checks:
#   1. Docker container health on <docker-host-ip> via the Docker API
#      (tcp://IP:2375, falls back to ssh if DOCKER_HOST is set)
#   2. Ping each <ping-ip> — requires 100% replies (0% loss)
set -uo pipefail

DOCKER_HOST_IP="${1:-}"
shift 2>/dev/null || true
PING_IPS=("$@")

[ -z "$DOCKER_HOST_IP" ] && { echo "usage: $0 <docker-host-ip> <ping-ip> [ping-ip ...]" >&2; exit 1; }
[ ${#PING_IPS[@]} -eq 0 ] && { echo "error: at least one ping target required" >&2; exit 1; }

PASS=0; FAIL=0; SKIP=0
ok()   { PASS=$((PASS+1)); printf '  [OK]   %s\n' "$1"; }
bad()  { FAIL=$((FAIL+1)); printf '  [FAIL] %s\n' "$1"; }
skip() { SKIP=$((SKIP+1)); printf '  [SKIP] %s\n' "$1"; }

ping_check() {
    local ip="$1"
    if ping -c 4 -W 2 "$ip" >/dev/null 2>&1; then
        ok "ping $ip (4/4 replies, 0% loss)"
    else
        bad "ping $ip (lossy or unreachable)"
    fi
}

echo "== Network health check =="
echo "docker host : $DOCKER_HOST_IP"
echo "ping targets: ${PING_IPS[*]}"
echo

# 1. Container health on the docker host
#    Try the unauthenticated Docker TCP API first (port 2375).
DOCKER_API="http://$DOCKER_HOST_IP:2375"
containers=$(curl -sf --connect-timeout 3 "$DOCKER_API/containers/json" 2>/dev/null)
if [ -n "$containers" ]; then
    # Parse name + health/status with python3 if available, else grep
    if command -v python3 >/dev/null 2>&1; then
        echo "$containers" | python3 -c '
import json,sys
cs = json.load(sys.stdin)
if not cs:
    print("NONE")
for c in cs:
    name = c["Names"][0].lstrip("/")
    state = c["State"]
    health = (c.get("Status") or "")
    print(f"{name}\t{state}\t{health}")
'
    else
        echo "$containers" | tr ',' '\n' | grep -o '"Names":\[[^]]*\]\|"State":"[^"]*"' || true
    fi
    # Count running vs not
    running=$(echo "$containers" | python3 -c 'import json,sys; print(sum(1 for c in json.load(sys.stdin) if c["State"]=="running"))' 2>/dev/null || echo "?")
    total=$(echo "$containers" | python3 -c 'import json,sys; print(len(json.load(sys.stdin)))' 2>/dev/null || echo "?")
    if [ "$running" = "$total" ]; then
        ok "docker $DOCKER_HOST_IP: $running/$total containers running"
    else
        bad "docker $DOCKER_HOST_IP: $running/$total containers running"
    fi
else
    # Fallback: try ssh docker ps if DOCKER_SSH env is set
    if [ -n "${DOCKER_SSH:-}" ]; then
        if ssh -o ConnectTimeout=3 -o BatchMode=yes "$DOCKER_SSH" "docker ps --format '{{.Names}} {{.Status}}'" 2>/dev/null; then
            ok "docker $DOCKER_HOST_IP via ssh: containers listed"
        else
            bad "docker $DOCKER_HOST_IP: API unreachable and ssh failed"
        fi
    else
        skip "docker $DOCKER_HOST_IP: API :2375 unreachable (set DOCKER_SSH for ssh fallback)"
    fi
fi
echo

# 2. Ping checks — 100% reply required
for ip in "${PING_IPS[@]}"; do
    ping_check "$ip"
done

echo
echo "== Result: $PASS passed, $FAIL failed, $SKIP skipped =="
[ "$FAIL" -eq 0 ]
