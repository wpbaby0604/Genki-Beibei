import os
import streamlit.components.v1 as components

# 跟你的 live2d_component 一樣：靜態 path 元件，不需要 npm build
_parent_dir = os.path.dirname(os.path.abspath(__file__))
_frontend_dir = os.path.join(_parent_dir, "frontend")

_component_func = components.declare_component("beibei_voice_ui", path=_frontend_dir)


def voice_input(
    lang: str = "zh-TW",
    speak_ms: int = 0,
    speak_token=0,
    min_chars: int = 1,
    paused: bool = False,
    key=None,
):
    """連續語音輸入元件（toggle 開著就一直聽、免一直點；貝貝說話時自動靜音避免收到回聲）。

    參數：
      lang       ：辨識語言，預設繁中 "zh-TW"。
      speak_ms   ：貝貝這段 TTS 語音的長度（毫秒）。元件會在偵測到「新的一段朗讀」時
                   靜音這麼久，講完自動恢復聆聽。純文字回覆傳 0。
      speak_token：這段語音的識別碼。**只要它變了**，元件就認定是「新的一段朗讀」而觸發靜音。
                   建議傳 hash(latest_audio)（見下方 audio_b64_ms/用法說明）。
      min_chars  ：短於這麼多字的整句視為雜訊丟掉（1＝只丟空白）。
      paused     ：外部強制暫停旗標；True 時一律靜音（例如你在跑很長的處理時）。

    回傳 dict 或 None：{"supported": bool, "seq": int, "text": str}
      - seq：每辨識出一句 +1；用它去重（見 pop_new_utterance）。
    """
    return _component_func(
        lang=lang,
        speak_ms=int(speak_ms or 0),
        speak_token=speak_token,
        min_chars=int(min_chars),
        paused=bool(paused),
        key=key,
        default=None,
    )


def audio_b64_ms(audio_b64, kbps: int = 48) -> int:
    """估算 edge-tts base64 MP3 的長度（毫秒）。

    edge-tts 預設輸出 24kHz / 48kbps / mono 的 CBR MP3，所以：
        時長 ≈ 位元組數 × 8 / 位元率
    base64 每 4 字元 = 3 位元組，故位元組數 ≈ len(b64) × 3/4。
    這是近似值（誤差幾個 %），元件那邊還會多加一點緩衝，夠用。
    """
    if not audio_b64:
        return 0
    raw_len = len(audio_b64) * 3 // 4
    return int(raw_len * 8 / (kbps * 1000) * 1000)


def pop_new_utterance(result, state, seq_key: str = "_voice_last_seq"):
    """從 voice_input() 的回傳值取出「這次才出現的新句子」，沒有就回 None。

    用法：
        import streamlit as st
        res = voice_input(lang="zh-TW",
                          speak_ms=st.session_state.get("latest_audio_ms", 0),
                          speak_token=st.session_state.get("latest_audio_tok", 0),
                          key="beibei_voice")
        spoken = pop_new_utterance(res, st.session_state)
        if spoken:
            handle_user_text(spoken)   # ← 換成你現有處理使用者訊息的函式
            st.rerun()
    """
    if not result or not result.get("supported"):
        return None
    seq = result.get("seq") or 0
    if seq <= 0 or seq == state.get(seq_key):
        return None
    state[seq_key] = seq
    text = (result.get("text") or "").strip()
    return text or None
