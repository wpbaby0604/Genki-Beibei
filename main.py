# ============================================================
#  main.py  —  Genki Beibei v2.3 (Hardened Routing Edition)
# ============================================================
import streamlit as st

st.set_page_config(
    page_title="元氣貝貝 Genki Beibei",
    layout="wide",
    page_icon="🌱",
)

import urllib.parse  # noqa: E402

from core_data import (  # noqa: E402
    set_bg_from_local,
    save_full_pipeline_data,
    validate_url_payload,
    verify_nonce,
)
from ui_pages import (  # noqa: E402
    show_landing,
    show_profile,
    show_home,
    show_analyzer,
    show_pvt_game,
    show_dashboard,
)
# 🐼 模式二 陪伴貝貝（Live2D AI 即時陪伴）— 整合自本地 Live2D 專案
from companion_live2d import show_companion  # noqa: E402

# ----------------- 背景與 Session State -----------------
set_bg_from_local("bg.png")

_defaults = {
    "page": "landing",
    "user_name": "",
    "user_age": 22,
    "user_job": "學生",
    "temp_analysis_data": None,
    "test_mode": "stroop",
    "logged_in": False,
}
for k, v in _defaults.items():
    if k not in st.session_state:
        st.session_state[k] = v

# ============================================================
#  🔧 本機測試開關（測模式二陪伴貝貝用）
#  設 True：跳過登入，直接進功能大廳，免設定 Supabase。
#  正式部署到雲端前請務必改回 False，否則任何人都能免登入進入。
# ============================================================
LOCAL_TEST_SKIP_LOGIN = True
if LOCAL_TEST_SKIP_LOGIN and not (st.session_state.get("user_name") or "").strip():
    st.session_state.user_name = "tester"
    st.session_state.logged_in = True
    if st.session_state.page in ("landing", "profile"):
        st.session_state.page = "home"


# ==========================================================
#  URL 攔截器 — 存檔 API
# ==========================================================
def _get_query_params():
    """跨版本相容地取得 query params。"""
    try:
        qp = st.query_params
        return qp, ("save" in qp)
    except AttributeError:
        qp = st.experimental_get_query_params()
        return qp, ("save" in qp)


def _clear_query_params():
    """跨版本相容地清空 query params。"""
    try:
        st.query_params.clear()
    except AttributeError:
        st.experimental_set_query_params()


def _qp_val(qp, k: str):
    v = qp.get(k)
    if v is None:
        return None
    if isinstance(v, list):
        return v[0] if v else None
    return v


qp, has_save = _get_query_params()

if has_save:
    raw_user = _qp_val(qp, "u") or ""
    user = urllib.parse.unquote(raw_user)[:64].strip()

    raw_payload = {
        "sleep_h":        _qp_val(qp, "sl"),
        "fatigue":        _qp_val(qp, "fa"),
        "delta_E":        _qp_val(qp, "de"),
        "delta_L":        _qp_val(qp, "dl"),
        "sclera_a":       _qp_val(qp, "sa"),
        "lid_puff":       _qp_val(qp, "lp"),
        "cheek_L":        _qp_val(qp, "cl"),
        "rt_mean":        _qp_val(qp, "rt"),
        "rt_congruent":   _qp_val(qp, "rc"),
        "rt_incongruent": _qp_val(qp, "ri"),
        "interference":   _qp_val(qp, "it"),
        "lapses":         _qp_val(qp, "la"),
        "false_starts":   _qp_val(qp, "fs"),
        "valid_trials":   _qp_val(qp, "vt"),
    }
    nonce = _qp_val(qp, "nc") or ""

    clean, err = validate_url_payload(raw_payload)

    if err is not None or not user:
        st.sidebar.error(f"🚫 存檔被拒絕：{err or '使用者名稱缺失'}")
        _clear_query_params()
    else:
        # nonce 簽章涵蓋 u/sleep_h/fatigue/delta_E/delta_L (皆為測驗前即已知的值),
        # 詳細設計理由請參見 core_data.py 中的 NONCE_FIELDS 註解。
        # RT 等測驗結果欄位的安全性由 _PAYLOAD_SCHEMA 範圍驗證守護。
        verify_dict = {
            "u":       user,
            "sleep_h": clean["sleep_h"],
            "fatigue": clean["fatigue"],
            "delta_E": clean["delta_E"],
            "delta_L": clean["delta_L"],
            "sclera_a": clean["sclera_a"],
            "lid_puff": clean["lid_puff"],
            "cheek_L":  clean["cheek_L"],
        }

        # 安全模型說明:
        #   防偽造攻擊的核心是 HMAC nonce — 因為 nonce 已涵蓋 u 欄位且簽章
        #   密鑰 HMAC_SECRET 不外洩, 攻擊者無法偽造任一使用者的存檔 URL。
        #   原本檢查「session_user == url_user」會在 iframe target=_top 跳轉
        #   後 kernel 重啟、session 流失時誤觸發, 因此改為「HMAC 通過即信任」。
        if not verify_nonce(verify_dict, nonce):
            st.sidebar.error("🚫 存檔被拒絕：完整性檢查 (HMAC) 失敗。")
            _clear_query_params()
        else:
            extra = st.session_state.get("temp_analysis_data") or {}
            ok = save_full_pipeline_data(
                name=user,
                sleep=clean["sleep_h"],
                fatigue=clean["fatigue"],
                delta_e=clean["delta_E"],
                delta_l=clean["delta_L"],
                sclera_a=clean["sclera_a"],
                lid_puff=clean["lid_puff"],
                cheek_l=clean["cheek_L"],
                rt_mean=clean["rt_mean"],
                rt_congruent=clean["rt_congruent"],
                rt_incongruent=clean["rt_incongruent"],
                interference=clean["interference"],
                lapses=clean["lapses"],
                false_starts=clean["false_starts"],
                valid_trials=clean["valid_trials"],
                delta_e_left=extra.get("delta_E_left"),
                delta_e_right=extra.get("delta_E_right"),
                asymmetry=extra.get("asymmetry"),
                custom_date=extra.get("custom_date"),  # 補登歷史資料用; None 則寫入當下時間
            )
            if ok:
                st.sidebar.success("✅ 已成功同步至 Supabase 雲端。")
            else:
                st.sidebar.error("❌ 雲端資料同步失敗，請檢查資料表架構。")

            st.session_state.user_name = user
            st.session_state.temp_analysis_data = None
            st.session_state.page = "dashboard"
            _clear_query_params()
            st.rerun()


# ==========================================================
#  路由控制器（含登入態守門）
# ==========================================================
ROUTES = {
    "landing":   show_landing,
    "profile":   show_profile,
    "home":      show_home,
    "analyzer":  show_analyzer,
    "pvt_game":  show_pvt_game,
    "dashboard": show_dashboard,
    "companion": show_companion,   # 🐼 模式二 陪伴貝貝
}

# 未登入時禁止直接進入需要驗身的內頁
PROTECTED_PAGES = {"home", "analyzer", "pvt_game", "dashboard", "companion"}

current_page = st.session_state.page
if current_page in PROTECTED_PAGES and not (st.session_state.get("user_name") or "").strip():
    st.session_state.page = "profile"
    current_page = "profile"

view = ROUTES.get(current_page, show_landing)
view()
