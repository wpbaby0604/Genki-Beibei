# 元氣貝貝 v2.6 — 複合疲勞臉指數 (多特徵融合)

承 v2.5 的「ΔL\* 方向指標 + 個人基線 z-score」, 本次新增三個臉部特徵並把
四項特徵融合成單一「疲勞臉指數」, 對應 Sundelin/Axelsson (2013) 的多線索框架。

## 第一批 — 特徵擴充 (`core_cv.py`)

新增三個量測 (MediaPipe landmark, 不換技術棧):
- **鞏膜泛紅 `sclera_a`**：眼白區 (眼睛輪廓多邊形扣掉虹膜圓) 的 a\* 中位數,
  越高越紅。新增 `extract_sclera_redness()`。
- **下眼瞼浮腫 `lid_puffiness`**：面頰 L\* − 下眼瞼帶 L\* 的陰影代理量,
  越大越浮腫。新增 `extract_lid_puffiness()`。純 2D 無法直接量 3D 突起,
  此為保守代理。
- **面頰蒼白 `cheek_pallor`**：直接取面頰 L\*。

新增 ROI 常數：`LEFT/RIGHT_EYE_CONTOUR`、`LEFT/RIGHT_IRIS`、
`LEFT/RIGHT_LOWER_LID`。三項特徵在無法取得時輸出 `None` (由資料層轉哨兵)。

## 第二批 — 資料層 + 融合引擎 (`core_data.py` / `main.py` / `ui_pages.py`)

**簽章鏈擴充**：三個新特徵皆為「測驗前即算好的 CV 值」, 比照 ΔL\* 納入
HMAC 預簽 (`NONCE_FIELDS` 增 `sclera_a/lid_puff/cheek_L`)、`_PAYLOAD_SCHEMA`、
URL 參數 (`sa/lp/cl`) 與存檔。
- **哨兵約定**：特徵缺失時以 `SENTINEL_MISSING = -999.0` 貫穿簽章鏈
  (始終是真實 float, 避免 None 字串化導致 HMAC 不一致), 寫入 DB 時轉回 NULL。

**融合引擎** (`core_data.py` 新增):
- `FATIGUE_INDEX_SPEC`：四特徵的方向與文獻先驗權重
  (浮腫 0.35 / ΔL\* 0.25 / 鞏膜 0.20 / 蒼白 0.20)。
- `compute_fatigue_face_index()`：各特徵轉個人基線 z-score → 加權合成;
  缺特徵自動略過並重新正規化權重 (回傳 coverage)。
- `calibrate_weights_against_rt()`：資料足量 (≥8 筆且特徵齊備) 時, 用使用者
  自身 Mean_RT (ρ≈0.89 的疲勞金標準) 做最小平方迴歸反推權重; 含防退化保護
  (單一特徵權重 ≥0.95 視為小樣本共線性, 退回先驗)。

**儀表板** (`ui_pages.py`)：新增「複合疲勞臉指數」面板 (指數 + 各特徵 z/權重/貢獻表
+ 權重來源標示), 三個新特徵也加入可繪圖指標。

## 必做：Supabase 資料表遷移

```sql
ALTER TABLE health_logs
  ADD COLUMN IF NOT EXISTS "Sclera_A"      DOUBLE PRECISION,
  ADD COLUMN IF NOT EXISTS "Lid_Puffiness" DOUBLE PRECISION,
  ADD COLUMN IF NOT EXISTS "Cheek_Pallor"  DOUBLE PRECISION;
```
(v2.5 的 `Delta_L` 若尚未加, 一併補上：`ADD COLUMN IF NOT EXISTS "Delta_L" DOUBLE PRECISION;`)

舊紀錄這些欄位為 NULL, 儀表板會提示「新版打卡後開始累積」。

## 驗證 (皆已實機跑過)
- HMAC round-trip：含 JS `String(64.0)→"64"` 整數浮點陷阱, 108 組合
  經真實 `validate_url_payload` + `verify_nonce` 全數通過。
- 融合引擎：睡飽日指數 ≈0.11、疲態日 ≈25, 方向正確;
  缺特徵時 coverage 正確下降並重正規化; 退化權重正確 fallback 先驗。
- 四支檔 `py_compile` 通過。

## 注意事項與後續
- 浮腫為 2D 陰影代理, 非真實 3D 突起; 受拍攝角度影響, 建議固定時段拍攝。
- 先驗權重來自文獻效應量, 屬「冷啟動」值; 累積足量資料後 RT 校準會自動接手。
- 仍建議：把白平衡改為臉部 ROI 估增益 (目前全幅灰世界), 可進一步降低
  背景/衣物造成的特徵漂移。此項風險較高, 尚未動。
