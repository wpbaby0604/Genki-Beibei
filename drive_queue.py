# ============================================================
#  drive_queue.py — Google Drive「待烤照片佇列」後端（路線 C 用）
#
#  跟 drive_store.py 是姊妹檔，設計原則完全一樣：
#    1. 【零新套件】只用 requests。
#    2. 不 import streamlit；憑證用 from_secrets() / from_env() 傳進來。
#    3. scope 用 drive.file → 雲端 Streamlit 與本機 baker 必須用【同一組
#       client_id + refresh_token】，否則本機看不到雲端上傳的照片（空清單）。
#
#  差別：drive_store.py 管「成品頭像 .json」，本檔管「待烤原始照片」，
#        放在【另一個資料夾 beibei_queue】，兩者分開、互不干擾。
#
#  一張待烤照片 = 佇列資料夾裡一個檔，檔名 = {job_id}.{副檔名}
#  （job_id 之後就是成品頭像的名字，所以必須符合主程式 ^[\w\-]{1,64}$）
# ============================================================
from __future__ import annotations

import re
import time
import requests

TOKEN_URL = "https://oauth2.googleapis.com/token"
UPLOAD_URL = "https://www.googleapis.com/upload/drive/v3/files"
FILES_URL = "https://www.googleapis.com/drive/v3/files"
TIMEOUT = 30

DEFAULT_QUEUE_FOLDER = "beibei_queue"

# 副檔名 → mimeType
MIME_BY_EXT = {
    "png": "image/png",
    "jpg": "image/jpeg",
    "jpeg": "image/jpeg",
    "webp": "image/webp",
}

_NAME_RE = re.compile(r"[\w\-]{1,64}")   # 跟主程式一致（\w 也涵蓋中文）


class DriveQueueError(RuntimeError):
    ...


def validate_job_id(job_id: str) -> str:
    if not job_id or not _NAME_RE.fullmatch(job_id):
        raise DriveQueueError(
            f"job_id 不合法：{job_id!r}（只能中英數、底線、連字號，1~64 字）"
        )
    return job_id


class DriveQueue:
    def __init__(
        self,
        client_id: str,
        client_secret: str,
        refresh_token: str,
        folder_name: str = DEFAULT_QUEUE_FOLDER,
    ):
        if not (client_id and client_secret and refresh_token):
            raise DriveQueueError("Google Drive 憑證不完整（client_id / secret / refresh_token）")
        self._cid = client_id
        self._csecret = client_secret
        self._rtoken = refresh_token
        self._folder_name = folder_name
        self._access_token = ""
        self._token_exp = 0.0
        self._folder_id: str | None = None

    # ---- 建構：跟 drive_store 用【同一組 [gdrive] 憑證】，只多讀一個 queue 資料夾名 ----
    @classmethod
    def from_secrets(cls) -> "DriveQueue":
        import streamlit as st
        g = st.secrets["gdrive"]
        return cls(
            client_id=g["client_id"],
            client_secret=g["client_secret"],
            refresh_token=g["refresh_token"],
            folder_name=g.get("queue_folder_name", DEFAULT_QUEUE_FOLDER),
        )

    @classmethod
    def from_env(cls) -> "DriveQueue":
        import os
        return cls(
            client_id=os.environ["GDRIVE_CLIENT_ID"],
            client_secret=os.environ["GDRIVE_CLIENT_SECRET"],
            refresh_token=os.environ["GDRIVE_REFRESH_TOKEN"],
            folder_name=os.environ.get("GDRIVE_QUEUE_FOLDER_NAME", DEFAULT_QUEUE_FOLDER),
        )

    # ==================== 內部：認證（與 drive_store 相同）====================
    def _token(self) -> str:
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
            raise DriveQueueError(
                f"取得 access token 失敗（{resp.status_code}）：{resp.text[:200]}。"
                "常見原因：refresh token 過期或憑證貼錯。"
            )
        data = resp.json()
        self._access_token = data["access_token"]
        self._token_exp = time.time() + data.get("expires_in", 3600)
        return self._access_token

    def _headers(self) -> dict:
        return {"Authorization": f"Bearer {self._token()}"}

    # ==================== 內部：資料夾 ====================
    def _get_folder_id(self) -> str:
        if self._folder_id:
            return self._folder_id
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
        r = requests.post(
            FILES_URL,
            headers={**self._headers(), "Content-Type": "application/json"},
            json={"name": self._folder_name, "mimeType": "application/vnd.google-apps.folder"},
            timeout=TIMEOUT,
        )
        r.raise_for_status()
        self._folder_id = r.json()["id"]
        return self._folder_id

    def _list_raw(self) -> list[dict]:
        """列出佇列資料夾內所有圖片檔的原始資訊。"""
        folder = self._get_folder_id()
        out: list[dict] = []
        page_token = None
        while True:
            params = {
                "q": f"'{folder}' in parents and trashed=false and mimeType contains 'image/'",
                "fields": "nextPageToken, files(id,name,modifiedTime,size)",
                "spaces": "drive",
                "pageSize": 200,
            }
            if page_token:
                params["pageToken"] = page_token
            r = requests.get(FILES_URL, headers=self._headers(), params=params, timeout=TIMEOUT)
            r.raise_for_status()
            body = r.json()
            out.extend(body.get("files", []))
            page_token = body.get("nextPageToken")
            if not page_token:
                break
        return out

    def _find(self, job_id: str) -> dict | None:
        """依 job_id 找佇列裡的照片（不管副檔名）。"""
        for f in self._list_raw():
            stem = f["name"].rsplit(".", 1)[0]
            if stem == job_id:
                return f
        return None

    # ==================== 對外介面 ====================
    def upload_photo(self, job_id: str, data: bytes, ext: str = "png") -> None:
        """把一張待烤照片上傳到佇列。job_id 之後就是成品頭像的名字。"""
        validate_job_id(job_id)
        ext = ext.lower().lstrip(".")
        mime = MIME_BY_EXT.get(ext)
        if not mime:
            raise DriveQueueError(f"不支援的圖片副檔名：{ext}（支援 png/jpg/jpeg/webp）")

        folder = self._get_folder_id()
        existing = self._find(job_id)
        boundary = "beibei_queue_boundary_x7f3a"
        filename = f"{job_id}.{ext}"
        import json as _json
        metadata = {"name": filename} if existing else {"name": filename, "parents": [folder]}
        body = (
            f"--{boundary}\r\n"
            "Content-Type: application/json; charset=UTF-8\r\n\r\n"
            f"{_json.dumps(metadata)}\r\n"
            f"--{boundary}\r\n"
            f"Content-Type: {mime}\r\n\r\n"
        ).encode() + data + f"\r\n--{boundary}--\r\n".encode()

        headers = {**self._headers(), "Content-Type": f"multipart/related; boundary={boundary}"}
        if existing:
            url = f"{UPLOAD_URL}/{existing['id']}?uploadType=multipart"
            r = requests.patch(url, headers=headers, data=body, timeout=TIMEOUT * 3)
        else:
            url = f"{UPLOAD_URL}?uploadType=multipart"
            r = requests.post(url, headers=headers, data=body, timeout=TIMEOUT * 3)
        if r.status_code not in (200, 201):
            raise DriveQueueError(f"上傳照片到佇列失敗（{r.status_code}）：{r.text[:200]}")

    def list_pending(self) -> list[dict]:
        """
        回傳待烤清單，每筆：
          {"job_id": str, "filename": str, "ext": str, "modified": str}
        照修改時間由舊到新排序（先進先出）。
        """
        items = []
        for f in self._list_raw():
            name = f["name"]
            stem, _, ext = name.rpartition(".")
            items.append({
                "job_id": stem or name,
                "filename": name,
                "ext": ext.lower(),
                "modified": f.get("modifiedTime", ""),
            })
        items.sort(key=lambda x: x["modified"])
        return items

    def download_photo(self, job_id: str) -> tuple[bytes, str]:
        """下載某張待烤照片，回傳 (bytes, ext)。"""
        f = self._find(job_id)
        if not f:
            raise DriveQueueError(f"佇列裡找不到 job_id={job_id}")
        r = requests.get(
            f"{FILES_URL}/{f['id']}",
            headers=self._headers(),
            params={"alt": "media"},
            timeout=TIMEOUT * 3,
        )
        r.raise_for_status()
        ext = f["name"].rpartition(".")[2].lower()
        return r.content, ext

    def delete_photo(self, job_id: str) -> None:
        """烤完後刪掉原始照片（隱私）。"""
        f = self._find(job_id)
        if not f:
            raise DriveQueueError(f"佇列裡找不到 job_id={job_id}")
        r = requests.delete(f"{FILES_URL}/{f['id']}", headers=self._headers(), timeout=TIMEOUT)
        if r.status_code not in (200, 204):
            raise DriveQueueError(f"刪除佇列照片失敗（{r.status_code}）：{r.text[:200]}")

    def delete_older_than(self, days: int) -> list[str]:
        """清掉在佇列放超過 days 天還沒被烤的照片，回傳被刪的 job_id。"""
        from datetime import datetime, timedelta, timezone
        cutoff = datetime.now(timezone.utc) - timedelta(days=days)
        deleted = []
        for f in self._list_raw():
            mt = f.get("modifiedTime", "")
            try:
                t = datetime.fromisoformat(mt.replace("Z", "+00:00"))
            except ValueError:
                continue
            if t < cutoff:
                requests.delete(f"{FILES_URL}/{f['id']}", headers=self._headers(), timeout=TIMEOUT)
                deleted.append(f["name"].rsplit(".", 1)[0])
        return deleted
