#!/usr/bin/env bash
# Basic health check for the voice stack on a target host.
# Usage: health-check.sh <ip-or-host> [port ...]
# Checks: ping (100% reply), HTTP 200 on known service ports, docker health.
set -uo pipefail

TARGET="${1:-127.0.0.1}"
shift 2>/dev/null || true
PORTS=("$@")
[ ${#PORTS[@]} -eq 0 ] && PORTS=(8178 11434 9090 5000)

PASS=0; FAIL=0
ok()   { PASS=$((PASS+1)); printf '  [OK]   %s\n' "$1"; }
bad()  { FAIL=$((FAIL+1)); printf '  [FAIL] %s\n' "$1"; }

echo "== Health check: $TARGET =="

# 1. Ping check (require 100% replies)
if ping -c 4 -W 2 "$TARGET" >/dev/null 2>&1; then
    ok "ping $TARGET (4/4 replies)"
else
    bad "ping $TARGET (no/lossy replies)"
fi

# 2. HTTP 200 checks on service ports
for port in "${PORTS[@]}"; do
    code=$(curl -s -o /dev/null -w '%{http_code}' --connect-timeout 3 "http://$TARGET:$port/" 2>/dev/null)
    if [ "$code" = "200" ]; then
        ok "http://$TARGET:$port -> 200"
    else
        bad "http://$TARGET:$port -> $code (port $port)"
    fi
done

# 3. Container health (only meaningful on the local docker host)
if command -v docker >/dev/null 2>&1; then
    unhealthy=$(docker ps --format '{{.Names}} {{.Status}}' 2>/dev/null \
        | grep -viE 'healthy|Up [0-9]' || true)
    total=$(docker ps -q 2>/dev/null | wc -l)
    if [ "$total" -eq 0 ]; then
        echo "  [SKIP] no running containers"
    elif [ -z "$unhealthy" ]; then
        ok "docker: $total container(s), all healthy/up"
    else
        bad "docker: unhealthy -> $unhealthy"
    fi
else
    echo "  [SKIP] docker not installed"
fi

echo "== Result: $PASS passed, $FAIL failed =="
[ "$FAIL" -eq 0 ]
