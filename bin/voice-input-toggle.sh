#!/usr/bin/env bash
set -euo pipefail

export YDOTOOL_SOCKET="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}/.ydotool_socket"

STATE_DIR="${XDG_RUNTIME_DIR:-/tmp}/voice-input"
PID_FILE="$STATE_DIR/record.pid"
PROCESSING_FILE="$STATE_DIR/processing"
DAEMON="$HOME/.local/bin/voice-input-daemon.py"
LOG_FILE="$STATE_DIR/daemon.log"

mkdir -p "$STATE_DIR"

if [[ -f "$PID_FILE" ]]; then
    # 第二次按下:結束持續聆聽(daemon 會先處理完最後一段語音再結束)
    DAEMON_PID="$(cat "$PID_FILE")"
    rm -f "$PID_FILE"
    touch "$PROCESSING_FILE"
    trap 'rm -f "$PROCESSING_FILE"' EXIT

    if kill -0 "$DAEMON_PID" 2>/dev/null; then
        kill -TERM "$DAEMON_PID" 2>/dev/null || true
        # 最多等 15 秒讓最後一段語音轉完文字並貼上；用 0.02 秒輪詢
        # (跟音框長度一樣)而不是 0.1 秒，沒語音要處理時關閉反應才夠快
        for _ in $(seq 1 750); do
            kill -0 "$DAEMON_PID" 2>/dev/null || break
            sleep 0.02
        done
        if kill -0 "$DAEMON_PID" 2>/dev/null; then
            kill -KILL "$DAEMON_PID" 2>/dev/null || true
        fi
    fi

    notify-send -t 1500 "語音輸入" "⏹️ 已結束聆聽" 2>/dev/null || true
else
    # 第一次按下:開始持續聆聽,自動偵測停頓分段轉文字、逐段貼上。
    # API key 存不存在是由 daemon.py 自己依目前選的服務商檢查、缺少時
    # 會自己跳通知並結束(不會寫 PID_FILE)，這裡不重複檢查，避免寫死
    # 只認 Groq 的 key（現在可能是用 Google）。
    rm -f "$PROCESSING_FILE"

    # 不要用 `echo $!` 記錄 PID：setsid 在 bash 背景工作(已經是 process
    # group leader)底下會自己 fork 一次，`$!` 抓到的其實是 setsid 那個
    # 馬上就結束的過渡行程，不是真正跑起來的 daemon，之後送 SIGTERM 會
    # 打不到人，daemon 變孤兒繼續在背景聽下去。改成讓 daemon.py 自己在
    # main() 一開始用 os.getpid() 把真正的 PID 寫進 PID_FILE。
    setsid python3 "$DAEMON" < /dev/null > "$LOG_FILE" 2>&1 &
fi
