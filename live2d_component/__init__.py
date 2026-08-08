import os
import streamlit.components.v1 as components

# 取得目前檔案所在的目錄
_parent_dir = os.path.dirname(os.path.abspath(__file__))
# 指向 frontend 資料夾
_frontend_dir = os.path.join(_parent_dir, "frontend")

# 🌟 修正：宣告元件時，必須把它存進 _component_func 這個變數裡！
_component_func = components.declare_component("render_beibei", path=_frontend_dir)

# 🌟 修正 1：在括號裡加上 companion_mode: int = 1 的預設參數
def render_beibei(audio_data: str = "", bg_image: str = "", ai_webp: str = None, companion_mode: int = 1, model_scale: float = 1.0, model_x: int = 0, model_y: int = 0, key=None, emotion: str = "neutral", forced_action: str = ""):
    component_value = _component_func(
        audio_data=audio_data,
        bg_image=bg_image,
        ai_webp=ai_webp,
        companion_mode=companion_mode,  # 🌟 修正 2：把它當作包裹交給底層的 _component_func
        model_scale=model_scale, # 🌟 裝進包裹
        model_x=model_x,         # 🌟 裝進包裹
        model_y=model_y,         # 🌟 裝進包裹
        emotion=emotion,         # 🆕 目前情緒（除錯用標籤，前端拿去顯示目前狀態）
        forced_action=forced_action,  # 🔧 除錯：強制顯示某個表情/動作（""＝正常自動）
        key=key,
        default=0
    )
    return component_value
