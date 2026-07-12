# 元氣貝貝 v2.0 — 部署指南

## 為何前次部署會失敗？

從您的部署日誌看到：

```
Using Python 3.14.4 environment at /home/adminuser/venv
× Failed to download and build `pillow==10.3.0`
KeyError: '__version__'
```

**根本原因：Streamlit Community Cloud 已部署的 app 不會因為 git push 變更
`runtime.txt` 而切換 Python 版本。** 這是 Streamlit 自 2025 年中持續存在的限制；
僅有「首次部署」時的 Advanced settings 對話框會生效，
之後改 `runtime.txt` 只是「裝飾」用。

Pillow 10.3.0 與 mediapipe 0.10.x 都沒有 Python 3.14 的 pre-built wheel；
Cloud 容器嘗試從原始碼編譯，遇到 setuptools 變更後爆 `KeyError: '__version__'`。

## 修復步驟

### Step 1 — 刪掉原有的 app
1. 進入 [share.streamlit.io](https://share.streamlit.io)
2. 找到你那個 `genki-beibei-copooshi.streamlit.app`
3. ⋯ → **Delete app**
4. 子網域立即可重用，不影響 GitHub

### Step 2 — push 本次 v2.0 程式碼到 GitHub
```bash
git add .
git commit -m "v2.0 — fix py3.14 deadlock + dual-eye CV + PVT-B"
git push
```

### Step 3 — 重新部署，**明確選 Python 3.11**
1. share.streamlit.io → **New app**
2. Repository / Branch / Main file path 填妥
3. **點開「Advanced settings」**
4. **Python version → 選 3.11**（這是關鍵步驟，不可省略）
5. 如有 `SUPABASE_URL` / `SUPABASE_KEY` / `HMAC_SECRET`，貼到 Secrets
6. Deploy

### Step 4 — 驗證日誌
應看到：
```
Using Python 3.11.x environment at /home/adminuser/venv
Collecting streamlit==1.41.1 ... using cached wheel
Collecting mediapipe==0.10.14 ... using cached wheel
...
Successfully installed ...
```

完全沒有 `building wheel`、`compiling`、`KeyError` 等訊號 = 部署成功。

## Secrets 設定（建議）

在 Streamlit Cloud 的 Secrets 區塊填入：

```toml
SUPABASE_URL = "https://xxxxxxxx.supabase.co"
SUPABASE_KEY = "eyJhbGciOiJIUzI1NiI..."

# HMAC nonce 用的隨機字串，至少 32 hex chars
HMAC_SECRET  = "請用 python -c 'import secrets;print(secrets.token_hex(32))' 自己產一組"
```

若沒設 `HMAC_SECRET`，程式會用 session 內的隨機 secret 自動降級，
但這意味著用戶重新整理頁面後舊 URL 就失效（這其實是好事，因為更安全）。

## Supabase Schema 變更

`v2.0` 新增了下列欄位，請執行：

```sql
ALTER TABLE health_logs
  ADD COLUMN IF NOT EXISTS "Delta_E_Left"    DOUBLE PRECISION,
  ADD COLUMN IF NOT EXISTS "Delta_E_Right"   DOUBLE PRECISION,
  ADD COLUMN IF NOT EXISTS "Asymmetry"       DOUBLE PRECISION,
  ADD COLUMN IF NOT EXISTS "RT_Congruent"    DOUBLE PRECISION,
  ADD COLUMN IF NOT EXISTS "RT_Incongruent"  DOUBLE PRECISION,
  ADD COLUMN IF NOT EXISTS "Interference"    DOUBLE PRECISION,
  ADD COLUMN IF NOT EXISTS "Valid_Trials"    INTEGER;
```

## 故障排查

| 症狀 | 原因 | 處理 |
|---|---|---|
| 仍然顯示 `Using Python 3.14.x` | 沒有刪舊 app 重部署 | 回到 Step 1 |
| `ModuleNotFoundError: cv2` | packages.txt 沒被讀到 | 確認 `libgl1` 在 packages.txt |
| Δ E 永遠是 0 | 沒裝 mediapipe wheel | 看 build log 是否成功 |
| 完整性檢查失敗 (HMAC) | secret 變了或 URL 被改 | 重新做一次測驗 |
