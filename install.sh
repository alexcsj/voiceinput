#!/usr/bin/env bash
# voiceinput 安裝腳本
# 用法：
#   git clone https://github.com/alexcsj/voiceinput.git && cd voiceinput && ./install.sh
# 或不 clone，直接：
#   curl -fsSL https://raw.githubusercontent.com/alexcsj/voiceinput/main/install.sh | bash

set -euo pipefail

UUID="voice-input@csj1980.local"
REPO_TARBALL_URL="https://codeload.github.com/alexcsj/voiceinput/tar.gz/refs/heads/main"
EXTENSIONS_DIR="$HOME/.local/share/gnome-shell/extensions"
TARGET_DIR="$EXTENSIONS_DIR/$UUID"
BIN_DIR="$HOME/.local/bin"
CONFIG_DIR="$HOME/.config/voice-input"

log() { printf '==> %s\n' "$1"; }
warn() { printf '!! %s\n' "$1" >&2; }

# --- 1. 找出原始檔的位置 --------------------------------------------------
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" && pwd)"

if [ -d "$SCRIPT_DIR/$UUID" ] && [ -d "$SCRIPT_DIR/bin" ]; then
    # 從已 clone 的 repo 內執行
    SOURCE_ROOT="$SCRIPT_DIR"
    CLEANUP_DIR=""
else
    # 透過 curl | bash 執行，沒有本機原始碼，先下載一份到暫存目錄
    if ! command -v curl >/dev/null 2>&1; then
        warn "找不到 curl，請先安裝 curl 再重試。"
        exit 1
    fi
    TMP_DIR="$(mktemp -d)"
    CLEANUP_DIR="$TMP_DIR"
    trap '[ -n "$CLEANUP_DIR" ] && rm -rf "$CLEANUP_DIR"' EXIT

    log "下載 voiceinput 原始碼..."
    curl -fsSL "$REPO_TARBALL_URL" -o "$TMP_DIR/repo.tar.gz"
    tar -xzf "$TMP_DIR/repo.tar.gz" -C "$TMP_DIR"
    SOURCE_ROOT="$(find "$TMP_DIR" -mindepth 1 -maxdepth 1 -type d -name 'voiceinput-*')"

    if [ ! -d "$SOURCE_ROOT/$UUID" ]; then
        warn "下載的內容裡找不到 $UUID/，安裝中止。"
        exit 1
    fi
fi

# --- 2. 檢查相依套件 ---------------------------------------------------
log "檢查相依套件..."

MISSING=0
check_cmd() {
    if ! command -v "$1" >/dev/null 2>&1; then
        warn "找不到指令：$1"
        MISSING=1
    fi
}

check_cmd pw-record
check_cmd wl-copy
check_cmd ydotool
check_cmd python3
check_cmd notify-send

PY_MISSING=0
if command -v python3 >/dev/null 2>&1; then
    python3 -c "import requests" >/dev/null 2>&1 || { warn "缺少 python3 的 requests 套件"; PY_MISSING=1; }
    python3 -c "import opencc" >/dev/null 2>&1 || { warn "缺少 python3 的 opencc 套件（繁簡轉換用，中文模式需要）"; PY_MISSING=1; }
fi

if [ "$MISSING" = "1" ] || [ "$PY_MISSING" = "1" ]; then
    echo
    warn "缺少必要相依套件，這個工具需要：pw-record（PipeWire）、wl-copy（wl-clipboard）、"
    warn "ydotool（含 ydotoold 背景服務）、python3、python3-requests、python3-opencc、libnotify。"
    echo
    echo "  Arch / Manjaro:"
    echo "    sudo pacman -S pipewire wl-clipboard ydotool python python-requests opencc libnotify"
    echo "  Ubuntu 24.04+ / Debian 13+:"
    echo "    sudo apt install pipewire-bin wl-clipboard ydotool python3 python3-requests python3-opencc libnotify-bin"
    echo "  Fedora:"
    echo "    sudo dnf install pipewire-utils wl-clipboard ydotool python3 python3-requests python3-opencc libnotify"
    echo
    warn "ydotool 還需要 ydotoold 背景服務跑起來才能模擬按鍵（貼上時要用），"
    warn "各發行版設定方式不同，請參考 ydotool 官方文件啟用對應的 systemd service。"
    echo
    warn "請安裝好相依套件後重新執行本腳本。"
    exit 1
fi

log "相依套件 OK"

# --- 3. 安裝 GNOME Shell 擴充功能 ------------------------------------------
mkdir -p "$EXTENSIONS_DIR"

if [ -e "$TARGET_DIR" ] || [ -L "$TARGET_DIR" ]; then
    log "偵測到舊版本，先移除 $TARGET_DIR"
    rm -rf "$TARGET_DIR"
fi

log "安裝擴充功能到 $TARGET_DIR"
cp -r "$SOURCE_ROOT/$UUID" "$TARGET_DIR"

# --- 4. 安裝背景腳本 --------------------------------------------------------
mkdir -p "$BIN_DIR"
log "安裝背景腳本到 $BIN_DIR"
cp "$SOURCE_ROOT/bin/voice-input-toggle.sh" "$BIN_DIR/voice-input-toggle.sh"
cp "$SOURCE_ROOT/bin/voice-input-daemon.py" "$BIN_DIR/voice-input-daemon.py"
chmod +x "$BIN_DIR/voice-input-toggle.sh" "$BIN_DIR/voice-input-daemon.py"

mkdir -p "$CONFIG_DIR"
chmod 700 "$CONFIG_DIR"

# --- 5. 設定 Ctrl+Super+V 快捷鍵 --------------------------------------------
log "設定 Ctrl+Super+V 快捷鍵..."

MK_SCHEMA="org.gnome.settings-daemon.plugins.media-keys"
BASE_PATH="/org/gnome/settings-daemon/plugins/media-keys/custom-keybindings"
TARGET_COMMAND="$BIN_DIR/voice-input-toggle.sh"

EXISTING="$(gsettings get "$MK_SCHEMA" custom-keybindings 2>/dev/null || echo "[]")"

# custom-keybindings 存的是 dconf 路徑陣列（例如 ['.../custom0/']），
# 要逐一查詢每個路徑的 command 屬性才知道是不是已經指到我們的腳本，
# 不能直接對這個陣列字串本身找字串（裡面本來就不會有指令內容）
ALREADY_SET=0
for existing_path in $(echo "$EXISTING" | grep -oE "$BASE_PATH/custom[0-9]+/"); do
    existing_cmd="$(gsettings get "$MK_SCHEMA.custom-keybinding:$existing_path" command 2>/dev/null | sed "s/^'//; s/'$//")"
    if [ "$existing_cmd" = "$TARGET_COMMAND" ]; then
        ALREADY_SET=1
        break
    fi
done

if [ "$ALREADY_SET" = "0" ]; then
    # 找出下一個沒用過的 customN 編號，不要覆蓋使用者其他的自訂快捷鍵
    N=0
    while echo "$EXISTING" | grep -q "custom$N/"; do
        N=$((N + 1))
    done
    NEW_PATH="$BASE_PATH/custom$N/"

    if [ "$EXISTING" = "[]" ] || [ "$EXISTING" = "@as []" ]; then
        NEW_ARRAY="['$NEW_PATH']"
    else
        # 用 # 當 sed 分隔符，因為 $NEW_PATH 裡有斜線，用預設的 / 會炸開
        NEW_ARRAY="$(echo "$EXISTING" | sed "s#\]\$#, '$NEW_PATH']#")"
    fi

    gsettings set "$MK_SCHEMA" custom-keybindings "$NEW_ARRAY"
    gsettings set "$MK_SCHEMA.custom-keybinding:$NEW_PATH" name "語音輸入切換"
    gsettings set "$MK_SCHEMA.custom-keybinding:$NEW_PATH" command "$TARGET_COMMAND"
    gsettings set "$MK_SCHEMA.custom-keybinding:$NEW_PATH" binding "<Control><Super>v"

    log "已將 Ctrl+Super+V 綁定到 voice-input-toggle.sh（$NEW_PATH）"
else
    log "偵測到已經有快捷鍵指到 voice-input-toggle.sh，略過設定"
fi

echo
log "安裝完成！"
echo
if command -v gnome-extensions >/dev/null 2>&1 && gnome-extensions list 2>/dev/null | grep -qx "$UUID"; then
    echo "已偵測到 GNOME Shell 認得這個擴充功能，執行以下指令啟用："
    echo "  gnome-extensions enable $UUID"
else
    echo "第一次安裝：GNOME Shell 需要重新掃描擴充功能目錄才能看到新裝的擴充功能。"
    echo "請「登出後再登入」（或重新開機），再執行："
    echo "  gnome-extensions enable $UUID"
fi
echo
echo "啟用後，點面板上的麥克風圖示或按 Ctrl+Super+V 第一次使用時，"
echo "會跳出對話框請你輸入 Groq 或 Google Cloud Speech-to-Text 的 API key。"
echo "設定檔資料夾：$CONFIG_DIR"
