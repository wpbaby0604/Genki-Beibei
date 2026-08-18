import os
import streamlit.components.v1 as components

_parent_dir = os.path.dirname(os.path.abspath(__file__))
_frontend_dir = os.path.join(_parent_dir, "frontend")
_component_func = components.declare_component("beibei_voice_ui", path=_frontend_dir)


def voice_input(listening: bool = False, lang: str = "zh-TW",
                speak_ms: int = 0, speak_token=0, min_chars: int = 1, key=None):
    """連續語音輸入元件（開關由 Python 端 st.toggle 控制，重排/重載都不會丟狀態）。

    參數：
      listening ：是否要聽（傳 st.toggle 的值進來）。
      lang      ：辨識語言，預設 "zh-TW"。
      speak_ms  ：貝貝這段 TTS 語音長度（毫秒）；偵測到新朗讀時靜音這麼久，講完自動恢復。
      speak_token：這段語音的識別碼，變了就代表「新的一段朗讀」。建議傳 hash(latest_audio)。
      min_chars ：短於這麼多字的整句視為雜訊丟掉。

    回傳 dict 或 None：{"supported": bool, "id": str, "text": str}
      - id：每句唯一（時間戳＋亂數），配合 pop_new_utterance 去重，永不誤判重複。
    """
    return _component_func(
        listening=bool(listening), lang=lang,
        speak_ms=int(speak_ms or 0), speak_token=speak_token,
        min_chars=int(min_chars), key=key, default=None,
    )


def audio_b64_ms(audio_b64, kbps: int = 48) -> int:
    """估算 edge-tts base64 MP3 的長度（毫秒）。edge-tts 預設 24kHz/48kbps/mono CBR，
    時長 ≈ 位元組數 × 8 / 位元率；base64 位元組數 ≈ len(b64) × 3/4。近似值，夠用。"""
    if not audio_b64:
        return 0
    raw_len = len(audio_b64) * 3 // 4
    return int(raw_len * 8 / (kbps * 1000) * 1000)


def pop_new_utterance(result, state, id_key: str = "_voice_last_id"):
    """取出「這次才出現的新句子」，沒有就回 None。用永不重複的 id 去重（重載也不會誤判）。

    用法：
        _spoken = pop_new_utterance(voice_input(listening=voice_on, ...), st.session_state)
        if _spoken and not user_input:
            user_input = _spoken
    """
    if not result or not result.get("supported"):
        return None
    uid = result.get("id")
    if not uid or uid == state.get(id_key):
        return None
    state[id_key] = uid
    text = (result.get("text") or "").strip()
    return text or None
