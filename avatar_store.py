# ============================================================
#  avatar_store.py — 頭像儲存抽象層
#
#  設計目標：
#    1. companion_live2d.py 只認識 list_avatars / load_avatar / save_avatar /
#       delete_avatar，完全不知道底下是本機資料夾還是 Google Drive。
#       之後要換 Supabase Storage / S3 / 任何東西，只改本檔，不動任何呼叫端。
#    2. 【不 import streamlit】——因為 Hugging Face Space 端也要 import 這支
#       （以及 drive_store.py）往 Drive 寫檔。快取是 Streamlit 的事，留在頁面層。
#    3. 【repo 內建 3 個頭像永遠保底】：就算 Google Drive 掛掉、憑證過期、
#       網路斷線，list_avatars() 仍然會回傳 crypko_06 / lambda_00 / lambda_01。
#       Drive 的錯誤只會被記錄，不會往上炸。
# ============================================================
from __future__ import annotations

import os
import re
from typing import Protocol, runtime_checkable

BAKED_DIR = "baked_avatars"

# 頭像名稱規則：允許中英數與底線/連字號（\w 在 re.UNICODE 下含中日韓）。
# 明確擋掉 . / \ 與空白，避免路徑穿越與 Drive query 注入。
_NAME_RE = re.compile(r"^[\w\-]{1,64}$", re.UNICODE)


class AvatarStoreError(RuntimeError):
    """儲存層的基底例外。"""


class AvatarNotFound(AvatarStoreError):
    """找不到指定頭像。"""


class AvatarReadOnly(AvatarStoreError):
    """對唯讀後端做寫入。"""


def validate_name(name: str) -> str:
    """檢查頭像名稱合法性；不合法直接 raise，不要讓髒名字流進檔案系統或 API。"""
    if not isinstance(name, str) or not _NAME_RE.match(name):
        raise AvatarStoreError(
            f"頭像名稱不合法：{name!r}（只允許中英數、底線、連字號，1-64 字）"
        )
    return name


# ============================================================
#  介面
# ============================================================
@runtime_checkable
class AvatarStore(Protocol):
    backend_id: str          # "repo" / "drive" / ...
    writable: bool

    def list_avatars(self) -> list[str]: ...
    def load_avatar(self, name: str) -> bytes: ...
    def save_avatar(self, name: str, data: bytes) -> None: ...
    def delete_avatar(self, name: str) -> None: ...
    def exists(self, name: str) -> bool: ...


# ============================================================
#  後端 1：本機資料夾（= repo 內建的 baked_avatars/，唯讀保底）
# ============================================================
class LocalDirStore:
    backend_id = "repo"

    def __init__(self, directory: str = BAKED_DIR, writable: bool = False):
        self.directory = directory
        self.writable = writable

    def _path(self, name: str) -> str:
        return os.path.join(self.directory, f"{validate_name(name)}.json")

    def list_avatars(self) -> list[str]:
        try:
            return sorted(
                os.path.splitext(f)[0]
                for f in os.listdir(self.directory)
                if f.lower().endswith(".json")
            )
        except (FileNotFoundError, OSError):
            return []

    def exists(self, name: str) -> bool:
        try:
            return os.path.isfile(self._path(name))
        except AvatarStoreError:
            return False

    def load_avatar(self, name: str) -> bytes:
        try:
            with open(self._path(name), "rb") as f:
                return f.read()
        except FileNotFoundError as e:
            raise AvatarNotFound(name) from e

    def save_avatar(self, name: str, data: bytes) -> None:
        if not self.writable:
            raise AvatarReadOnly(f"{self.backend_id} 後端是唯讀的（不寫 repo）")
        os.makedirs(self.directory, exist_ok=True)
        tmp = self._path(name) + ".tmp"
        with open(tmp, "wb") as f:
            f.write(data)
        os.replace(tmp, self._path(name))      # 原子性寫入，避免半截檔被讀到

    def delete_avatar(self, name: str) -> None:
        if not self.writable:
            raise AvatarReadOnly(f"{self.backend_id} 後端是唯讀的（不刪 repo）")
        try:
            os.remove(self._path(name))
        except FileNotFoundError as e:
            raise AvatarNotFound(name) from e


# ============================================================
#  組合後端：repo（保底）+ Drive（使用者烤的）
# ============================================================
class CompositeStore:
    """依序查詢多個後端。列表 = 聯集（前者優先、去重）；
    寫入 / 刪除 = 交給第一個 writable 的後端。
    任一後端在 list 時炸掉，只會被跳過並記錄，不影響其他後端。"""

    backend_id = "composite"

    def __init__(self, stores: list[AvatarStore]):
        if not stores:
            raise AvatarStoreError("CompositeStore 至少要有一個後端")
        self.stores = list(stores)
        self.last_errors: dict[str, str] = {}

    @property
    def writable(self) -> bool:
        return any(s.writable for s in self.stores)

    def _writable_store(self) -> AvatarStore:
        for s in self.stores:
            if s.writable:
                return s
        raise AvatarReadOnly("目前沒有任何可寫入的後端（Google Drive 未設定？）")

    def list_avatars(self) -> list[str]:
        seen: list[str] = []
        for s in self.stores:
            try:
                names = s.list_avatars()
                self.last_errors.pop(s.backend_id, None)
            except Exception as e:                      # noqa: BLE001 — 保底：絕不讓後端拖垮列表
                self.last_errors[s.backend_id] = str(e)
                print(f"[avatar_store] 後端 {s.backend_id} 列表失敗（已跳過）：{e}")
                continue
            for n in names:
                if n not in seen:
                    seen.append(n)
        return seen

    def which(self, name: str) -> str | None:
        """這個頭像住在哪個後端。"""
        for s in self.stores:
            try:
                if s.exists(name):
                    return s.backend_id
            except Exception:                           # noqa: BLE001
                continue
        return None

    def exists(self, name: str) -> bool:
        return self.which(name) is not None

    def load_avatar(self, name: str) -> bytes:
        for s in self.stores:
            try:
                if s.exists(name):
                    return s.load_avatar(name)
            except AvatarNotFound:
                continue
        raise AvatarNotFound(name)

    def save_avatar(self, name: str, data: bytes) -> None:
        self._writable_store().save_avatar(name, data)

    def delete_avatar(self, name: str) -> None:
        for s in self.stores:
            if s.exists(name):
                if not s.writable:
                    raise AvatarReadOnly(f"「{name}」是內建頭像，不能刪除")
                s.delete_avatar(name)
                return
        raise AvatarNotFound(name)


# ============================================================
#  模組級門面（呼叫端只用這四個 function）
# ============================================================
_store: AvatarStore | None = None


def _try_build_drive_store() -> AvatarStore | None:
    """第 3 步會補上 drive_store.py。現在它不存在 → 靜靜地只用 repo 後端。
    憑證沒設好也只是停用 Drive，不會讓整頁崩掉。"""
    try:
        from drive_store import GoogleDriveStore
    except ImportError:
        return None
    try:
        return GoogleDriveStore.from_secrets()
    except Exception as e:                              # noqa: BLE001
        print(f"[avatar_store] Google Drive 後端停用：{e}")
        return None


def _build_default_store() -> AvatarStore:
    stores: list[AvatarStore] = [LocalDirStore(BAKED_DIR, writable=False)]
    drive = _try_build_drive_store()
    if drive is not None:
        stores.append(drive)
    return CompositeStore(stores)


def get_store() -> AvatarStore:
    global _store
    if _store is None:
        _store = _build_default_store()
    return _store


def set_store(store: AvatarStore) -> None:
    """換後端 / 測試注入用。"""
    global _store
    _store = store


def list_avatars() -> list[str]:
    return get_store().list_avatars()


def load_avatar(name: str) -> bytes:
    return get_store().load_avatar(name)


def save_avatar(name: str, data: bytes) -> None:
    get_store().save_avatar(name, data)


def delete_avatar(name: str) -> None:
    get_store().delete_avatar(name)


def exists(name: str) -> bool:
    return get_store().exists(name)
