# ============================================================
#  drive_store.py — Google Drive 後端（給 avatar_store 用）
#
#  設計原則：
#    1. 【零新套件】只用 requests（Streamlit 本來就相依它）。
#       不引入 google-api-python-client / google-auth，避開相依衝突與記憶體開銷。
#    2. 【不 import streamlit】憑證用 from_secrets() 傳進來即可，
#       這樣 Hugging Face Space 端也能 import 同一支往 Drive 寫檔。
#    3. scope 用 drive.file：只看得到「本 App 建立的檔案」。
#       ⚠️ 所以 Streamlit 和 HF Space 必須用【同一組 client_id + refresh_token】，
#          否則 Streamlit 看不到 HF Space 建的檔（會是空清單，不是報錯，最難查）。
#    4. access token 會過期（約 1 小時），本檔自動用 refresh token 續，呼叫端無感。
# ============================================================
from __future__ import annotations

import io
import time
import requests

# 這兩個是 avatar_store 定義的例外；若單獨測試 drive_store 也能 fallback。
try:
    from avatar_store import AvatarNotFound, AvatarStoreError
except ImportError:  # pragma: no cover
    class AvatarStoreError(RuntimeError): ...
    class AvatarNotFound(AvatarStoreError): ...


TOKEN_URL = "https://oauth2.googleapis.com/token"
UPLOAD_URL = "https://www.googleapis.com/upload/drive/v3/files"
FILES_URL = "https://www.googleapis.com/drive/v3/files"
TIMEOUT = 30


class GoogleDriveStore:
    backend_id = "drive"
    writable = True

    def __init__(
        self,
        client_id: str,
        client_secret: str,
        refresh_token: str,
        folder_name: str = "beibei_avatars",
    ):
        if not (client_id and client_secret and refresh_token):
            raise AvatarStoreError("Google Drive 憑證不完整（client_id / secret / refresh_token）")
        self._cid = client_id
        self._csecret = client_secret
        self._rtoken = refresh_token
        self._folder_name = folder_name
        self._access_token = ""
        self._token_exp = 0.0
        self._folder_id: str | None = None

    # ---- 從 Streamlit secrets 建構（給雲端 Streamlit 用）----
    @classmethod
    def from_secrets(cls) -> "GoogleDriveStore":
        import streamlit as st  # 只有這個 classmethod 用到 streamlit
        g = st.secrets["gdrive"]
        return cls(
            client_id=g["client_id"],
            client_secret=g["client_secret"],
            refresh_token=g["refresh_token"],
            folder_name=g.get("folder_name", "beibei_avatars"),
        )

    # ---- 從環境變數建構（給 HF Space 用，那邊用 env 而非 st.secrets）----
    @classmethod
    def from_env(cls) -> "GoogleDriveStore":
        import os
        return cls(
            client_id=os.environ["GDRIVE_CLIENT_ID"],
            client_secret=os.environ["GDRIVE_CLIENT_SECRET"],
            refresh_token=os.environ["GDRIVE_REFRESH_TOKEN"],
            folder_name=os.environ.get("GDRIVE_FOLDER_NAME", "beibei_avatars"),
        )

    # ==================== 內部：認證 ====================
    def _token(self) -> str:
        """回傳有效的 access token；過期就自動用 refresh token 換新的。"""
        if self._access_token and time.time() < self._token_exp - 60:
            return self._access_token
        resp = requests.post(
            TOKEN_URL,
            data={
                "client_id": self._cid,
                "client_secret": self._csecret,
                "refresh_token": self._rtoken,
                "grant_type": "refresh_token",
            },
            timeout=TIMEOUT,
        )
        if resp.status_code != 200:
            raise AvatarStoreError(
                f"取得 access token 失敗（{resp.status_code}）：{resp.text[:200]}。"
                "常見原因：refresh token 過期（OAuth 同意畫面沒發布成正式版？）"
            )
        data = resp.json()
        self._access_token = data["access_token"]
        self._token_exp = time.time() + data.get("expires_in", 3600)
        return self._access_token

    def _headers(self) -> dict:
        return {"Authorization": f"Bearer {self._token()}"}

    # ==================== 內部：資料夾 ====================
    def _get_folder_id(self) -> str:
        """找到（或建立）存頭像的資料夾，回傳它的 id。結果快取在記憶體。"""
        if self._folder_id:
            return self._folder_id
        # 找現有的
        q = (
            f"name='{self._folder_name}' and "
            "mimeType='application/vnd.google-apps.folder' and trashed=false"
        )
        r = requests.get(
            FILES_URL,
            headers=self._headers(),
            params={"q": q, "fields": "files(id,name)", "spaces": "drive"},
            timeout=TIMEOUT,
        )
        r.raise_for_status()
        files = r.json().get("files", [])
        if files:
            self._folder_id = files[0]["id"]
            return self._folder_id
        # 沒有就建一個
        r = requests.post(
            FILES_URL,
            headers={**self._headers(), "Content-Type": "application/json"},
            json={
                "name": self._folder_name,
                "mimeType": "application/vnd.google-apps.folder",
            },
            timeout=TIMEOUT,
        )
        r.raise_for_status()
        self._folder_id = r.json()["id"]
        return self._folder_id

    def _find_file(self, name: str) -> dict | None:
        """依頭像名字找檔案（存成 {name}.json）。回傳 {id, name, ...} 或 None。"""
        folder = self._get_folder_id()
        q = f"name='{name}.json' and '{folder}' in parents and trashed=false"
        r = requests.get(
            FILES_URL,
            headers=self._headers(),
            params={
                "q": q,
                "fields": "files(id,name,modifiedTime,size)",
                "spaces": "drive",
            },
            timeout=TIMEOUT,
        )
        r.raise_for_status()
        files = r.json().get("files", [])
        return files[0] if files else None

    # ==================== 對外介面 ====================
    def list_avatars(self) -> list[str]:
        folder = self._get_folder_id()
        names: list[str] = []
        page_token = None
        while True:
            params = {
                "q": f"'{folder}' in parents and trashed=false and mimeType='application/json'",
                "fields": "nextPageToken, files(name)",
                "spaces": "drive",
                "pageSize": 200,
            }
            if page_token:
                params["pageToken"] = page_token
            r = requests.get(FILES_URL, headers=self._headers(), params=params, timeout=TIMEOUT)
            r.raise_for_status()
            body = r.json()
            for f in body.get("files", []):
                nm = f["name"]
                if nm.endswith(".json"):
                    names.append(nm[:-5])
            page_token = body.get("nextPageToken")
            if not page_token:
                break
        return sorted(names)

    def exists(self, name: str) -> bool:
        return self._find_file(name) is not None

    def load_avatar(self, name: str) -> bytes:
        f = self._find_file(name)
        if not f:
            raise AvatarNotFound(name)
        r = requests.get(
            f"{FILES_URL}/{f['id']}",
            headers=self._headers(),
            params={"alt": "media"},
            timeout=TIMEOUT * 2,  # 檔案可能 10MB+，給多一點時間
        )
        r.raise_for_status()
        return r.content

    def save_avatar(self, name: str, data: bytes) -> None:
        """新增或覆蓋。用 multipart 一次把 metadata + 內容送上去。"""
        folder = self._get_folder_id()
        existing = self._find_file(name)
        boundary = "beibei_boundary_x7f3a"
        metadata = (
            {"name": f"{name}.json"}
            if existing
            else {"name": f"{name}.json", "parents": [folder]}
        )
        import json as _json
        body = (
            f"--{boundary}\r\n"
            "Content-Type: application/json; charset=UTF-8\r\n\r\n"
            f"{_json.dumps(metadata)}\r\n"
            f"--{boundary}\r\n"
            "Content-Type: application/json\r\n\r\n"
        ).encode() + data + f"\r\n--{boundary}--\r\n".encode()

        headers = {
            **self._headers(),
            "Content-Type": f"multipart/related; boundary={boundary}",
        }
        if existing:
            url = f"{UPLOAD_URL}/{existing['id']}?uploadType=multipart"
            r = requests.patch(url, headers=headers, data=body, timeout=TIMEOUT * 3)
        else:
            url = f"{UPLOAD_URL}?uploadType=multipart"
            r = requests.post(url, headers=headers, data=body, timeout=TIMEOUT * 3)
        if r.status_code not in (200, 201):
            raise AvatarStoreError(f"寫入 Drive 失敗（{r.status_code}）：{r.text[:200]}")

    def delete_avatar(self, name: str) -> None:
        f = self._find_file(name)
        if not f:
            raise AvatarNotFound(name)
        r = requests.delete(f"{FILES_URL}/{f['id']}", headers=self._headers(), timeout=TIMEOUT)
        if r.status_code not in (200, 204):
            raise AvatarStoreError(f"刪除 Drive 檔案失敗（{r.status_code}）：{r.text[:200]}")

    # ==================== 保留期限清理（第 7 步會用到）====================
    def delete_older_than(self, days: int) -> list[str]:
        """刪掉超過 days 天沒修改的頭像，回傳被刪的名字清單。"""
        from datetime import datetime, timedelta, timezone
        folder = self._get_folder_id()
        cutoff = datetime.now(timezone.utc) - timedelta(days=days)
        cutoff_str = cutoff.strftime("%Y-%m-%dT%H:%M:%S")
        r = requests.get(
            FILES_URL,
            headers=self._headers(),
            params={
                "q": (
                    f"'{folder}' in parents and trashed=false "
                    f"and mimeType='application/json' "
                    f"and modifiedTime < '{cutoff_str}'"
                ),
                "fields": "files(id,name)",
                "spaces": "drive",
            },
            timeout=TIMEOUT,
        )
        r.raise_for_status()
        deleted = []
        for f in r.json().get("files", []):
            requests.delete(f"{FILES_URL}/{f['id']}", headers=self._headers(), timeout=TIMEOUT)
            deleted.append(f["name"].removesuffix(".json"))
        return deleted
