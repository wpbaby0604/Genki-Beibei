# ============================================================
#  companion_live2d.py — 模式二「陪伴貝貝」(Live2D AI 即時陪伴)
#
#  本檔由本地版 ui_pages.py 的 show_live2d_chat() 移植而來，並針對
#  Streamlit Community Cloud 做了以下調整：
#
#  ① 只走「原生 Live2D」分支（前端 PIXI + Cubism，跑在瀏覽器 iframe）。
#     本地版的「上傳自訂頭像 → THA3 神經網路算圖」分支需要 torch + 500MB
#     權重 + CPU 逐幀推論，無法在雲端免費版運作，故在此移除/停用；該功能
#     仍保留在你的本地 app。介面上保留入口，但會提示「本地端限定」。
#  ② Gemini 金鑰改用 api_config.get_api_keys()（雲端 Secrets 優先），
#     且缺金鑰時友善提示而非整頁崩潰。
#  ③ 所有 Streamlit 呼叫改用 1.41.1 相容寫法（use_container_width，
#     不使用新版的 width="stretch"），以免動搖既有雲端版的固定版本。
#
#  與第一部分（雲端版）的耦合點只有：core_data.go_to。
#  健康數據「不」由本頁寫入；本頁是純陪伴互動模組。
# ============================================================
from __future__ import annotations

import os
import re
import json
import time
import base64
import html
import random
import asyncio

import streamlit as st

from core_data import go_to
from live2d_component import render_beibei
from api_config import get_api_keys

# 🆕 頭像儲存抽象層（repo 內建保底 + 之後接 Google Drive）
import avatar_store

# 🆕 待烤照片佇列（路線 C：使用者上傳照片 → 本機 baker 收件烤製）
from drive_queue import DriveQueue, DriveQueueError

# edge-tts 的非同步在 Streamlit 腳本執行緒裡用 asyncio.run 即可；
# 保險套用 nest_asyncio，避免某些情況「event loop already running」。
try:
    import nest_asyncio
    nest_asyncio.apply()
except Exception:
    pass


# ==========================================
# Gemini 聊天
# ==========================================
GEMINI_MODEL = "gemini-2.5-flash"   # 穩定 GA；勿用已下架的 gemini-2.0-flash
MAX_RETRIES_PER_KEY = 3
BASE_BACKOFF_SEC = 1.0


def get_ai_response(user_data, is_audio: bool = False) -> str:
    """呼叫 Gemini。金鑰缺失或套件未安裝時，回傳友善訊息而非丟例外。"""
    api_keys = get_api_keys()
    if not api_keys:
        return ("（系統提示）還沒設定 Gemini 金鑰喔～請在 Streamlit Cloud 的 "
                "Settings → Secrets 加入 GEMINI_API_KEY_1，貝貝才能開口聊天 (｡•́ω•ˋ｡)")

    try:
        from google import genai
        from google.genai import types
    except Exception:
        return "（系統提示）找不到 google-genai 套件，請確認 requirements.txt 已安裝。"

    system_prompt = (
        "妳是『貝貝』，一個活潑且可愛的 AI 陪伴者，並且深度了解過世界上發生的任何事，甚至是網路流行梗你也很擅長。"
        "如果是用繁體中文問問題必須用繁體中文回覆，除非使用者特別要求語言。"
        "你要考慮對方說的話是不是玩梗或者是唱歌，如果是唱歌你可以接著唱下一句，如果是玩梗的話你可以吐槽或者是接下一句。"
        "平常聊天請保持親切 約30字，但如果使用者要求推薦、解釋或詢問具體問題時，請務必給出『完整的具體答案』。"
        "口語中如果你是要裝可愛可以適度加入日系語助詞（一段對話最多用一次）。"
        "句子結尾可以加入顏文字，例如：『(´･-･●`)』、『(｡•́ω•ˋ｡)』、『ʕ´•×•`ʔ』。"
    )

    # 把最近的對話脈絡打包，賦予貝貝記憶
    history_text = "【過去的對話脈絡】\n"
    for msg in st.session_state.get("chat_history", [])[-6:]:
        role_name = "貝貝" if msg["role"] == "assistant" else "主人"
        clean = msg["content"].replace("🚨 【打瞌睡警報】\n\n", "").replace("🌸 【主動關懷】\n\n", "")
        history_text += f"{role_name}：{clean}\n"
    history_text += "【現在】\n"

    try:
        if is_audio:
            audio_part = types.Part.from_bytes(data=user_data, mime_type="audio/wav")
            contents = [system_prompt, history_text,
                        "使用者傳送了一段語音，請結合上面的對話脈絡，直接聽這段語音並回覆：",
                        audio_part]
        else:
            contents = f"{system_prompt}\n\n{history_text}\n使用者說：{user_data}"

        last_error = ""
        for i, current_key in enumerate(api_keys):
            client = genai.Client(api_key=current_key)
            for attempt in range(MAX_RETRIES_PER_KEY):
                try:
                    response = client.models.generate_content(model=GEMINI_MODEL, contents=contents)
                    return response.text.replace("*", "")
                except Exception as e:
                    last_error = str(e)
                    is_transient = any(code in last_error for code in [
                        "503", "UNAVAILABLE", "overloaded", "500", "INTERNAL",
                        "429", "RESOURCE_EXHAUSTED",
                    ])
                    if is_transient and attempt < MAX_RETRIES_PER_KEY - 1:
                        time.sleep(BASE_BACKOFF_SEC * (2 ** attempt) + random.uniform(0, 0.4))
                        continue
                    break  # 換下一把鑰匙

        if any(code in last_error for code in ["429", "RESOURCE_EXHAUSTED"]):
            return "主人對不起…貝貝今天聊太多次，額度暫時用完了，我們等一下或明天再聊好不好呀？(´;ω;`)"
        return "現在找貝貝聊天的人有點多，伺服器小塞車中…可以等幾秒再跟我說一次嗎？(｡•́ω•ˋ｡)"
    except Exception as e:
        print(f"系統發生未預期錯誤: {e}")
        return "貝貝的腦袋剛剛短路了一下，可以再說一次嗎？"


# ==========================================
# Edge TTS 語音
# ==========================================
VOICE_PROFILES = {
    "female": {"voice": "zh-CN-XiaoxiaoNeural", "rate": "+25%", "pitch": "+5Hz"},
    "male":   {"voice": "zh-CN-YunxiNeural",    "rate": "+15%", "pitch": "+0Hz"},
}


# 語音專用清字：只保留「可朗讀」的字元（中文／中文擴充A／英數／空白／基本標點），
# 其餘一律清掉——顏文字 (´･ω･｀)、emoji、箭頭、幾何符號等都會被移除，不會被唸出來。
# 注意：這只影響「要唸的版本」，聊天泡泡仍顯示完整內容。
_SPEECH_KEEP = re.compile(
    r"[^\u4e00-\u9fff\u3400-\u4dbfA-Za-z0-9\s，。！？、；：,.!?]"
)


def _clean_for_speech(text) -> str:
    t = str(text)
    t = t.replace("～", "，").replace("~", "，").replace("*", "")
    t = re.sub(r"[\U00010000-\U0010ffff]", "", t)   # 去掉星平面 emoji
    t = _SPEECH_KEEP.sub("", t)                       # 只留可朗讀字元（顏文字符號一併清掉）
    t = re.sub(r"\s+", " ", t).strip()                # 收斂多餘空白
    return t


async def generate_voice(text, gender=None):
    """文字轉語音（base64 mp3）。套件缺失或失敗時回傳 None。"""
    try:
        import edge_tts
    except Exception:
        print("找不到 edge-tts 套件")
        return None

    text_for_speech = _clean_for_speech(text)
    if not text_for_speech:
        return None   # 清完沒剩可唸的字（例如整句都是顏文字）→ 不發聲，避免 TTS 出錯

    if gender is None:
        try:
            gender = st.session_state.get("beibei_voice_gender", "female")
        except Exception:
            gender = "female"
    profile = VOICE_PROFILES.get(gender, VOICE_PROFILES["female"])

    output_file = "temp_reply.mp3"
    try:
        communicate = edge_tts.Communicate(
            text_for_speech, profile["voice"],
            rate=profile["rate"], pitch=profile["pitch"],
        )
        await communicate.save(output_file)
        with open(output_file, "rb") as f:
            audio_b64 = base64.b64encode(f.read()).decode("utf-8")
        if os.path.exists(output_file):
            os.remove(output_file)
        return audio_b64
    except Exception as e:
        print(f"聲音生成出錯: {e}")
        return None


def _speak(text) -> str | None:
    """同步包一層 generate_voice。"""
    try:
        return asyncio.run(generate_voice(text))
    except RuntimeError:
        # 已有 running loop（少數情況）→ 用新 loop 跑
        loop = asyncio.new_event_loop()
        try:
            return loop.run_until_complete(generate_voice(text))
        finally:
            loop.close()


# ==========================================
# 使用者偏好持久化（與本地版同款；雲端為暫存，重啟回預設）
# ==========================================
SAVED_DIR = "saved_avatars"
AVATAR_POS_FILE = os.path.join(SAVED_DIR, "avatar_positions.json")
DEFAULT_AVATAR_POS = {"scale": 1.0, "x": 0, "y": 0}
LIVE2D_SETTINGS_FILE = os.path.join(SAVED_DIR, "live2d_settings.json")
DEFAULT_LIVE2D_SETTINGS = {
    "idle_chat_enabled": True,
    "chat_user_emoji": "🧑",
    "chat_custom_bg": False,
    "chat_bg_color": "#FFF5F5",
    "chat_bubble_color": "#FFFFFF",
    "chat_user_avatar_path": "",
}


def load_avatar_positions() -> dict:
    try:
        with open(AVATAR_POS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}


def save_avatar_positions(positions: dict) -> None:
    os.makedirs(SAVED_DIR, exist_ok=True)
    try:
        with open(AVATAR_POS_FILE, "w", encoding="utf-8") as f:
            json.dump(positions, f, ensure_ascii=False, indent=2)
    except OSError:
        pass


def load_live2d_settings() -> dict:
    try:
        with open(LIVE2D_SETTINGS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            return {**DEFAULT_LIVE2D_SETTINGS, **data}
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        pass
    return dict(DEFAULT_LIVE2D_SETTINGS)


def save_live2d_settings(settings: dict) -> None:
    os.makedirs(SAVED_DIR, exist_ok=True)
    try:
        with open(LIVE2D_SETTINGS_FILE, "w", encoding="utf-8") as f:
            json.dump(settings, f, ensure_ascii=False, indent=2)
    except OSError:
        pass


# ==========================================
# 頁面：模式二 陪伴貝貝
# ==========================================
# ==========================================
# 自訂頭像（THA3 預烤）— 雲端只「讀檔播放」，不跑神經網路
# ==========================================
BAKED_DIR = avatar_store.BAKED_DIR   # 保留舊名字，實際路徑由 avatar_store 決定


@st.cache_data(ttl=60, show_spinner=False)
def list_baked_avatars() -> list[str]:
    """列出所有可用頭像：repo 內建 3 個（保底，永遠可用）+ Google Drive 上使用者烤的。
    ttl=60：新烤好的頭像最慢一分鐘內出現；烤製完成時我們也會主動呼叫 .clear()。"""
    return avatar_store.list_avatars()


def load_baked_avatar(name: str) -> dict | None:
    """讀入一個預烤頭像 JSON（bake_avatar.py / HF Space 產出，格式完全相同）。
    刻意【不加快取】：它只會在 get_ai_webp() 快取未命中時被呼叫一次，
    讓那個 15MB 的 dict 用完即散，不要常駐在 1GB 的容器裡。"""
    try:
        data = json.loads(avatar_store.load_avatar(name))
        if isinstance(data, dict) and "clips" in data:
            return data
    except Exception as e:   # noqa: BLE001
        print(f"[companion] 讀取頭像失敗 {name}: {e}")
    return None


def classify_emotion(text: str) -> str:
    """粗略判斷回覆情緒 → neutral / happy / sad / angry，用來選說話版動畫。"""
    if not text:
        return "neutral"
    happy = ["開心", "高興", "太好", "哈哈", "嘿嘿", "讚", "好棒", "喜歡", "愛你", "好耶", "😄", "😊", "🥰", "😆", "✨", "💖"]
    sad = ["難過", "傷心", "抱歉", "對不起", "唉", "失望", "可惜", "好累", "孤單", "嗚", "😢", "😭", "🥺"]
    angry = ["生氣", "討厭", "哼", "可惡", "好煩", "不可以", "警告", "別這樣", "😠", "😡", "💢"]
    score = {"happy": 0, "sad": 0, "angry": 0}
    for w in happy:
        if w in text:
            score["happy"] += 1
    for w in sad:
        if w in text:
            score["sad"] += 1
    for w in angry:
        if w in text:
            score["angry"] += 1
    best = max(score, key=score.get)
    return best if score[best] > 0 else "neutral"


def build_ai_webp(baked: dict, emotion: str = "neutral") -> str:
    """把預烤資料組成前端 ai_webp 要的 JSON 字串（依情緒挑說話版）。
    與本地 run_tha3_animation 的輸出格式完全一致。"""
    clips = baked.get("clips", {})
    talking_key = {"happy": "talk_happy", "sad": "talk_sad", "angry": "talk_angry"}.get(emotion, "talk_neutral")
    return json.dumps({
        "idle":    clips.get("idle", ""),
        "talking": clips.get(talking_key, clips.get("talk_neutral", "")),
        "yawn":    clips.get("yawn", ""),
        "alert":   clips.get("alert", ""),
        "grid":    baked.get("grid", []),
        "gridDim": baked.get("gridDim", 7),
    })


@st.cache_resource(max_entries=4, ttl=3600, show_spinner="🎨 正在載入頭像動畫…")
def get_ai_webp(name: str, emotion: str) -> str:
    """🆕 快取層。key = (頭像名稱, 情緒)，所以不需要把 15MB 的 dict 丟進 cache 簽名。

    為什麼是 cache_resource 而不是 cache_data：
      cache_data 在記憶體裡存的是 pickle 過的 bytes，每次命中都會 pickle.loads
      還原成【新物件】—— 對 10.8MB 的字串就是每次 rerun 重配置 10.8MB。
      cache_resource 直接回傳同一個物件的參照；字串是 immutable，共用完全安全，
      而且跨 session 共享（多人同時看同一個頭像時只有一份）。

    失敗時 raise 而不是 return None —— 讓 Streamlit 不要把「失敗」快取一小時。
    """
    baked = load_baked_avatar(name)
    if not baked:
        raise avatar_store.AvatarNotFound(name)
    return build_ai_webp(baked, emotion)      # ← 產物格式一個位元都沒變


# ============================================================
#  路線 C：上傳照片訂製角色（送進 Drive 待烤佇列）
#  —— 以下皆為新增，未更動任何既有邏輯。
# ============================================================
@st.cache_resource
def _get_drive_queue():
    """建立 DriveQueue（用雲端 Secrets 的同一組 [gdrive] 憑證）。失敗回 None。"""
    try:
        return DriveQueue.from_secrets()
    except Exception as e:  # noqa: BLE001
        print(f"[companion] DriveQueue 初始化失敗: {e}")
        return None


def _make_job_code(nickname: str) -> str:
    """暱稱清成合法檔名 + 補 6 碼隨機，當認領代碼／成品頭像名（符合 ^[\\w\\-]{1,64}$）。"""
    base = re.sub(r"[^\w\-]", "", nickname)[:40] if nickname else ""
    if not base:
        base = "me"
    suffix = "".join(random.choices("0123456789abcdef", k=6))
    return f"{base}_{suffix}"


def _render_job_status(queue, code: str) -> None:
    """顯示某認領代碼目前狀態：已完成 / 排隊中。"""
    ready = False
    try:
        ready = code in list_baked_avatars()
    except Exception:  # noqa: BLE001
        pass
    if ready:
        st.success(f"🎉 你的角色「{code}」已經烤好了！在上面「🎨 我的自訂頭像」下拉選它即可。")
        return
    pending = False
    try:
        pending = any(p["job_id"] == code for p in queue.list_pending())
    except Exception:  # noqa: BLE001
        pass
    if pending:
        st.info(f"⏳ 「{code}」排隊製作中，請稍後回來重整查看。")
    else:
        st.caption(f"（暫時查不到「{code}」的狀態，可能已完成並被清理，或代碼有誤。）")


def _submit_photo(queue, photo, nickname) -> None:
    """把上傳的照片送進待烤佇列。"""
    try:
        data = photo.getvalue()
    except Exception:  # noqa: BLE001
        data = photo.read()
    if not data:
        st.error("讀不到照片內容，請重新選擇。")
        return
    if len(data) > 5 * 1024 * 1024:
        st.error("照片超過 5MB，請壓縮後再試。")
        return
    ext = (photo.name.rsplit(".", 1)[-1] if "." in photo.name else "png").lower()
    code = _make_job_code(nickname)
    try:
        queue.upload_photo(code, data, ext=ext)
    except DriveQueueError as e:
        st.error(f"上傳失敗：{e}")
        return
    except Exception as e:  # noqa: BLE001
        st.error(f"上傳時發生問題：{e}")
        return
    st.session_state.uploaded_job_code = code
    st.success(
        f"✅ 送出成功！你的認領代碼是 **{code}**\n\n"
        "角色會在數小時內烤好，屆時回到「🎨 我的自訂頭像」下拉選這個代碼即可。"
    )


def render_photo_upload_ui(embed: bool = False) -> None:
    """側邊欄：上傳照片 → 送進待烤佇列。embed=True 時只渲染內容（供嵌進其他 expander，Streamlit 不允許 expander 巢狀）。"""
    def _body():
        st.caption(
            "上傳一張正面清楚的照片，我們會用 THA3 幫你烤成會動的專屬角色。"
            "⚠️ 動漫／插畫風效果最好；**真人自拍**可能略有落差。"
        )

        queue = _get_drive_queue()
        if queue is None:
            st.warning("目前無法連上雲端儲存，請稍後再試。")
            return

        last_code = st.session_state.get("uploaded_job_code")
        if last_code:
            _render_job_status(queue, last_code)
            st.markdown("---")

        photo = st.file_uploader(
            "選擇照片（png / jpg / webp，上限 5MB）",
            type=["png", "jpg", "jpeg", "webp"],
            key="companion_photo_uploader",
        )
        nickname = st.text_input(
            "幫這個角色取個名字（可留白）",
            key="companion_photo_nick",
            max_chars=40,
            placeholder="例如：mybeibei",
        )
        agree = st.checkbox(
            "我了解照片會暫存於雲端供製作、完成後即刪除，且我有權使用這張照片。",
            key="companion_photo_consent",
        )

        disabled = (photo is None) or (not agree)
        if st.button("🚀 送出製作", key="companion_photo_submit",
                     disabled=disabled, use_container_width=True):
            _submit_photo(queue, photo, nickname)

    if embed:
        _body()
    else:
        with st.sidebar.expander("📷 上傳照片訂製角色", expanded=False):
            _body()


def show_companion() -> None:
    st.button("⬅️ 取消並返回功能大廳", on_click=go_to, args=("home",))
    st.markdown(
        "<h2 style='color:#FF8A80;'>🐼 模式二：陪伴貝貝 (Live2D AI 即時陪伴)</h2>",
        unsafe_allow_html=True,
    )

    # ---- session 初始化 ----
    st.session_state.setdefault("chat_history", [])
    st.session_state.setdefault("latest_audio", None)
    st.session_state.setdefault("last_processed_audio", None)
    st.session_state.setdefault("last_click_id", None)
    st.session_state.setdefault("beibei_voice_gender", "female")
    st.session_state.setdefault("beibei_emotion", "neutral")

    # ================= 側邊欄：外觀與聲音 =================
    selected_baked = None   # 選到的預烤頭像名稱（None = 用原生 Live2D）
    with st.sidebar.expander("✨ 角色外觀設定", expanded=True):
        avatar_mode = st.radio(
            "請選擇陪伴模式：",
            ["🎀 原本的貝貝 (Live2D)", "🎨 我的自訂頭像 (THA3 預烤)"],
            key="avatar_mode_radio_key",
        )
        if avatar_mode.startswith("🎨"):
            baked_list = list_baked_avatars()
            if not baked_list:
                st.info(
                    "還沒有預烤好的自訂頭像。\n\n"
                    "請在**本地端**用 `python bake_avatar.py 你的照片.jpg 名稱` 烤好，"
                    "把產生的 `baked_avatars/名稱.json` 一起上傳到雲端 repo，這裡就會出現可選。\n\n"
                    "（雲端不跑神經網路，只播放你預烤好的成品。）"
                )
            else:
                selected_baked = st.selectbox("選擇自訂頭像：", baked_list, key="baked_avatar_select")
                st.caption(f"🎨 目前使用預烤頭像：**{selected_baked}**")

        st.markdown("---")
        st.markdown("##### 📷 上傳照片訂製角色")
        render_photo_upload_ui(embed=True)

    # ================= 側邊欄：貝貝的聲音（獨立一欄）=================
    with st.sidebar.expander("🔊 貝貝的聲音", expanded=False):
        _voice_label = st.radio(
            "選擇陪伴聲線：",
            ["👧 女生 (曉曉)", "👦 男生 (雲希)"],
            key="beibei_voice_label",
            horizontal=True,
        )
        st.session_state.beibei_voice_gender = "male" if "男生" in _voice_label else "female"

    # ================= 側邊欄：互動行為模式 =================
    with st.sidebar.expander("⚙️ 互動行為模式", expanded=False):
        companion_mode = st.radio(
            "請選擇貝貝的行為模式：",
            ["🎭 模式一：自主行動 (發呆)", "🪞 模式二：VR 臉部同步 (跟著你轉頭)"],
            key="live2d_mode_radio",
        )
        mode_code = 1 if "模式一" in companion_mode else 2

    # ================= 側邊欄：主動搭話（跨重啟記憶）=================
    with st.sidebar.expander("💬 主動搭話設定", expanded=False):
        if "idle_chat_enabled" not in st.session_state:
            st.session_state.idle_chat_enabled = load_live2d_settings()["idle_chat_enabled"]
        idle_chat_enabled = st.toggle(
            "🌸 開啟「貝貝主動找你聊天」",
            key="idle_chat_enabled",   # 不放 value=，避免每次 rerun 蓋掉讀回值
            help="開啟後，若你超過 5 分鐘沒理貝貝，她會主動跳出來勾搭你～",
        )
        _saved = load_live2d_settings()
        if _saved.get("idle_chat_enabled") != idle_chat_enabled:
            _saved["idle_chat_enabled"] = idle_chat_enabled
            save_live2d_settings(_saved)
        st.caption("🟢 發呆太久貝貝會主動關心你！" if idle_chat_enabled else "⚪ 已靜音～貝貝會乖乖等你開口。")

    # ================= 側邊欄：對話框外觀（跨重整記憶）=================
    with st.sidebar.expander("💬 對話框外觀", expanded=False):
        # 先把存檔讀回來，當作各設定的初始值（沒放 value=，才不會每次 rerun 蓋掉讀回值）
        _appear = load_live2d_settings()
        for _ak in ("chat_user_emoji", "chat_custom_bg", "chat_bg_color",
                    "chat_bubble_color", "chat_user_avatar_path"):
            if _ak not in st.session_state:
                st.session_state[_ak] = _appear.get(_ak, DEFAULT_LIVE2D_SETTINGS[_ak])
        chat_user_emoji = st.selectbox(
            "🧑 主人的頭像（未上傳圖片時使用）", ["🧑", "😀", "🧑‍💻", "👩", "👨", "🐱", "🐰", "⭐"],
            key="chat_user_emoji",
        )

        # ---- 上傳自己的聊天頭像（會縮成小圖存起來，跨重整記憶）----
        _up_av = st.file_uploader(
            "🖼️ 或上傳自己的頭像 (PNG/JPG)", type=["png", "jpg", "jpeg"],
            key="chat_user_avatar_upload",
        )
        if _up_av is not None:
            _sig = f"{_up_av.name}:{_up_av.size}"
            if st.session_state.get("_chat_av_sig") != _sig:
                try:
                    from PIL import Image
                    _img = Image.open(_up_av).convert("RGBA")
                    _img.thumbnail((96, 96))          # 縮小，避免拖慢頁面
                    os.makedirs(SAVED_DIR, exist_ok=True)
                    _av_path = os.path.join(SAVED_DIR, "user_chat_avatar.png")
                    _img.save(_av_path, "PNG")
                    st.session_state["chat_user_avatar_path"] = _av_path
                    st.session_state["_chat_av_sig"] = _sig
                    _sv = load_live2d_settings()
                    _sv["chat_user_avatar_path"] = _av_path
                    save_live2d_settings(_sv)
                    st.success("頭像已更新！")
                except Exception as _e:
                    st.error(f"頭像處理失敗：{_e}")
        if st.session_state.get("chat_user_avatar_path"):
            st.caption("目前使用上傳的頭像。")
            if st.button("↩️ 改回用表情符號", key="clear_chat_avatar"):
                st.session_state["chat_user_avatar_path"] = ""
                st.session_state.pop("_chat_av_sig", None)
                _sv = load_live2d_settings()
                _sv["chat_user_avatar_path"] = ""
                save_live2d_settings(_sv)
                st.rerun()

        st.markdown("---")
        chat_custom_bg = st.toggle(
            "🎨 啟用自訂對話框背景", key="chat_custom_bg",
            help="關閉時維持預設外觀；開啟後才套用下面挑的顏色。",
        )
        chat_bg_color = st.color_picker("對話框底色", key="chat_bg_color")
        chat_bubble_color = st.color_picker("訊息泡泡底色", key="chat_bubble_color")
        # 有變更就存檔，重整後才讀得回來
        _new_appear = {
            "chat_user_emoji": chat_user_emoji,
            "chat_custom_bg": chat_custom_bg,
            "chat_bg_color": chat_bg_color,
            "chat_bubble_color": chat_bubble_color,
        }
        if any(_appear.get(_k) != _v for _k, _v in _new_appear.items()):
            _saved_all = load_live2d_settings()
            _saved_all.update(_new_appear)
            save_live2d_settings(_saved_all)

    # ================= 載入預烤頭像（若有選）=================
    ai_b64_data = None
    if selected_baked:
        try:
            ai_b64_data = get_ai_webp(
                selected_baked, st.session_state.get("beibei_emotion", "neutral")
            )
        except Exception as e:   # noqa: BLE001
            print(f"[companion] get_ai_webp 失敗: {e}")
            st.sidebar.error(f"⚠️ 讀取頭像「{selected_baked}」失敗，改用原生 Live2D。")

    # ================= 側邊欄：位置與大小（per-avatar 記憶）=================
    current_model_id = f"baked::{selected_baked}" if selected_baked else "__live2d__"
    _all_positions = load_avatar_positions()
    _pos = {**DEFAULT_AVATAR_POS, **_all_positions.get(current_model_id, {})}
    _scale_key = f"pos_scale::{current_model_id}"
    _x_key = f"pos_x::{current_model_id}"
    _y_key = f"pos_y::{current_model_id}"

    with st.sidebar.expander("📐 角色位置與大小調整", expanded=False):
        _label = selected_baked if selected_baked else "原本的貝貝 (Live2D)"
        st.caption(f"🎯 正在調整：**{_label}**")
        model_scale = st.slider("🔍 大小縮放", 0.5, 3.0, value=float(_pos["scale"]), step=0.1, key=_scale_key)
        model_x = st.slider("↔️ 左右平移", -500, 500, value=int(_pos["x"]), step=10, key=_x_key)
        model_y = st.slider("↕️ 上下平移", -500, 500, value=int(_pos["y"]), step=10, key=_y_key)
        _new_pos = {"scale": model_scale, "x": model_x, "y": model_y}
        if _all_positions.get(current_model_id) != _new_pos:
            _all_positions[current_model_id] = _new_pos
            save_avatar_positions(_all_positions)
        if st.button("↩️ 重設位置", key=f"pos_reset::{current_model_id}"):
            _all_positions.pop(current_model_id, None)
            save_avatar_positions(_all_positions)
            for _k in (_scale_key, _x_key, _y_key):
                st.session_state.pop(_k, None)
            st.rerun()

    # ================= 主畫面：左 Live2D，右 聊天 =================
    col_left, col_right = st.columns([1.5, 1])

    with col_left:
        assets_dir = os.path.join("live2d_component", "frontend", "assets")
        os.makedirs(assets_dir, exist_ok=True)
        valid_exts = [".jpg", ".jpeg", ".png"]

        with st.expander("🖼️ 背景管理 (上傳與刪除)"):
            up = st.file_uploader("上傳新背景 (JPG/PNG)", type=["jpg", "jpeg", "png"], label_visibility="collapsed")
            custom_name = st.text_input("為背景取個名字 (選填)", placeholder="例如：我的房間")
            if up is not None and st.button("確認上傳", type="primary"):
                ext = os.path.splitext(up.name)[1]
                fname = f"{custom_name.strip()}{ext}" if custom_name.strip() else up.name
                fpath = os.path.join(assets_dir, fname)
                if os.path.exists(fpath):
                    st.error(f"⚠️ 已有叫「{fname}」的背景，請換個名字！")
                else:
                    with open(fpath, "wb") as f:
                        f.write(up.getbuffer())
                    st.success(f"✅ 成功上傳：{fname}")
                    time.sleep(1)
                    st.rerun()
            st.caption("（雲端版背景檔為暫存，App 重啟後會回到預設；本地版則永久保存。）")

        bg_files = [f for f in os.listdir(assets_dir)
                    if os.path.isfile(os.path.join(assets_dir, f))
                    and os.path.splitext(f)[1].lower() in valid_exts]
        bg_choice = st.selectbox("切換背景：", ["純白背景"] + bg_files)
        selected_bg = "" if bg_choice == "純白背景" else bg_choice

        # 給 Live2D 元件容器加一個淡黑框
        st.markdown(
            """
            <style>
            [data-testid="stVerticalBlockBorderWrapper"]:has(iframe){
                border:1px solid rgba(0,0,0,0.35) !important; border-radius:14px !important;
            }
            </style>
            """,
            unsafe_allow_html=True,
        )

        with st.container(border=True):
            beibei_event = render_beibei(
                audio_data=st.session_state.get("latest_audio", "") or "",
                bg_image=selected_bg,
                ai_webp=ai_b64_data,         # None=原生 Live2D；有值=播放預烤 THA3
                companion_mode=mode_code,
                model_scale=model_scale,
                model_x=model_x,
                model_y=model_y,
            )

        # ---- 事件處理：點擊 / 打瞌睡 / 主動搭話 ----
        if beibei_event:
            event_id = str(beibei_event)
            if event_id != st.session_state.last_click_id:
                st.session_state.last_click_id = event_id

                if "clicked_" in event_id:
                    with st.spinner("貝貝在想怎麼回你..."):
                        prompt = ("（使用者用滑鼠『戳』了你。請扮演貼心的日系少女『貝貝』給出一句撒嬌或驚訝的反應。"
                                  "請使用「戳」「摸」「碰」等正常動詞，嚴禁說出「排列」等電腦詞彙！"
                                  "繁中回覆，25 字內，可加顏文字。）")
                        reply = get_ai_response(prompt)
                        st.session_state.chat_history.append({"role": "assistant", "content": reply})
                        st.session_state.latest_audio = _speak(reply)
                        st.rerun()

                elif "drowsy_" in event_id:
                    with st.spinner("⚠️ 發現你打瞌睡！貝貝正在找話題叫醒你..."):
                        prompt = ("（系統偵測到使用者正在打瞌睡。請扮演貼心的少女『貝貝』，語氣自然流暢，"
                                  "先叫醒使用者，接著立刻拋出一個有趣的話題或問題（例如今天天氣、最近過得好不好），"
                                  "強迫他回話以保持清醒。繁中回覆，並以一個問句結尾！）")
                        reply = get_ai_response(prompt)
                        st.session_state.chat_history.append(
                            {"role": "assistant", "content": f"🚨 【打瞌睡警報】\n\n{reply}"})
                        st.session_state.latest_audio = _speak(reply)
                        st.rerun()

                elif "idle_" in event_id and st.session_state.get("idle_chat_enabled", True):
                    with st.spinner("🌸 貝貝覺得有點寂寞，正在主動找你聊天..."):
                        prompt = ("（你發現主人已經 5 分鐘沒理你了。請扮演活潑貼心的日系少女『貝貝』主動勾搭主人、"
                                  "引導他說話，語氣帶點傲嬌或溫柔關心。繁中回覆，句尾必須是問句，35 字內，要加可愛顏文字。）")
                        reply = get_ai_response(prompt)
                        st.session_state.chat_history.append(
                            {"role": "assistant", "content": f"🌸 【主動關懷】\n\n{reply}"})
                        st.session_state.latest_audio = _speak(reply)
                        st.rerun()

    with col_right:
        st.markdown("<br>" * 3, unsafe_allow_html=True)
        st.subheader("💬 與貝貝聊天")

        beibei_chat_avatar = "🐼"   # 貝貝聊天頭像用熊貓
        user_emoji = st.session_state.get("chat_user_emoji", "🧑")

        # 貝貝頭像（左）
        beibei_av_html = f'<span style="font-size:24px;line-height:1;">{beibei_chat_avatar}</span>'
        # 使用者頭像（右）：優先用上傳的小圖，沒有就用表情符號
        _user_av_path = st.session_state.get("chat_user_avatar_path", "")
        user_av_html = f'<span style="font-size:24px;line-height:1;">{html.escape(user_emoji)}</span>'
        if _user_av_path and os.path.exists(_user_av_path):
            try:
                _b64 = base64.b64encode(open(_user_av_path, "rb").read()).decode("ascii")
                user_av_html = (
                    f'<img src="data:image/png;base64,{_b64}" '
                    'style="width:32px;height:32px;border-radius:50%;object-fit:cover;">'
                )
            except OSError:
                pass

        # 顏色：開了自訂就用使用者選的，否則用預設
        if st.session_state.get("chat_custom_bg", False):
            _bg = st.session_state.get("chat_bg_color", "#FFF5F5")
            _bubble = st.session_state.get("chat_bubble_color", "#FFFFFF")
        else:
            _bg, _bubble = "transparent", "#FFFFFF"
        st.markdown(
            f"""<style>
            .st-key-beibei_chat_box {{ background:{_bg} !important; border-radius:14px !important; padding:8px 12px !important; }}
            </style>""",
            unsafe_allow_html=True,
        )

        chat_container = st.container(height=400, key="beibei_chat_box")
        with chat_container:
            for msg in st.session_state.chat_history:
                _is_user = msg["role"] != "assistant"
                _content = html.escape(str(msg["content"])).replace("\n", "<br>")
                _av = user_av_html if _is_user else beibei_av_html
                _dir = "row-reverse" if _is_user else "row"
                _justify = "flex-end" if _is_user else "flex-start"
                st.markdown(
                    f"""
                    <div style="display:flex;justify-content:{_justify};margin:8px 0;padding:0 10px;box-sizing:border-box;">
                      <div style="display:flex;flex-direction:{_dir};align-items:flex-start;gap:8px;max-width:78%;">
                        <div style="flex:0 0 auto;">{_av}</div>
                        <div style="background:{_bubble};color:#222;padding:8px 12px;border-radius:14px;overflow-wrap:anywhere;">{_content}</div>
                      </div>
                    </div>
                    """,
                    unsafe_allow_html=True,
                )

        col_text, col_mic = st.columns([4, 1])
        with col_text:
            user_input = st.chat_input("想對貝貝說什麼？")
        with col_mic:
            try:
                from audio_recorder_streamlit import audio_recorder
                audio_bytes = audio_recorder(text="", icon_size="2x", icon_name="microphone")
            except Exception:
                audio_bytes = None
                st.caption("🎤×")

        is_new_text = bool(user_input)
        is_new_audio = (audio_bytes is not None) and (audio_bytes != st.session_state.last_processed_audio)

        if is_new_text or is_new_audio:
            with st.spinner("貝貝正在豎起耳朵聽..."):
                if is_new_audio:
                    st.session_state.last_processed_audio = audio_bytes
                    user_display = "🎤 (語音訊息)"
                    reply = get_ai_response(audio_bytes, is_audio=True)
                else:
                    user_display = user_input
                    reply = get_ai_response(user_input, is_audio=False)

                st.session_state.chat_history.append({"role": "user", "content": user_display})
                st.session_state.chat_history.append({"role": "assistant", "content": reply})
                st.session_state.beibei_emotion = classify_emotion(reply)
                st.session_state.latest_audio = _speak(reply)
                st.rerun()
