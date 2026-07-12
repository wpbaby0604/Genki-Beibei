# 整合說明 — 模式二「陪伴貝貝 (Live2D)」併入雲端版

> 本檔記錄把本地 Live2D 陪伴功能整合進雲端版（`Genki_Cloud_DataVersion` v2.6）的全部改動、設計決策、待測項與部署步驟。
> 整合日：2026-06。整合範圍 = **原生 Live2D 陪伴（可上雲）**；THA3 自訂頭像維持本地限定（理由見下）。

---

## 1. 一句話總結

雲端版首頁那顆「模式二・陪伴貝貝（維護中）」的死按鈕，現在接上了真正的 **Live2D AI 即時陪伴頁**：瀏覽器視訊臉部追蹤 + 語音/文字聊天（Gemini）+ 貝貝語音回覆（Edge TTS）+ 打瞌睡提醒 + 主動搭話。整頁 Live2D 與臉部追蹤都跑在瀏覽器 iframe 裡（函式庫走 CDN），所以能在 Streamlit Community Cloud 上運作。

---

## 2. 改了哪些檔案

| 檔案 | 動作 | 內容 |
|---|---|---|
| `companion_live2d.py` | **新增** | 模式二頁面本體 `show_companion()` + Gemini 聊天 + Edge TTS 語音 + 偏好持久化。由本地 `show_live2d_chat()` 移植、改寫成雲端相容版。 |
| `api_config.py` | **新增** | Gemini 金鑰讀取：**雲端 Secrets 優先、`.env` 其次**；缺金鑰「不致命」（不會讓整個 app 崩）。 |
| `live2d_component/` | **新增（整包搬入）** | Live2D 自訂元件：`__init__.py` + `frontend/`（`index.html`、`main.js`、模型 `rice_pc_pro_t02.*`）。**未改動程式**，只清掉兩張測試殘圖。 |
| `main.py` | **修改** | `import show_companion`；`ROUTES` 加 `"companion"`；`PROTECTED_PAGES` 加 `"companion"`（需登入才進）。 |
| `ui_pages.py` | **修改（僅一處）** | `show_home()` 把模式二按鈕從 `disabled=True` 改成 `on_click=go_to,("companion",)`，文案去掉「維護中」。**其餘雲端頁面一字未動。** |
| `requirements.txt` | **修改** | 追加 `google-genai`、`edge-tts`、`audio-recorder-streamlit`、`nest_asyncio`、`python-dotenv`。 |
| `.gitignore` | **修改** | 追加 `saved_avatars/`、`temp_reply.mp3` 等執行期暫存。 |
| `.streamlit/secrets.toml.example` | **新增** | 金鑰範本（Supabase/HMAC + Gemini）。 |

> 第一部分（雲端版）的資料層、安全鏈、CV、儀表板 **完全沒動**。陪伴頁是獨立模組，唯一耦合點是 `core_data.go_to`。

---

## 3. 關鍵設計決策（為什麼這樣做）

**① 為什麼只搬「原生 Live2D」，不搬 THA3。**
你的本地版有兩條動畫線：原生 Live2D（PIXI + Cubism，瀏覽器即時）與 THA3（神經網路把自訂照片算成動畫）。THA3 需要 `torch` + 500MB 權重 + CPU 逐幀推論（首次烤製 8–12 分鐘），Streamlit 免費版資源與檔案大小都扛不住；而原生 Live2D 整包跑在瀏覽器、函式庫走 CDN、模型只有 ~3MB，天生適合上雲。所以雲端走原生 Live2D，THA3 留在你的本地 app。介面上「上傳自訂圖片」入口仍在，但會提示「本地限定」並自動回退到 Live2D 貝貝。

**② 為什麼新開 `companion_live2d.py` 而不是塞進 `ui_pages.py`。**
本地與雲端的 `ui_pages.py` 是**同源但分岔的兩個 fork**（本地用本地 CSV、無 HMAC；雲端用 Supabase + 簽章鏈），直接覆蓋會毀掉雲端資料層。而且本地 `ui_pages.py` 頂層 `import torch` / `from tha3...`，一旦併進去，雲端沒裝 torch 會讓**整個 app import 失敗**。獨立成模組可完全隔離這風險，耦合面也最小。

**③ 為什麼不升 Streamlit 版本。**
本地版跑 1.53.1、用了新 API（`width="stretch"`）；雲端鎖 1.41.1。為了不動搖既有可運作的雲端頁，我把陪伴頁改寫成 1.41.1 相容寫法（`use_container_width=True`），維持雲端版本不變。

**④ 金鑰安全。**
本地 `api_config.py` 在 import 就 `raise`（沒金鑰整個 app 崩）。雲端版改成讀不到就回空清單，只有真的要聊天時才友善提示——這樣模式一/三在沒設 Gemini 金鑰時照常能用。

---

## 4. 部署前你要做的事（重要）

1. **設定 Secrets**（Streamlit Cloud → App → Settings → Secrets），照 `.streamlit/secrets.toml.example`：
   - 原有：`SUPABASE_URL`、`SUPABASE_KEY`、`HMAC_SECRET`
   - 新增：至少一把 `GEMINI_API_KEY_1`
2. **首次部署在 Advanced settings 選 Python 3.11**（沿用雲端版既有限制 KI-5）。
3. Gemini 金鑰請到 AI Studio 確認 `gemini-2.5-flash` 對你的專案仍可用（model 名在 `companion_live2d.py` 的 `GEMINI_MODEL`，要換在那裡改）。

---

## 5. 待測清單（部署後請逐項驗）

- [ ] 首頁模式二按鈕可點 → 進入「🐼 模式二：陪伴貝貝」頁。
- [ ] Live2D 貝貝（rice 模型）有出現在左欄、會呼吸、可點擊。
- [ ] 允許瀏覽器**鏡頭權限**後，切「模式二：VR 臉部同步」→ 貝貝跟你轉頭、眨眼。
- [ ] 右欄打字送出 → 貝貝有文字回覆 + 出聲（首次播放可能要先點一下畫面解除瀏覽器自動播放限制）。
- [ ] 側邊欄切男聲/女聲 → 再講一句，聲線有換。
- [ ] 閉眼約 3 秒 → 跳「打瞌睡警報」並語音叫你。
- [ ] 既有三個模式（檢測打卡 / 儀表板 / 登入）功能不受影響。

---

## 6. 已知限制與風險（誠實標註）

- **雲端為暫態儲存**：背景圖、`saved_avatars/` 的位置與設定 json 在 App 重啟/重部署後會清空、回預設。要永久保存需另接雲端儲存（可比照第一部分的 Supabase）。
- **套件相依衝突可能性**：`google-genai` 會帶入 `pydantic v2`、`google-auth` 等，與 `supabase==2.5.1` 的相依在極端情況可能版本打架。若部署 build 失敗，多半出在這裡——可回報 pip 的衝突訊息，我再幫你釘版本。
- **`google-genai` 版本**：requirements 用 `>=0.8.0`。本地舊 `requirements.txt` 寫的是 `google-generativeai`（套件名不同、API 也不同），實際程式用的是新版 `from google import genai`，所以雲端用 `google-genai` 才對。
- **Edge TTS 走微軟線上服務**：雲端要能對外連線（一般沒問題），偶有節流。
- **THA3 自訂頭像**：雲端停用（見決策①），此功能請在本地 app 使用。
- **`main.js` 的舊技術債照舊**：音訊節點未釋放的長跑記憶體問題、`onResults` 無條件寫 `ParamAngleX/Y` 等，沿用本地現況未動（屬「原生 Live2D / 聲音輸出既有流程勿動」的範圍）。

---

## 7. 之後可以接著做（如果你要）

- 把 THA3 自訂頭像做成「本地專屬」分支：偵測 `torch` 在不在，在就顯示上傳/烤製，不在就走 Live2D（讓同一份碼本地跑全功能、雲端跑精簡版）。
- 把陪伴聊天紀錄/設定接到 Supabase，解決雲端暫態問題。
- 模式二（VR 同步）目前是原生 Live2D 的頭部 `ParamAngleX/Y` + 眼睛開合；要更細的表情（嘴型隨語音、歪頭）可再加參數。
