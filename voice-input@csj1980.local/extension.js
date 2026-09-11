import Clutter from 'gi://Clutter';
import Gio from 'gi://Gio';
import GLib from 'gi://GLib';
import St from 'gi://St';

import * as Main from 'resource:///org/gnome/shell/ui/main.js';
import * as PanelMenu from 'resource:///org/gnome/shell/ui/panelMenu.js';
import * as PopupMenu from 'resource:///org/gnome/shell/ui/popupMenu.js';
import * as ModalDialog from 'resource:///org/gnome/shell/ui/modalDialog.js';
import { Extension } from 'resource:///org/gnome/shell/extensions/extension.js';

const PULSE_MS = 650;
const SPIN_MS = 900;

const LANG_MODES = ['zh-hant', 'zh-hans', 'auto', 'en', 'ja'];
const LANG_BADGES = { 'zh-hant': '繁', 'zh-hans': '簡', auto: '中英', en: 'EN', ja: '日' };
const LANG_NAMES = { 'zh-hant': '繁體中文', 'zh-hans': '簡體中文', auto: '中英混雜', en: '純英文', ja: '日文' };

const PROVIDER_MODES = ['groq', 'google', 'grok'];
const PROVIDER_NAMES = { groq: 'Groq (Whisper)', google: 'Google Cloud STT', grok: 'Grok (xAI)' };
const PROVIDER_KEY_FILES = { groq: 'groq_api_key', google: 'google_api_key', grok: 'grok_api_key' };
const PROVIDER_KEY_HELP = {
    groq: '到 console.groq.com/keys 建立 API key',
    google: '到 Google Cloud Console 啟用 Cloud Speech-to-Text API 後，\n在憑證頁面建立「API 金鑰」（不是 OAuth 用戶端 ID）',
    grok: '到 console.x.ai 的 API Keys 頁面建立 key（xai- 開頭）',
};

export default class VoiceInputExtension extends Extension {
    enable() {
        const runtimeDir = GLib.getenv('XDG_RUNTIME_DIR') || '/tmp';
        this._stateDir = GLib.build_filenamev([runtimeDir, 'voice-input']);
        this._pidFilePath = GLib.build_filenamev([this._stateDir, 'record.pid']);
        this._processingFilePath = GLib.build_filenamev([this._stateDir, 'processing']);
        this._configDir = GLib.build_filenamev([GLib.get_home_dir(), '.config', 'voice-input']);
        this._scriptPath = GLib.build_filenamev([GLib.get_home_dir(), '.local', 'bin', 'voice-input-toggle.sh']);
        this._langConfigPath = GLib.build_filenamev([this._configDir, 'language']);
        this._providerConfigPath = GLib.build_filenamev([this._configDir, 'provider']);

        this._state = null;
        this._monitor = null;
        this._langMode = this._loadLangMode();
        this._providerMode = this._loadProviderMode();

        this._indicator = new PanelMenu.Button(0.0, '語音輸入', true);

        const box = new St.BoxLayout({ style_class: 'voice-input-box' });
        this._icon = new St.Icon({
            icon_name: 'audio-input-microphone-symbolic',
            style_class: 'voice-input-icon',
            icon_size: 16,
        });
        this._langBadge = new St.Label({
            style_class: 'voice-input-lang-badge',
            y_align: Clutter.ActorAlign.CENTER,
        });
        box.add_child(this._icon);
        box.add_child(this._langBadge);
        this._indicator.add_child(box);
        this._updateLangBadge();

        this._langMenu = new PopupMenu.PopupMenu(this._indicator, 0.5, St.Side.TOP);
        this._langMenu.actor.add_style_class_name('voice-input-lang-menu');
        Main.uiGroup.add_child(this._langMenu.actor);
        this._langMenu.actor.hide();
        // 用專屬的 PopupMenuManager，不要掛在 Main.panel.menuManager 這個
        // 全面板共用的管理器上——共用管理器會讓「已經有一個選單開著時，
        // 滑鼠移到任何掛在同一個 manager 的圖示上就自動切換打開它的選單」
        // 這種選單列 hover 行為套用到我們身上，只是把滑鼠移過去(不用點)
        // 就會彈出選單，擋住左鍵的操作。專屬管理器讓這個 hover 切換行為
        // 只在自己管理的選單之間生效(目前只有一個，等於不會誤觸發)。
        this._menuManager = new PopupMenu.PopupMenuManager(this._indicator);
        this._menuManager.addMenu(this._langMenu);
        this._buildLangMenu();

        this._langMenu.addMenuItem(new PopupMenu.PopupSeparatorMenuItem());
        this._buildProviderMenu();

        const configItem = new PopupMenu.PopupMenuItem('開啟設定資料夾…');
        configItem.connect('activate', () => this._openConfigFolder());
        this._langMenu.addMenuItem(new PopupMenu.PopupSeparatorMenuItem());
        this._langMenu.addMenuItem(configItem);

        this._indicator.connect('button-press-event', (actor, event) => {
            const button = event.get_button();
            if (button === Clutter.BUTTON_PRIMARY) {
                this._runToggleScript();
                return Clutter.EVENT_STOP;
            } else if (button === Clutter.BUTTON_SECONDARY) {
                this._langMenu.toggle();
                return Clutter.EVENT_STOP;
            } else if (button === Clutter.BUTTON_MIDDLE) {
                this._openConfigFolder();
                return Clutter.EVENT_STOP;
            }
            return Clutter.EVENT_PROPAGATE;
        });

        Main.panel.addToStatusArea(this.uuid, this._indicator, 1, 'right');

        this._applyState('idle');
        this._setupStateWatcher();
    }

    _loadLangMode() {
        try {
            const [, contents] = GLib.file_get_contents(this._langConfigPath);
            let mode = new TextDecoder('utf-8').decode(contents).trim();
            if (mode === 'zh') mode = 'zh-hant'; // 舊版設定檔相容（拆分簡繁之前只有單一 zh）
            if (LANG_MODES.includes(mode)) return mode;
        } catch (e) {
            // 設定檔還不存在，用預設值
        }
        return 'zh-hant';
    }

    _saveLangMode(mode) {
        try {
            GLib.mkdir_with_parents(this._configDir, 0o700);
            GLib.file_set_contents(this._langConfigPath, mode);
        } catch (e) {
            logError(e, 'voice-input: 無法儲存語言模式');
        }
    }

    _buildLangMenu() {
        this._langMenuItems = {};
        for (const mode of LANG_MODES) {
            const item = new PopupMenu.PopupMenuItem(LANG_NAMES[mode]);
            item.setOrnament(mode === this._langMode ? PopupMenu.Ornament.DOT : PopupMenu.Ornament.NONE);
            item.connect('activate', () => this._selectLangMode(mode));
            this._langMenu.addMenuItem(item);
            this._langMenuItems[mode] = item;
        }
    }

    _selectLangMode(mode) {
        if (mode === this._langMode) return;
        this._langMode = mode;
        this._saveLangMode(mode);
        this._updateLangBadge();
        for (const [m, item] of Object.entries(this._langMenuItems)) {
            item.setOrnament(m === mode ? PopupMenu.Ornament.DOT : PopupMenu.Ornament.NONE);
        }
        Main.notify('語音輸入', `辨識語言已切換為：${LANG_NAMES[mode]}（下次開始聆聽時生效）`);
    }

    _updateLangBadge() {
        this._langBadge.text = LANG_BADGES[this._langMode];
    }

    _loadProviderMode() {
        try {
            const [, contents] = GLib.file_get_contents(this._providerConfigPath);
            const mode = new TextDecoder('utf-8').decode(contents).trim();
            if (PROVIDER_MODES.includes(mode)) return mode;
        } catch (e) {
            // 設定檔還不存在，用預設值
        }
        return 'groq';
    }

    _saveProviderMode(mode) {
        try {
            GLib.mkdir_with_parents(this._configDir, 0o700);
            GLib.file_set_contents(this._providerConfigPath, mode);
        } catch (e) {
            logError(e, 'voice-input: 無法儲存辨識服務設定');
        }
    }

    _buildProviderMenu() {
        this._providerMenuItems = {};
        for (const mode of PROVIDER_MODES) {
            const item = new PopupMenu.PopupMenuItem(PROVIDER_NAMES[mode]);
            item.setOrnament(mode === this._providerMode ? PopupMenu.Ornament.DOT : PopupMenu.Ornament.NONE);
            item.connect('activate', () => this._selectProviderMode(mode));
            this._langMenu.addMenuItem(item);
            this._providerMenuItems[mode] = item;
        }
    }

    _selectProviderMode(mode) {
        if (mode === this._providerMode) return;
        if (!this._hasApiKey(mode)) {
            this._promptForApiKey(mode);
            return;
        }
        this._applyProviderMode(mode);
    }

    _applyProviderMode(mode) {
        this._providerMode = mode;
        this._saveProviderMode(mode);
        for (const [m, item] of Object.entries(this._providerMenuItems)) {
            item.setOrnament(m === mode ? PopupMenu.Ornament.DOT : PopupMenu.Ornament.NONE);
        }
        Main.notify('語音輸入', `辨識服務已切換為：${PROVIDER_NAMES[mode]}（下次開始聆聽時生效）`);
    }

    _keyFilePath(mode) {
        return GLib.build_filenamev([this._configDir, PROVIDER_KEY_FILES[mode]]);
    }

    _hasApiKey(mode) {
        try {
            const [, contents] = GLib.file_get_contents(this._keyFilePath(mode));
            return new TextDecoder('utf-8').decode(contents).trim().length > 0;
        } catch (e) {
            return false;
        }
    }

    _saveApiKey(mode, key) {
        try {
            GLib.mkdir_with_parents(this._configDir, 0o700);
            const file = Gio.File.new_for_path(this._keyFilePath(mode));
            const bytes = new TextEncoder().encode(key);
            // Gio.FileCreateFlags.PRIVATE 讓檔案一建立就是只有自己能讀
            // （比照既有 API key 檔案 600 的權限慣例），不要用
            // GLib.file_set_contents，那個是照 umask 走，通常會是 644。
            file.replace_contents(bytes, null, false, Gio.FileCreateFlags.PRIVATE, null);
        } catch (e) {
            logError(e, 'voice-input: 無法儲存 API key');
        }
    }

    _promptForApiKey(mode) {
        const dialog = new ModalDialog.ModalDialog({ styleClass: 'voice-input-key-dialog' });

        dialog.contentLayout.add_child(new St.Label({
            text: `尚未設定 ${PROVIDER_NAMES[mode]} 的 API key`,
            style_class: 'voice-input-key-dialog-title',
        }));
        dialog.contentLayout.add_child(new St.Label({
            text: PROVIDER_KEY_HELP[mode],
            style_class: 'voice-input-key-dialog-hint',
        }));

        const entry = new St.Entry({
            style_class: 'voice-input-key-dialog-entry',
            hint_text: '貼上 API key…',
            can_focus: true,
            x_expand: true,
        });
        entry.clutter_text.set_password_char('●');
        dialog.contentLayout.add_child(entry);

        const errorLabel = new St.Label({ style_class: 'voice-input-key-dialog-error' });
        errorLabel.hide();
        dialog.contentLayout.add_child(errorLabel);

        const trySave = () => {
            const key = entry.get_text().trim();
            if (!key) {
                errorLabel.text = '請輸入 API key';
                errorLabel.show();
                return;
            }
            this._saveApiKey(mode, key);
            dialog.close();
            this._applyProviderMode(mode);
        };

        entry.clutter_text.connect('activate', trySave);

        dialog.setButtons([
            { label: '取消', action: () => dialog.close(), key: Clutter.KEY_Escape },
            { label: '儲存', action: trySave, default: true },
        ]);

        dialog.open();
        dialog.setInitialKeyFocus(entry);
    }

    _setupStateWatcher() {
        try {
            GLib.mkdir_with_parents(this._stateDir, 0o700);
            const dir = Gio.File.new_for_path(this._stateDir);
            this._monitor = dir.monitor_directory(Gio.FileMonitorFlags.NONE, null);
            this._monitor.connect('changed', () => this._refreshState());
        } catch (e) {
            logError(e, 'voice-input: 無法監看狀態資料夾');
        }
        this._refreshState();
    }

    _refreshState() {
        const recording = GLib.file_test(this._pidFilePath, GLib.FileTest.EXISTS);
        if (recording) {
            this._applyState('recording');
            return;
        }
        const processing = GLib.file_test(this._processingFilePath, GLib.FileTest.EXISTS);
        this._applyState(processing ? 'processing' : 'idle');
    }

    _applyState(state) {
        if (this._state === state) return;
        this._state = state;

        this._icon.remove_all_transitions();
        this._icon.opacity = 255;
        this._icon.rotation_angle_z = 0;
        this._icon.remove_style_class_name('voice-input-icon-recording');
        this._icon.remove_style_class_name('voice-input-icon-processing');

        switch (state) {
        case 'recording':
            this._icon.icon_name = 'media-record-symbolic';
            this._icon.add_style_class_name('voice-input-icon-recording');
            this._indicator.accessible_name = '語音輸入：錄音中（點擊或按 Ctrl+Super+V 結束並轉文字）';
            this._pulse();
            break;
        case 'processing':
            this._icon.icon_name = 'content-loading-symbolic';
            this._icon.add_style_class_name('voice-input-icon-processing');
            this._indicator.accessible_name = '語音輸入：辨識中…';
            this._spin();
            break;
        default:
            this._icon.icon_name = 'audio-input-microphone-symbolic';
            this._indicator.accessible_name = '語音輸入：閒置（點擊或按 Ctrl+Super+V 開始錄音）';
            break;
        }
    }

    _pulse() {
        if (this._state !== 'recording' || !this._icon) return;
        this._icon.ease({
            opacity: 90,
            duration: PULSE_MS,
            mode: Clutter.AnimationMode.EASE_IN_OUT_SINE,
            onComplete: () => {
                if (this._state !== 'recording' || !this._icon) return;
                this._icon.ease({
                    opacity: 255,
                    duration: PULSE_MS,
                    mode: Clutter.AnimationMode.EASE_IN_OUT_SINE,
                    onComplete: () => this._pulse(),
                });
            },
        });
    }

    _spin() {
        if (this._state !== 'processing' || !this._icon) return;
        this._icon.ease({
            rotation_angle_z: this._icon.rotation_angle_z + 360,
            duration: SPIN_MS,
            mode: Clutter.AnimationMode.LINEAR,
            onComplete: () => this._spin(),
        });
    }

    _runToggleScript() {
        if (!GLib.file_test(this._scriptPath, GLib.FileTest.IS_EXECUTABLE)) {
            Main.notifyError('語音輸入', `找不到可執行的腳本：${this._scriptPath}`);
            return;
        }
        try {
            const proc = Gio.Subprocess.new([this._scriptPath], Gio.SubprocessFlags.NONE);
            proc.wait_async(null, (p, result) => {
                try {
                    p.wait_finish(result);
                    if (!p.get_successful()) {
                        logError(new Error(`exit code ${p.get_exit_status()}`), 'voice-input: voice-input-toggle.sh 執行失敗');
                    }
                } catch (e) {
                    logError(e, 'voice-input: 等待 voice-input-toggle.sh 結束時發生錯誤');
                }
            });
        } catch (e) {
            logError(e, 'voice-input: 無法啟動 voice-input-toggle.sh');
            Main.notifyError('語音輸入', '無法啟動錄音腳本');
        }
    }

    _ensureReadme() {
        const readmePath = GLib.build_filenamev([this._configDir, 'README.txt']);
        if (GLib.file_test(readmePath, GLib.FileTest.EXISTS)) return;
        const content =
            '語音輸入設定資料夾\n' +
            '====================\n\n' +
            '把對應服務的 API key 存成純文字檔（不要有換行、不要有多餘空白）：\n\n' +
            '  groq_api_key    Groq API key\n' +
            '                  申請頁面：https://console.groq.com/keys\n\n' +
            '  google_api_key  Google Cloud Speech-to-Text API key\n' +
            '                  到 Google Cloud Console 建立專案、啟用\n' +
            '                  「Cloud Speech-to-Text API」，再到憑證頁面建立\n' +
            '                  API 金鑰：https://console.cloud.google.com/apis/credentials\n' +
            '                  每月有 60 分鐘免費額度，超過依用量計費。\n\n' +
            'language 檔存目前的辨識語言模式，provider 檔存目前用哪個服務，\n' +
            '都是面板選單點選後自動寫入的，不需要手動編輯。\n';
        try {
            GLib.file_set_contents(readmePath, content);
        } catch (e) {
            logError(e, 'voice-input: 無法寫入 README.txt');
        }
    }

    _openConfigFolder() {
        try {
            GLib.mkdir_with_parents(this._configDir, 0o700);
            this._ensureReadme();
            const uri = Gio.File.new_for_path(this._configDir).get_uri();
            Gio.AppInfo.launch_default_for_uri(uri, null);
        } catch (e) {
            logError(e, 'voice-input: 無法開啟設定資料夾');
        }
    }

    disable() {
        if (this._monitor) {
            this._monitor.cancel();
            this._monitor = null;
        }
        this._icon?.remove_all_transitions();
        this._langMenu?.destroy();
        this._langMenu = null;
        this._menuManager = null;
        this._langMenuItems = null;
        this._providerMenuItems = null;
        this._indicator?.destroy();
        this._indicator = null;
        this._icon = null;
        this._langBadge = null;
        this._state = null;
    }
}
