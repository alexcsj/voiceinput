#!/usr/bin/env python3
"""持續聆聽語音輸入 daemon。

啟動後透過 pw-record 持續擷取麥克風原始 PCM，用簡單的音量門檻做語音活動偵測
(VAD)：偵測到說話 -> 靜音一段時間(停頓) -> 自動把這段語音切開丟去 Groq
Whisper API 轉文字，貼上後繼續聽下一段，直到收到 SIGTERM/SIGINT 才結束。
行為類似「OK Google / Siri」的持續聆聽模式。
"""
import os
import pathlib
import queue
import signal
import struct
import subprocess
import sys
import threading
import time
import wave
from collections import deque

RATE = 16000
CHANNELS = 1
SAMPLE_WIDTH = 2  # 16-bit
FRAME_MS = 20
FRAME_BYTES = int(RATE * FRAME_MS / 1000) * SAMPLE_WIDTH  # 640 bytes/frame

# 可透過環境變數覆寫，方便依麥克風/環境雜訊微調
SILENCE_HANG_MS = int(os.environ.get('VOICE_INPUT_SILENCE_MS', '700'))
MIN_SPEECH_MS = int(os.environ.get('VOICE_INPUT_MIN_SPEECH_MS', '300'))
MAX_SEGMENT_MS = int(os.environ.get('VOICE_INPUT_MAX_SEGMENT_MS', '30000'))
NOISE_MULT = float(os.environ.get('VOICE_INPUT_NOISE_MULT', '2.8'))
ABS_MIN_RMS = float(os.environ.get('VOICE_INPUT_MIN_RMS', '150'))
PREROLL_MS = int(os.environ.get('VOICE_INPUT_PREROLL_MS', '300'))
# 標準版 WER 較低（10.3% vs turbo 12%），短片段的延遲差異可忽略，優先選準確度
MODEL = os.environ.get('VOICE_INPUT_MODEL', 'whisper-large-v3')
# 本地 Whisper（faster-whisper）用的模型大小，CPU 環境下 base 是官方建議的
# 起始點（約 5-10x real-time），準確度要求高可以換 small/medium，但會變慢
LOCAL_MODEL_SIZE = os.environ.get('VOICE_INPUT_LOCAL_MODEL', 'base')

STATE_DIR = pathlib.Path(os.environ.get('XDG_RUNTIME_DIR', '/tmp')) / 'voice-input'
PID_FILE = STATE_DIR / 'record.pid'
CONFIG_DIR = pathlib.Path.home() / '.config' / 'voice-input'
GROQ_KEY_FILE = CONFIG_DIR / 'groq_api_key'
GOOGLE_KEY_FILE = CONFIG_DIR / 'google_api_key'
GROK_KEY_FILE = CONFIG_DIR / 'grok_api_key'
LANG_CONFIG_FILE = CONFIG_DIR / 'language'
PROVIDER_CONFIG_FILE = CONFIG_DIR / 'provider'


VALID_MODES = ('zh-hant', 'zh-hans', 'auto', 'en', 'ja')
VALID_PROVIDERS = ('groq', 'google', 'grok', 'local')
# 'local' 不用 key 檔案，故意不放進這張表；查不到就代表不需要 key
_KEY_FILES = {'groq': GROQ_KEY_FILE, 'google': GOOGLE_KEY_FILE, 'grok': GROK_KEY_FILE}


def _resolve_provider():
    provider = os.environ.get('VOICE_INPUT_PROVIDER', '').strip()
    if not provider:
        try:
            provider = PROVIDER_CONFIG_FILE.read_text().strip()
        except FileNotFoundError:
            provider = 'groq'
    return provider if provider in VALID_PROVIDERS else 'groq'


PROVIDER = _resolve_provider()
KEY_FILE = _KEY_FILES.get(PROVIDER)


def _resolve_language_mode():
    # VOICE_INPUT_LANG 明確指定時優先（沿用舊的環境變數相容性），
    # 否則讀取面板擴充功能寫入的語言模式設定檔（zh-hant / zh-hans / auto / en / ja）
    mode = os.environ.get('VOICE_INPUT_LANG', '').strip()
    if not mode:
        try:
            mode = LANG_CONFIG_FILE.read_text().strip()
        except FileNotFoundError:
            mode = 'zh-hant'
    if mode == 'zh':  # 舊版設定檔相容（拆分簡繁之前只有單一 zh）
        mode = 'zh-hant'
    return mode if mode in VALID_MODES else 'zh-hant'


LANGUAGE_MODE = _resolve_language_mode()

# Whisper（Groq、本地 faster-whisper）跟 Grok(xAI) 的 language 參數都只認得
# 基礎的 'zh'，簡繁是轉錄完之後再用 OpenCC 轉換固定輸出的腳本：zh-hant 用
# s2twp 轉成台灣慣用字詞，zh-hans 用 tw2sp 轉成大陸標準字詞（不只轉字形，
# 連「軟體/網路」這種台灣說法也會一併轉成「软件/网络」）。Google STT 用
# BCP-47 語言代碼（zh-TW/zh-CN）可以直接指定輸出腳本，不需要這一步。
_OPENCC_CONFIG = {'zh-hant': 's2twp', 'zh-hans': 'tw2sp'}.get(LANGUAGE_MODE) if PROVIDER in ('groq', 'grok', 'local') else None
_opencc_converter = None
if _OPENCC_CONFIG:
    try:
        import opencc
        _opencc_converter = opencc.OpenCC(_OPENCC_CONFIG)
    except ImportError:
        print('警告：找不到 opencc 套件，繁簡轉換會被跳過', file=sys.stderr)

_WHISPER_LANG = {'zh-hant': 'zh', 'zh-hans': 'zh', 'en': 'en', 'ja': 'ja'}.get(LANGUAGE_MODE)
# xAI 的 STT API 文件裡 language 參數說明是「用來做文字格式化(數字/日期
# 正規化)」，不確定是不是嚴格限制辨識語言，但用法（en/fr/de 這種基礎
# ISO 代碼）跟 Whisper 一致，所以沿用跟 Groq 一樣的映射；auto 模式不帶
# language，讓它自己判斷。
_GROK_LANG = _WHISPER_LANG

# Google STT 用 BCP-47 語言代碼，且可以直接指定繁體/簡體，不用事後轉換。
# 'auto'（中英混雜）用「主要中文＋英文備選」近似，這不是真正的自動語言
# 偵測——Google v1 沒有對應 Whisper「不給 language 參數就自動判斷」的行為，
# alternativeLanguageCodes 只是讓辨識器在有限的候選語言之間挑一個。
_GOOGLE_LANG = {
    'zh-hant': ('zh-TW', []),
    'zh-hans': ('zh-CN', []),
    'en': ('en-US', []),
    'ja': ('ja-JP', []),
    'auto': ('zh-TW', ['en-US']),
}.get(LANGUAGE_MODE, ('zh-TW', []))

# Whisper 對靜音/雜訊常會幻覺出固定套語（英文「you」「thank you」、
# 中文「感谢观看」「请不吝点赞 订阅 转发 打赏支持明镜与点点栏目」之類
# 影片結尾字幕，這些是訓練資料裡極常見的樣板，模型甚至會「很有信心」地
# 幻覺出來 —— 實測過 avg_logprob 可以高達 -0.13（非常自信）同時
# no_speech_prob 高達 0.68（其實是靜音），代表官方 whisper 參考實作那種
# 「兩個條件同時成立」的門檻不夠用，這裡改成任一條件成立就丟棄，
# 再疊加已知幻覺套語的關鍵字比對當第二層防護。
NO_SPEECH_THRESHOLD = float(os.environ.get('VOICE_INPUT_NO_SPEECH_THRESHOLD', '0.5'))
LOGPROB_THRESHOLD = float(os.environ.get('VOICE_INPUT_LOGPROB_THRESHOLD', '-1.0'))

_HALLUCINATION_MARKERS = (
    '明镜与点点', '明鏡與點點', '请不吝点赞', '請不吝點贊',
    '点赞 订阅 转发', '點贊 訂閱 轉發', '打赏支持', '打賞支持',
    '感谢观看', '感謝觀看', '谢谢观看', '謝謝觀看',
    '字幕由', '字幕組', '字幕组', 'amara.org',
    '不吝點贊', '不吝点赞', '订阅转发', '訂閱轉發',
    'thank you for watching', 'thanks for watching', 'please subscribe',
    # 日文版本的「感谢观看」，訓練資料裡大量日文影片結尾字幕造成的幻覺樣板
    'ご視聴ありがとうございました', 'ご視聴ありがとうございます',
    'チャンネル登録',
)
_HALLUCINATION_EXACT = ('you', 'thank you', 'thank you.', 'bye', 'bye.', 'bye bye')


def _looks_hallucinated(text):
    stripped = text.strip()
    if not stripped:
        return True
    lowered = stripped.lower()
    if lowered in _HALLUCINATION_EXACT:
        return True
    return any(marker.lower() in lowered for marker in _HALLUCINATION_MARKERS)

_stop_requested = threading.Event()


def _handle_signal(signum, frame):
    _stop_requested.set()


signal.signal(signal.SIGTERM, _handle_signal)
signal.signal(signal.SIGINT, _handle_signal)


def _rms(frame_bytes):
    count = len(frame_bytes) // 2
    if count == 0:
        return 0.0
    total = 0
    for i in range(0, count * 2, 2):
        sample = struct.unpack_from('<h', frame_bytes, i)[0]
        total += sample * sample
    return (total / count) ** 0.5


def _notify(text):
    try:
        subprocess.run(['notify-send', '-t', '2000', '語音輸入', text], check=False)
    except Exception:
        pass


def _wl_copy(text):
    try:
        subprocess.run(['wl-copy'], input=text.encode('utf-8'), check=False)
    except Exception:
        pass


def _paste():
    try:
        subprocess.run(['ydotool', 'key', '29:1', '47:1', '47:0', '29:0'], check=False)
    except Exception:
        pass


def _transcribe_groq(pcm_bytes, seg_index, api_key):
    wav_path = STATE_DIR / f'segment-{seg_index}.wav'
    with wave.open(str(wav_path), 'wb') as wf:
        wf.setnchannels(CHANNELS)
        wf.setsampwidth(SAMPLE_WIDTH)
        wf.setframerate(RATE)
        wf.writeframes(pcm_bytes)

    text = ''
    try:
        import requests
        # verbose_json 才會附上 no_speech_prob / avg_logprob，用來過濾
        # Whisper 對靜音/雜訊的幻覺輸出
        data = {'model': MODEL, 'response_format': 'verbose_json'}
        # 中英混雜(auto)不指定 language，讓 Whisper 依這一小段語音自行判斷
        if _WHISPER_LANG:
            data['language'] = _WHISPER_LANG
        with open(wav_path, 'rb') as f:
            resp = requests.post(
                'https://api.groq.com/openai/v1/audio/transcriptions',
                headers={'Authorization': f'Bearer {api_key}'},
                files={'file': (wav_path.name, f, 'audio/wav')},
                data=data,
                timeout=30,
            )
        if resp.ok:
            payload = resp.json()
            segments = payload.get('segments') or []
            if segments:
                kept = []
                for seg in segments:
                    no_speech = seg.get('no_speech_prob', 0.0)
                    logprob = seg.get('avg_logprob', 0.0)
                    seg_text = seg.get('text', '')
                    # 任一條件成立就丟棄（見上方註解：幻覺有時信心分數很高，
                    # 不能只靠兩個條件同時成立才判定）
                    if no_speech > NO_SPEECH_THRESHOLD or logprob < LOGPROB_THRESHOLD:
                        continue
                    if _looks_hallucinated(seg_text):
                        continue
                    kept.append(seg_text)
                text = ''.join(kept).strip()
            else:
                text = (payload.get('text') or '').strip()
                if _looks_hallucinated(text):
                    text = ''
    except Exception as e:
        print(f'轉文字失敗(Groq): {e}', file=sys.stderr)
    finally:
        try:
            wav_path.unlink()
        except FileNotFoundError:
            pass

    # Whisper 的 language='zh' 不保證輸出簡體或繁體，這裡強制轉成目標腳本
    if text and _opencc_converter:
        text = _opencc_converter.convert(text)

    return text


def _transcribe_google(pcm_bytes, api_key):
    import base64
    import requests

    language_code, alt_codes = _GOOGLE_LANG
    body = {
        'config': {
            'encoding': 'LINEAR16',
            'sampleRateHertz': RATE,
            'languageCode': language_code,
            'enableAutomaticPunctuation': True,
        },
        'audio': {'content': base64.b64encode(pcm_bytes).decode('ascii')},
    }
    if alt_codes:
        body['config']['alternativeLanguageCodes'] = alt_codes

    text = ''
    try:
        resp = requests.post(
            'https://speech.googleapis.com/v1/speech:recognize',
            params={'key': api_key},
            json=body,
            timeout=30,
        )
        if resp.ok:
            payload = resp.json()
            # 純靜音時 Google 直接回傳空的 results，不像 Whisper 會幻覺出
            # 整句話，這裡不需要額外的信心分數/黑名單過濾
            parts = [
                r['alternatives'][0]['transcript']
                for r in payload.get('results', [])
                if r.get('alternatives')
            ]
            text = ''.join(parts).strip()
        else:
            print(f'轉文字失敗(Google): HTTP {resp.status_code} {resp.text}', file=sys.stderr)
    except Exception as e:
        print(f'轉文字失敗(Google): {e}', file=sys.stderr)

    return text


def _transcribe_grok(pcm_bytes, seg_index, api_key):
    import requests

    wav_path = STATE_DIR / f'segment-{seg_index}.wav'
    with wave.open(str(wav_path), 'wb') as wf:
        wf.setnchannels(CHANNELS)
        wf.setsampwidth(SAMPLE_WIDTH)
        wf.setframerate(RATE)
        wf.writeframes(pcm_bytes)

    data = {}
    if _GROK_LANG:
        data['language'] = _GROK_LANG

    text = ''
    try:
        with open(wav_path, 'rb') as f:
            # xAI 文件要求 file 欄位要放在其他參數後面；requests 用分開的
            # data/files 參數時，序列化順序本來就是 data 先、files 後，
            # 天然符合這個要求，不用特別處理。
            resp = requests.post(
                'https://api.x.ai/v1/stt',
                headers={'Authorization': f'Bearer {api_key}'},
                data=data,
                files={'file': (wav_path.name, f, 'audio/wav')},
                timeout=30,
            )
        if resp.ok:
            payload = resp.json()
            text = (payload.get('text') or '').strip()
            # xAI 的回應不像 Groq 有 no_speech_prob/avg_logprob 這種信心
            # 分數可以拿來過濾幻覺，只能靠已知幻覺套語黑名單當防線
            if _looks_hallucinated(text):
                text = ''
        else:
            print(f'轉文字失敗(Grok): HTTP {resp.status_code} {resp.text}', file=sys.stderr)
    except Exception as e:
        print(f'轉文字失敗(Grok): {e}', file=sys.stderr)
    finally:
        try:
            wav_path.unlink()
        except FileNotFoundError:
            pass

    if text and _opencc_converter:
        text = _opencc_converter.convert(text)

    return text


def _load_local_model():
    """本地 Whisper 模型只在整個聆聽階段載入一次，不是每段語音都重載——
    光載入模型權重就要幾秒鐘，逐段重載會慢到沒辦法用。"""
    from faster_whisper import WhisperModel
    return WhisperModel(LOCAL_MODEL_SIZE, device='cpu', compute_type='int8')


def _transcribe_local(pcm_bytes, model):
    import numpy as np

    # faster-whisper 的 audio 參數可以直接吃 numpy float32 陣列，不用先寫
    # 成 wav 檔再讓它用 ffmpeg 解碼一次，省掉一個外部相依套件跟磁碟 I/O。
    # Whisper 系列模型固定吃 16-bit PCM 正規化到 [-1, 1] 的 float32。
    audio = np.frombuffer(pcm_bytes, dtype=np.int16).astype(np.float32) / 32768.0

    text = ''
    try:
        segments, _info = model.transcribe(
            audio,
            language=_WHISPER_LANG,
            beam_size=5,
        )
        kept = []
        for seg in segments:
            # faster-whisper 的 Segment 跟 Groq 的 verbose_json 是同一套
            # 欄位（no_speech_prob/avg_logprob），沿用一樣的雙層過濾
            if seg.no_speech_prob > NO_SPEECH_THRESHOLD or seg.avg_logprob < LOGPROB_THRESHOLD:
                continue
            if _looks_hallucinated(seg.text):
                continue
            kept.append(seg.text)
        text = ''.join(kept).strip()
    except Exception as e:
        print(f'轉文字失敗(本地 Whisper): {e}', file=sys.stderr)

    if text and _opencc_converter:
        text = _opencc_converter.convert(text)

    return text


def _transcribe_and_paste(pcm_bytes, seg_index, api_key, model=None):
    if PROVIDER == 'google':
        text = _transcribe_google(pcm_bytes, api_key)
    elif PROVIDER == 'grok':
        text = _transcribe_grok(pcm_bytes, seg_index, api_key)
    elif PROVIDER == 'local':
        text = _transcribe_local(pcm_bytes, model)
    else:
        text = _transcribe_groq(pcm_bytes, seg_index, api_key)

    if not text:
        return

    _wl_copy(text)
    time.sleep(0.1)
    _paste()


def _worker_loop(seg_queue, api_key, model=None):
    seg_index = 0
    while True:
        item = seg_queue.get()
        if item is None:
            break
        seg_index += 1
        _transcribe_and_paste(item, seg_index, api_key, model)


def main():
    api_key = None
    model = None

    if PROVIDER == 'local':
        _notify('⏳ 正在載入本地 Whisper 模型…（第一次使用要先下載，可能要等一下）')
        try:
            model = _load_local_model()
        except ImportError:
            _notify('⚠️ 找不到 faster-whisper，請先安裝：pip install faster-whisper（Arch 上可能需要加 --break-system-packages，或用虛擬環境）')
            sys.exit(1)
        except Exception as e:
            _notify(f'⚠️ 載入本地 Whisper 模型失敗：{e}')
            sys.exit(1)
    else:
        provider_label = {'groq': 'Groq', 'google': 'Google Cloud', 'grok': 'Grok (xAI)'}[PROVIDER]
        if not KEY_FILE.exists() or not KEY_FILE.read_text().strip():
            _notify(f'⚠️ 找不到 {provider_label} API key ({KEY_FILE})')
            sys.exit(1)
        api_key = KEY_FILE.read_text().strip()

    STATE_DIR.mkdir(parents=True, exist_ok=True)
    # 自己回報 PID，不能靠 toggle.sh 的 `setsid ... & echo $!`：setsid 在
    # bash 背景工作(已經是 process group leader)底下會自己 fork 一次，
    # `$!` 抓到的是 setsid 那個馬上就結束的過渡行程，不是真正的 daemon
    # PID，會導致 toggle.sh 之後送 SIGTERM 打不到人、daemon 變成孤兒繼續
    # 跑（實測證實過：kill 那個假 PID 會顯示「沒有此一程序」，但真正的
    # daemon/pw-record 還活著）。
    PID_FILE.write_text(str(os.getpid()))

    seg_queue = queue.Queue()
    worker = threading.Thread(target=_worker_loop, args=(seg_queue, api_key, model), daemon=True)
    worker.start()

    proc = subprocess.Popen(
        ['pw-record', '-a', '--format', 's16', '--rate', str(RATE),
         '--channels', str(CHANNELS), '-'],
        stdout=subprocess.PIPE,
    )

    _notify('🎙️ 持續聆聽中…(再按一次 Ctrl+Super+V 或點面板圖示結束)')

    speech_buf = bytearray()
    in_speech = False
    silence_ms = 0
    speech_ms = 0
    noise_floor = ABS_MIN_RMS
    # 靜音時持續保留最近 PREROLL_MS 的音訊，開口瞬間音量門檻還沒觸發前的
    # 起音才不會被吃掉（VAD 常見的漏字/吃字問題多半出在這裡）
    preroll_frames = max(1, PREROLL_MS // FRAME_MS)
    preroll = deque(maxlen=preroll_frames)

    try:
        while not _stop_requested.is_set():
            frame = proc.stdout.read(FRAME_BYTES)
            if len(frame) < FRAME_BYTES:
                if proc.poll() is not None or _stop_requested.is_set():
                    break
                continue

            level = _rms(frame)
            threshold = max(noise_floor * NOISE_MULT, ABS_MIN_RMS)
            is_loud = level > threshold

            if not in_speech:
                preroll.append(frame)

            if is_loud:
                if not in_speech:
                    in_speech = True
                    speech_ms = 0
                    speech_buf = bytearray()
                    for f in preroll:
                        speech_buf.extend(f)
                    speech_ms += len(preroll) * FRAME_MS
                    preroll.clear()
                speech_buf.extend(frame)
                speech_ms += FRAME_MS
                silence_ms = 0

                if speech_ms >= MAX_SEGMENT_MS:
                    seg_queue.put(bytes(speech_buf))
                    in_speech = False
                    speech_buf = bytearray()
            elif in_speech:
                speech_buf.extend(frame)
                silence_ms += FRAME_MS
                if silence_ms >= SILENCE_HANG_MS:
                    if speech_ms >= MIN_SPEECH_MS:
                        seg_queue.put(bytes(speech_buf))
                    in_speech = False
                    speech_buf = bytearray()
                    silence_ms = 0
            else:
                # 安靜狀態下，讓雜訊基準值慢慢跟著環境調整
                noise_floor = noise_floor * 0.98 + level * 0.02
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=2)
        except Exception:
            proc.kill()

        if in_speech and speech_ms >= MIN_SPEECH_MS:
            seg_queue.put(bytes(speech_buf))

        seg_queue.put(None)
        worker.join(timeout=60)


if __name__ == '__main__':
    main()
