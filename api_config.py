# =====================================================
# api_config.py — Gemini API 金鑰集中管理（雲端 / 本地雙來源）
#
# 整合說明（與本地版差異）：
#   1. 雲端優先：先讀 Streamlit Secrets（st.secrets），再讀環境變數 / .env。
#      Streamlit Community Cloud 的 Secrets 不會自動進入 os.environ，
#      所以必須主動讀 st.secrets，否則雲端拿不到金鑰。
#   2. 缺金鑰「不致命」：本地版在 import 階段就 raise，會讓整個 app 一開首頁就崩。
#      整合後改成回傳空清單 []，讓「模式一/三」等不需要 Gemini 的頁面照常運作；
#      只有「模式二 陪伴貝貝」真的要用到時，才由 get_ai_response 友善提示。
#
# 在 Streamlit Cloud 設定（App → Settings → Secrets）範例：
#   GEMINI_API_KEY_1 = "xxxx"
#   GEMINI_API_KEY_2 = "yyyy"   # 可選備援
# 本地開發則放在 .env：GEMINI_API_KEY_1=xxxx
# =====================================================
import os


def _from_env() -> list[str]:
    """從環境變數 / .env 讀取金鑰。"""
    try:
        from dotenv import load_dotenv
        load_dotenv()  # 本地有 .env 就載入；雲端沒有也不會壞
    except Exception:
        pass

    keys: list[str] = []
    for i in range(1, 6):  # 最多 5 把備援
        key = os.getenv(f"GEMINI_API_KEY_{i}")
        if key and key.strip():
            keys.append(key.strip())
    if not keys:
        key = os.getenv("GEMINI_API_KEY")
        if key and key.strip():
            keys.append(key.strip())
    return keys


def _from_secrets() -> list[str]:
    """從 Streamlit Secrets 讀取金鑰（雲端部署用）。"""
    keys: list[str] = []
    try:
        import streamlit as st
        # 存取 st.secrets 在沒有 secrets.toml 時可能拋例外，包起來
        for i in range(1, 6):
            name = f"GEMINI_API_KEY_{i}"
            if name in st.secrets:
                val = str(st.secrets[name]).strip()
                if val:
                    keys.append(val)
        if not keys and "GEMINI_API_KEY" in st.secrets:
            val = str(st.secrets["GEMINI_API_KEY"]).strip()
            if val:
                keys.append(val)
    except Exception:
        pass
    return keys


def get_api_keys() -> list[str]:
    """雲端優先、環境變數其次。找不到時回傳空清單（不 raise）。"""
    keys = _from_secrets()
    if not keys:
        keys = _from_env()
    return keys


# 向下相容：保留 API_KEYS 名稱。雲端可能在 import 時還讀不到 secrets，
# 所以真正使用時建議呼叫 get_api_keys() 重新取一次（get_ai_response 已這樣做）。
API_KEYS = get_api_keys()
