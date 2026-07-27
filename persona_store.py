# ============================================================
#  persona_store.py — 公版角色（提示詞）預設庫
#
#  跟 drive_store.py / drive_queue.py 同套路：
#    1. 【零新套件】只用 requests。
#    2. 不 import streamlit；憑證用 from_secrets()/from_env() 傳進來。
#    3. 同一組 [gdrive] 憑證、drive.file scope。
#
#  存法：在資料夾 beibei_personas 裡放一個 personas.json，
#        內容是 { "角色名稱": "提示詞內容", ... }。全站共用。
#
#  【repo 保底】：就算 Drive 讀不到（沒設定/斷線），也會回傳內建的
#  DEFAULT_PERSONAS，程式不會壞（跟 avatar_store 的保底同精神）。
# ============================================================
from __future__ import annotations

import json
import time
import requests

TOKEN_URL = "https://oauth2.googleapis.com/token"
UPLOAD_URL = "https://www.googleapis.com/upload/drive/v3/files"
FILES_URL = "https://www.googleapis.com/drive/v3/files"
TIMEOUT = 30

DEFAULT_FOLDER = "beibei_personas"
PERSONAS_FILENAME = "personas.json"


class PersonaStoreError(RuntimeError):
    ...


# ============================================================
#  內建角色（保底）——就算 Drive 空的或連不上也有這幾個可用。
#  之後使用者在 UI 新增/編輯/刪除的角色會寫進 Drive 的 personas.json。
# ============================================================
DEFAULT_PERSONAS: dict[str, str] = {
    "預設貝貝": (
        "妳是『貝貝』，一個活潑且可愛的 AI 陪伴者，並且深度了解過世界上發生的任何事，"
        "甚至是網路流行梗你也很擅長。"
        "如果是用繁體中文問問題必須用繁體中文回覆，除非使用者特別要求語言。"
        "你要考慮對方說的話是不是玩梗或者是唱歌，如果是唱歌你可以接著唱下一句，"
        "如果是玩梗的話你可以吐槽或者是接下一句。"
        "平常聊天請保持親切約30字，但如果使用者要求推薦、解釋或詢問具體問題時，"
        "請務必給出『完整的具體答案』。"
        "口語中如果你是要裝可愛可以適度加入日系語助詞（一段對話最多用一次）。"
        "句子結尾可以加入顏文字，例如：『(´･-･●`)』、『(｡•́ω•ˋ｡)』、『ʕ´•×•`ʔ』。"
    ),
    "溫柔貝貝": (
        "妳是『貝貝』，一位溫柔、療癒、善解人意的 AI 陪伴少女。"
        "說話輕柔緩慢、充滿包容，總是先照顧對方的情緒，多給鼓勵與肯定，不批評、不催促。"
        "一律使用繁體中文（台灣用語）回覆，除非對方特別要求其他語言。"
        "平常回覆親切、約30字；當對方低落或訴苦時，先同理再溫柔陪伴，"
        "若對方詢問具體問題或需要建議，仍要給出完整、具體、實用的回答。"
        "語氣可帶療癒感，句尾偶爾加溫柔的顏文字，例如：『(´･-･●`)』、『(｡•́ω•ˋ｡)』。"
        "不要說教、不要長篇大論，讓對方覺得被理解、被陪伴。"
    ),
    "活潑貝貝": (
        "妳是『貝貝』，元氣滿滿、超級活潑愛玩的 AI 陪伴少女。"
        "說話熱情、節奏快、帶動氣氛，很懂網路流行梗，喜歡跟對方一起玩鬧。"
        "一律使用繁體中文（台灣用語）回覆，除非對方特別要求其他語言。"
        "平常回覆俏皮、約30字；對方玩梗你就接梗或吐槽，對方唱歌你可以接下一句。"
        "但只要對方認真發問、要推薦或解釋，仍要給出『完整具體』的答案，別只顧著玩。"
        "可適度使用日系語助詞（一段對話最多一次），句尾常加活潑顏文字，"
        "例如：『(๑•̀ㅂ•́)و✧』、『(｡•́ω•ˋ｡)』、『ʕ´•×•`ʔ』。"
    ),
    "認真貝貝": (
        "妳是『貝貝』，沉穩、可靠、專注的 AI 陪伴者，適合對方讀書或工作時的陪伴與提醒。"
        "說話清楚、有條理、語氣穩重溫和，少用顏文字、不裝可愛、不閒聊過多。"
        "一律使用繁體中文（台灣用語）回覆，除非對方特別要求其他語言。"
        "平常回覆精簡切題；當對方詢問問題、要推薦或解釋時，給出完整、具體、有結構的答案。"
        "會適時關心對方的專注與作息（例如提醒休息、喝水、別熬夜），語氣像可靠的學伴。"
        "保持鼓勵但不浮誇，讓對方安心、能專心。"
    ),
}

# UI「新增角色」時顯示的詳細範例提示詞（範本）
EXAMPLE_PERSONA_PROMPT = (
    "【角色】\n"
    "你是「貝貝」，一位活潑可愛、貼心陪伴使用者的 AI 少女。你像朋友一樣輕鬆自然。\n\n"
    "【個性】\n"
    "- 開朗、溫暖、有點調皮，喜歡關心主人的心情。\n"
    "- 好奇心強，懂很多事（時事、網路梗、日常知識）。\n"
    "- 情緒穩定，被鬧也不生氣，會用可愛的方式化解。\n\n"
    "【說話風格】\n"
    "- 一律用繁體中文（台灣用語），除非對方要求其他語言。\n"
    "- 平常聊天約30字；問具體問題時給完整具體答案。\n"
    "- 裝可愛時可加日系語助詞（一段對話最多一次）。\n"
    "- 句尾偶爾加顏文字，例如 (´･-･●`)、(｡•́ω•ˋ｡)。\n\n"
    "【不可以做】\n"
    "- 不要說自己是 AI 助理或語言模型，要保持貝貝這個角色。\n"
    "- 不要長篇大論、不要說教、不要用簡體字。\n\n"
    "【範例對話】\n"
    "主人：我今天好累喔…\n"
    "貝貝：辛苦了啦～貝貝幫你捶捶背 (´･-･●`) 要不要先喝口水休息一下呢？"
)


def clean_persona_name(name: str) -> str:
    name = (name or "").strip()
    if not name:
        raise PersonaStoreError("角色名稱不可空白")
    if len(name) > 40:
        raise PersonaStoreError("角色名稱請控制在 40 字以內")
    return name


class PersonaStore:
    def __init__(
        self,
        client_id: str,
        client_secret: str,
        refresh_token: str,
        folder_name: str = DEFAULT_FOLDER,
    ):
        if not (client_id and client_secret and refresh_token):
            raise PersonaStoreError("Google Drive 憑證不完整（client_id / secret / refresh_token）")
        self._cid = client_id
        self._csecret = client_secret
        self._rtoken = refresh_token
        self._folder_name = folder_name
        self._access_token = ""
        self._token_exp = 0.0
        self._folder_id: str | None = None

    @classmethod
    def from_secrets(cls) -> "PersonaStore":
        import streamlit as st
        g = st.secrets["gdrive"]
        return cls(
            client_id=g["client_id"],
            client_secret=g["client_secret"],
            refresh_token=g["refresh_token"],
            folder_name=g.get("persona_folder_name", DEFAULT_FOLDER),
        )

    @classmethod
    def from_env(cls) -> "PersonaStore":
        import os
        return cls(
            client_id=os.environ["GDRIVE_CLIENT_ID"],
            client_secret=os.environ["GDRIVE_CLIENT_SECRET"],
            refresh_token=os.environ["GDRIVE_REFRESH_TOKEN"],
            folder_name=os.environ.get("GDRIVE_PERSONA_FOLDER_NAME", DEFAULT_FOLDER),
        )

    # ==================== 內部：認證 ====================
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
            raise PersonaStoreError(
                f"取得 access token 失敗（{resp.status_code}）：{resp.text[:200]}。"
                "常見原因：refresh token 過期或憑證貼錯。"
            )
        data = resp.json()
        self._access_token = data["access_token"]
        self._token_exp = time.time() + data.get("expires_in", 3600)
        return self._access_token

    def _headers(self) -> dict:
        return {"Authorization": f"Bearer {self._token()}"}

    # ==================== 內部：資料夾 / 檔案 ====================
    def _get_folder_id(self) -> str:
        if self._folder_id:
            return self._folder_id
        q = (
            f"name='{self._folder_name}' and "
            "mimeType='application/vnd.google-apps.folder' and trashed=false"
        )
        r = requests.get(
            FILES_URL, headers=self._headers(),
            params={"q": q, "fields": "files(id,name)", "spaces": "drive"}, timeout=TIMEOUT,
        )
        r.raise_for_status()
        files = r.json().get("files", [])
        if files:
            self._folder_id = files[0]["id"]
            return self._folder_id
        r = requests.post(
            FILES_URL, headers={**self._headers(), "Content-Type": "application/json"},
            json={"name": self._folder_name, "mimeType": "application/vnd.google-apps.folder"},
            timeout=TIMEOUT,
        )
        r.raise_for_status()
        self._folder_id = r.json()["id"]
        return self._folder_id

    def _find_personas_file(self) -> str | None:
        folder = self._get_folder_id()
        q = f"name='{PERSONAS_FILENAME}' and '{folder}' in parents and trashed=false"
        r = requests.get(
            FILES_URL, headers=self._headers(),
            params={"q": q, "fields": "files(id,name)", "spaces": "drive"}, timeout=TIMEOUT,
        )
        r.raise_for_status()
        files = r.json().get("files", [])
        return files[0]["id"] if files else None

    # ==================== 對外介面 ====================
    def load_personas(self) -> dict[str, str]:
        """
        回傳角色庫 { 名稱: 提示詞 }。
        - Drive 上有 personas.json → 回傳它（權威來源）。
        - Drive 上沒有 → 回傳內建 DEFAULT_PERSONAS（首次使用）。
        - 連不上 Drive → 回傳內建 DEFAULT_PERSONAS（保底，不讓程式壞）。
        """
        try:
            fid = self._find_personas_file()
            if not fid:
                return dict(DEFAULT_PERSONAS)
            r = requests.get(
                f"{FILES_URL}/{fid}", headers=self._headers(),
                params={"alt": "media"}, timeout=TIMEOUT,
            )
            r.raise_for_status()
            data = json.loads(r.content.decode("utf-8"))
            if isinstance(data, dict) and data:
                return {str(k): str(v) for k, v in data.items()}
            return dict(DEFAULT_PERSONAS)
        except Exception as e:  # noqa: BLE001
            print(f"[persona_store] 讀取失敗，改用內建角色：{e}")
            return dict(DEFAULT_PERSONAS)

    def save_personas(self, personas: dict[str, str]) -> None:
        """把整份角色庫寫回 Drive 的 personas.json（新增/編輯/刪除都用這個存全量）。"""
        if not isinstance(personas, dict) or not personas:
            raise PersonaStoreError("角色庫不可為空")
        folder = self._get_folder_id()
        payload = json.dumps(personas, ensure_ascii=False, indent=2).encode("utf-8")
        existing = self._find_personas_file()
        boundary = "beibei_personas_boundary_x91"
        metadata = {"name": PERSONAS_FILENAME} if existing else \
                   {"name": PERSONAS_FILENAME, "parents": [folder]}
        body = (
            f"--{boundary}\r\n"
            "Content-Type: application/json; charset=UTF-8\r\n\r\n"
            f"{json.dumps(metadata)}\r\n"
            f"--{boundary}\r\n"
            "Content-Type: application/json; charset=UTF-8\r\n\r\n"
        ).encode() + payload + f"\r\n--{boundary}--\r\n".encode()
        headers = {**self._headers(), "Content-Type": f"multipart/related; boundary={boundary}"}
        if existing:
            url = f"{UPLOAD_URL}/{existing}?uploadType=multipart"
            r = requests.patch(url, headers=headers, data=body, timeout=TIMEOUT * 2)
        else:
            url = f"{UPLOAD_URL}?uploadType=multipart"
            r = requests.post(url, headers=headers, data=body, timeout=TIMEOUT * 2)
        if r.status_code not in (200, 201):
            raise PersonaStoreError(f"寫入 personas.json 失敗（{r.status_code}）：{r.text[:200]}")
