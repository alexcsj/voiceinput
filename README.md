# voiceinput

GNOME Shell 擴充功能：在 top panel 常駐一個麥克風按鈕，點擊（或按 **Ctrl+Super+V**）開始持續聆聽語音輸入，說話停頓時自動分段送到雲端語音辨識服務轉成文字，即時貼到目前焦點視窗，行為類似 OK Google / Siri 的持續聆聽模式。再按一次結束。

支援三種辨識服務：**Groq**（`whisper-large-v3`）、**Google Cloud Speech-to-Text**、**Grok (xAI)**，可以在面板選單即時切換；同一個選單也能切換辨識語言：繁體中文、簡體中文、中英混雜、純英文、日文。

## 功能特色

- **持續聆聽、自動斷句**：不用每句話都手動按鍵，用簡單的音量門檻做語音活動偵測（VAD），偵測到停頓就自動把這段語音切開送出，並保留 300ms 的 pre-roll 緩衝避免漏字
- **三種服務商可切換**：Groq Whisper（速度快）、Google Cloud STT（可直接指定輸出腳本）、Grok xAI（2026 年 4 月才推出的獨立 STT API）之間隨時切換，第一次選某個服務商如果還沒設定 API key，會跳出對話框讓你直接貼上
- **繁簡分離**：Groq／Grok 用 OpenCC（`s2twp`/`tw2sp`）把輸出強制轉成台灣慣用繁體或大陸標準簡體，不只轉字形也轉詞彙（軟體/软件、網路/网络）；Google STT 用 BCP-47 語言代碼（`zh-TW`/`zh-CN`）直接指定腳本
- **幻覺過濾**：生成式 STT 對靜音/雜訊常會幻覺出訓練資料裡的影片結尾套語（英文「you」「thank you for watching」、中文「感谢观看」、日文「ご視聴ありがとうございました」等）。Groq 有 `no_speech_prob`/`avg_logprob` 信心分數可以用，加上已知樣板黑名單雙重過濾；Grok（xAI）的回應沒有信心分數，只能靠黑名單防線；Google STT 是判別式模型，天生不太會有這個問題
- **面板圖示即時反映狀態**：閒置／錄音中（紅色脈動）／辨識中（黃色旋轉），跟按鍵盤快捷鍵或點面板圖示觸發的動作完全同步

## 運作原理

1. `bin/voice-input-daemon.py`：Python daemon，用 `pw-record` 持續讀取麥克風原始 PCM，自己做 VAD 斷句，每切出一段語音就丟給對應服務商的 API 轉文字，成功後複製到剪貼簿並用 `ydotool` 模擬 Ctrl+V 貼上
2. `bin/voice-input-toggle.sh`：GNOME 快捷鍵呼叫的進入點，第一次執行背景啟動 daemon，第二次執行送 SIGTERM 讓 daemon 處理完最後一段語音再結束
3. `voice-input@csj1980.local/`：GNOME Shell 擴充功能本體，面板圖示 + 右鍵選單（語言/服務商切換、開啟設定資料夾），監看 daemon 寫的狀態檔即時更新圖示

## 安裝

### 相依套件

- `pw-record`（PipeWire）
- `wl-copy`（wl-clipboard，Wayland 剪貼簿）
- `ydotool` + `ydotoold` 背景服務（模擬 Ctrl+V 貼上，需要另外設定 `ydotoold` 開機啟動，各發行版方式不同，請參考 [ydotool 官方文件](https://github.com/ReimuNotMoe/ydotool)）
- `python3`、`python3-requests`、`python3-opencc`（繁簡轉換，中文模式需要）
- `notify-send`（libnotify，通知用）

| 發行版 | 安裝指令 |
|---|---|
| Arch / Manjaro | `sudo pacman -S pipewire wl-clipboard ydotool python python-requests opencc libnotify` |
| Ubuntu 24.04+ / Debian 13+ | `sudo apt install pipewire-bin wl-clipboard ydotool python3 python3-requests python3-opencc libnotify-bin` |
| Fedora | `sudo dnf install pipewire-utils wl-clipboard ydotool python3 python3-requests python3-opencc libnotify` |

> 目前僅支援 **Wayland** session（用 `wl-copy` 操作剪貼簿）。

> 這個 repo 目前是 **private**，`curl | bash` 一鍵安裝連結需要能公開存取才有用；
> 在轉成 public 之前請用下面 clone 的方式安裝（`git clone` 私有 repo 需要你自己
> 已經有存取權限，例如已用 `gh auth login` 登入或設定過 SSH key）。

```bash
git clone https://github.com/alexcsj/voiceinput.git
cd voiceinput
./install.sh
```

`install.sh` 會：檢查相依套件（缺少的話列出對應發行版安裝指令，不會自動 `sudo` 幫你裝）、把擴充功能複製到 `~/.local/share/gnome-shell/extensions/voice-input@csj1980.local`、把兩支背景腳本複製到 `~/.local/bin/`、設定 **Ctrl+Super+V** 快捷鍵（會找一個沒用過的 `customN` 插槽，不會覆蓋你既有的其他自訂快捷鍵；如果偵測到已經設定過會跳過）。

### 啟用

**第一次安裝**：GNOME Shell 需要重新掃描擴充功能目錄才能看到新裝的擴充功能，請**登出再登入**（或重開機）一次，再執行：

```bash
gnome-extensions enable voice-input@csj1980.local
```

之後要更新版本（重新執行 `install.sh` 或 `git pull`）：如果只有 `bin/` 裡的腳本改了，不用重新登入，下次觸發就會套用新版；如果 `voice-input@csj1980.local/` 裡的 `extension.js` 改了，需要登出登入才會生效（Wayland 底下 GNOME Shell 沒有像 X11 的 Alt+F2 `r` 可以熱重載擴充功能）。

### 設定 API key

點面板圖示右鍵選單裡的「開啟設定資料夾…」，或直接選一個還沒設定過的服務商，會跳出對話框請你貼上 API key：

- **Groq**：[console.groq.com/keys](https://console.groq.com/keys) 建立
- **Google Cloud STT**：到 [Google Cloud Console](https://console.cloud.google.com/apis/credentials) 啟用「Cloud Speech-to-Text API」後，建立**「API 金鑰」**（注意不是「OAuth 用戶端 ID」，那是完全不同的認證方式，兩者很容易搞混），每月有 60 分鐘免費額度
- **Grok (xAI)**：到 [console.x.ai](https://console.x.ai) 的 API Keys 頁面建立（`xai-` 開頭），批次轉錄 $0.10/小時

金鑰存在 `~/.config/voice-input/{groq,google,grok}_api_key`，權限 600 只有自己能讀。

## 操作方式

- **左鍵點面板圖示 / Ctrl+Super+V**：開始／結束持續聆聽
- **右鍵點面板圖示**：彈出選單，切換辨識語言、切換服務商、開啟設定資料夾
- **中鍵點面板圖示**：直接開啟設定資料夾

## 已知限制

- 只支援 Wayland
- `auto`（中英混雜）模式在 Google STT 那邊是用「主要語言＋備選語言」近似（`zh-TW` + `en-US`），不是真正的自動語言偵測；Groq/Whisper 跟 Grok/xAI 則是不指定語言參數讓模型自行判斷，逐句斷句的情況下通常效果不錯
- 沒有做 Anthropic 的語音轉文字選項——查證過 Anthropic 目前沒有公開的語音轉文字 API 可以串接，Claude Code 裡的語音輸入是內部服務，只認 Claude.ai 帳號登入
