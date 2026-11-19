#!/usr/bin/env bash
# Manage the local voice assistant stack:
#   whisper-server (whisper.cpp STT) + ollama (LLM) + voice_assistant.py
set -euo pipefail

SCRIPT_NAME=$(basename -- "$0")
SCRIPT_DIR=$(cd -- "$(dirname -- "$0")" && pwd)
REPO_DIR=$(cd -- "$SCRIPT_DIR/.." && pwd)
WHISPER_CPP_DIR="$HOME/whisper.cpp"
VENV_PY="$REPO_DIR/whisper_env/bin/python"
ASSISTANT_PY="$SCRIPT_DIR/voice_assistant.py"

WHISPER_PORT=8178
OLLAMA_PORT=11434
WHISPER_MODEL="${WHISPER_MODEL:-ggml-base.bin}"
OLLAMA_MODEL="${OLLAMA_MODEL:-qwen2.5:1.5b}"
MEM_LIMIT="${MEM_LIMIT:-1.5G}"

LOG_DIR="${XDG_RUNTIME_DIR:-/tmp}/voice-assistant"
mkdir -p "$LOG_DIR"

log() { echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*"; }
die() { log "ERROR: $*" >&2; exit 1; }

usage() {
    cat <<EOF
Usage: $SCRIPT_NAME <command>

Commands:
    start    Start whisper-server, ollama (capped), and the voice assistant
    stop     Stop all components
    status   Show which components are running
    logs     Tail assistant log

Environment overrides:
    WHISPER_MODEL   ggml model file (default: $WHISPER_MODEL)
    OLLAMA_MODEL    ollama model (default: $OLLAMA_MODEL)
    MEM_LIMIT       ollama memory cap (default: $MEM_LIMIT)
    STT             whispercpp|faster-whisper (default: whispercpp)
    TTS             piper|kokoro (default: piper)
EOF
}

port_open() { ss -tlnH "sport = :$1" 2>/dev/null | grep -q .; }

whisper_pid() { pgrep -f "whisper-server.*--port $WHISPER_PORT" | head -1 || true; }
ollama_pid() { pgrep -x ollama | head -1 || true; }
assistant_pid() { pgrep -f "voice_assistant.py" | head -1 || true; }

cmd_start() {
    log "Starting voice assistant stack"

    if [ -n "$(whisper_pid)" ]; then
        log "whisper-server already running (pid $(whisper_pid))"
    else
        [ -x "$WHISPER_CPP_DIR/build/bin/whisper-server" ] \
            || die "whisper-server not built at $WHISPER_CPP_DIR"
        [ -f "$WHISPER_CPP_DIR/models/$WHISPER_MODEL" ] \
            || die "model not found: $WHISPER_CPP_DIR/models/$WHISPER_MODEL"
        (cd "$WHISPER_CPP_DIR" && nohup ./build/bin/whisper-server \
            -m "models/$WHISPER_MODEL" --host 127.0.0.1 --port "$WHISPER_PORT" -l en \
            > "$LOG_DIR/whisper-server.log" 2>&1 &)
        log "whisper-server starting (model: $WHISPER_MODEL, port: $WHISPER_PORT)"
    fi

    if [ -n "$(ollama_pid)" ] || port_open "$OLLAMA_PORT"; then
        log "ollama already running"
    else
        command -v ollama >/dev/null || die "ollama not found in PATH"
        nohup systemd-run --user --scope --unit=ollama-capped \
            -p "MemoryMax=$MEM_LIMIT" ollama serve \
            > "$LOG_DIR/ollama.log" 2>&1 &
        log "ollama starting (cap: $MEM_LIMIT)"
    fi

    if [ -n "$(assistant_pid)" ]; then
        log "voice_assistant already running (pid $(assistant_pid))"
        return 0
    fi
    [ -x "$VENV_PY" ] || die "venv python not found: $VENV_PY"
    [ -f "$WHISPER_CPP_DIR/models/$WHISPER_MODEL" ] || true

    nohup "$VENV_PY" "$ASSISTANT_PY" \
        --stt "${STT:-whispercpp}" --stt-url "http://localhost:$WHISPER_PORT" \
        --ollama-model "$OLLAMA_MODEL" --tts "${TTS:-piper}" \
        > "$LOG_DIR/assistant.log" 2>&1 &
    log "voice_assistant starting (pid $!, ollama model: $OLLAMA_MODEL)"
    log "Tail logs with: $0 logs"
}

cmd_stop() {
    log "Stopping voice assistant stack"
    pkill -f "voice_assistant.py" 2>/dev/null && log "stopped voice_assistant" || true
    pkill -f "whisper-server.*--port $WHISPER_PORT" 2>/dev/null \
        && log "stopped whisper-server" || true
    if systemctl --user list-units --type=scope 2>/dev/null | grep -q ollama-capped; then
        systemctl --user stop ollama-capped.scope 2>/dev/null && log "stopped ollama (scope)" || true
    else
        pkill -x ollama 2>/dev/null && log "stopped ollama" || true
    fi
    pkill -f "llama-server" 2>/dev/null || true
    pkill -x paplay -x aplay -x ffplay -x piper 2>/dev/null || true
    log "done"
}

cmd_status() {
    echo "whisper-server :8178 : $([ -n "$(whisper_pid)" ] && echo "running (pid $(whisper_pid))" || echo "stopped")"
    echo "ollama        :11434 : $([ -n "$(ollama_pid)" ] && echo "running (pid $(ollama_pid))" || echo "stopped")"
    echo "assistant            : $([ -n "$(assistant_pid)" ] && echo "running (pid $(assistant_pid))" || echo "stopped")"
}

cmd_logs() {
    tail -f "$LOG_DIR/assistant.log"
}

case "${1:-}" in
    start)  cmd_start ;;
    stop)   cmd_stop ;;
    status) cmd_status ;;
    logs)   cmd_logs ;;
    -h|--help|"") usage; [ -z "${1:-}" ] && exit 1 || exit 0 ;;
    *) usage >&2; die "unknown command: $1" ;;
esac
