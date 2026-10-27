"""Encrypted per-user Chrome profile vault.

A profile directory is packed as tar.gz and sealed with AES-256-GCM using a per-user key
derived from the master key (HKDF-SHA256, info = b"profile:" + user_id). The blob layout is
nonce(12) || ciphertext+tag, and the AAD binds it to f"{user_id}/{profile}". Plaintext
profile content is never logged.
"""
from __future__ import annotations

import base64
import binascii
import io
import os
import posixpath
import tarfile
from pathlib import Path
from urllib.parse import unquote, urlparse
from urllib.request import url2pathname

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

NONCE_LEN = 12
EXCLUDED_NAMES = {"Cache", "Code Cache", "GPUCache", "DawnCache", "Crashpad"}
EXCLUDED_SEQUENCES = [("Service Worker", "CacheStorage")]


class VaultError(Exception):
    pass


# ---------------------------------------------------------------- stores

class LocalStore:
    def __init__(self, root: Path):
        self.root = Path(root)

    def _path(self, key: str) -> Path:
        root = self.root.resolve()
        p = (root / key).resolve()
        if p != root and root not in p.parents:
            raise VaultError("storage key escapes the store root")
        return p

    def put(self, key: str, data: bytes) -> None:
        p = self._path(key)
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_name(p.name + ".tmp")
        tmp.write_bytes(data)
        os.replace(tmp, p)

    def get(self, key: str) -> bytes | None:
        p = self._path(key)
        return p.read_bytes() if p.is_file() else None

    def delete_prefix(self, prefix: str) -> int:
        root = self.root.resolve()
        if not root.is_dir():
            return 0
        n = 0
        for f in sorted(root.rglob("*")):
            if f.is_file() and f.relative_to(root).as_posix().startswith(prefix):
                f.unlink()
                n += 1
        return n


class BlobStore:
    def __init__(self, connection_string: str, container: str = "profiles"):
        from azure.core.exceptions import ResourceExistsError
        from azure.storage.blob import BlobServiceClient
        self._client = BlobServiceClient.from_connection_string(connection_string)
        self._container = self._client.get_container_client(container)
        try:
            self._container.create_container()
        except ResourceExistsError:
            pass

    def put(self, key: str, data: bytes) -> None:
        self._container.upload_blob(key, data, overwrite=True)

    def get(self, key: str) -> bytes | None:
        from azure.core.exceptions import ResourceNotFoundError
        try:
            return self._container.download_blob(key).readall()
        except ResourceNotFoundError:
            return None

    def delete_prefix(self, prefix: str) -> int:
        n = 0
        for b in list(self._container.list_blobs(name_starts_with=prefix)):
            self._container.delete_blob(b.name)
            n += 1
        return n


def store_from_url(url: str):
    if url.startswith("file://"):
        parsed = urlparse(url)
        raw = unquote(parsed.netloc + parsed.path) if parsed.netloc else url2pathname(parsed.path)
        return LocalStore(Path(raw))
    return BlobStore(url)


# ---------------------------------------------------------------- crypto

def master_key_from_env(value: str) -> bytes:
    try:
        key = base64.b64decode(value.strip(), validate=True)
    except (binascii.Error, ValueError) as e:
        raise ValueError("PROFILE_MASTER_KEY is not valid base64") from e
    if len(key) != 32:
        raise ValueError("PROFILE_MASTER_KEY must decode to 32 bytes")
    return key


def blob_key(user_id: str, profile: str) -> str:
    return f"{user_id}/{profile}.tar.gz.enc"


def _user_key(master_key: bytes, user_id: str) -> bytes:
    return HKDF(algorithm=hashes.SHA256(), length=32, salt=None,
                info=b"profile:" + user_id.encode()).derive(master_key)


def _aad(user_id: str, profile: str) -> bytes:
    return f"{user_id}/{profile}".encode()


def _seal(master_key: bytes, user_id: str, profile: str, plaintext: bytes) -> bytes:
    nonce = os.urandom(NONCE_LEN)
    ct = AESGCM(_user_key(master_key, user_id)).encrypt(nonce, plaintext, _aad(user_id, profile))
    return nonce + ct


def _open(master_key: bytes, user_id: str, profile: str, blob: bytes) -> bytes:
    if len(blob) < NONCE_LEN + 16:
        raise VaultError("profile blob is too short")
    try:
        return AESGCM(_user_key(master_key, user_id)).decrypt(
            blob[:NONCE_LEN], blob[NONCE_LEN:], _aad(user_id, profile))
    except InvalidTag as e:
        raise VaultError("profile blob failed authentication") from e


# ---------------------------------------------------------------- archive

def _excluded(rel_parts: tuple[str, ...]) -> bool:
    if any(p in EXCLUDED_NAMES for p in rel_parts):
        return True
    for seq in EXCLUDED_SEQUENCES:
        n = len(seq)
        if any(tuple(rel_parts[i:i + n]) == seq for i in range(len(rel_parts) - n + 1)):
            return True
    return False


def _pack(profile_dir: Path) -> bytes:
    profile_dir = Path(profile_dir)
    buf = io.BytesIO()

    def filt(ti: tarfile.TarInfo):
        parts = tuple(p for p in ti.name.split("/") if p not in ("", "."))
        return None if _excluded(parts) else ti

    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        for child in sorted(profile_dir.iterdir()):
            tf.add(child, arcname=child.name, filter=filt)
    return buf.getvalue()


def _check_members(tf: tarfile.TarFile) -> list[tarfile.TarInfo]:
    members = tf.getmembers()
    for m in members:
        name = m.name
        if name.startswith(("/", "\\")) or posixpath.isabs(name) or (len(name) > 1 and name[1] == ":"):
            raise VaultError("archive member has an absolute path")
        if ".." in name.replace("\\", "/").split("/"):
            raise VaultError("archive member escapes the destination")
        if m.issym() or m.islnk():
            target = m.linkname.replace("\\", "/")
            if target.startswith("/") or (len(target) > 1 and target[1] == ":"):
                raise VaultError("archive link has an absolute target")
            base = posixpath.dirname(name) if m.issym() else ""
            resolved = posixpath.normpath(posixpath.join(base, target))
            if resolved == ".." or resolved.startswith("../"):
                raise VaultError("archive link points outside the destination")
        elif not (m.isfile() or m.isdir()):
            raise VaultError("archive contains a special file")
    return members


def _unpack(data: bytes, dest_dir: Path) -> None:
    dest_dir = Path(dest_dir)
    try:
        with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tf:
            members = _check_members(tf)
            dest_dir.mkdir(parents=True, exist_ok=True)
            tf.extractall(dest_dir, members=members, filter="data")
    except VaultError:
        raise
    except (tarfile.TarError, OSError, EOFError, ValueError) as e:
        raise VaultError(f"profile archive could not be extracted: {type(e).__name__}") from e


# ---------------------------------------------------------------- public API

def save_profile(store, master_key: bytes, user_id: str, profile: str, profile_dir: Path) -> str:
    key = blob_key(user_id, profile)
    store.put(key, _seal(master_key, user_id, profile, _pack(profile_dir)))
    return key


def restore_profile(store, master_key: bytes, user_id: str, profile: str, dest_dir: Path) -> bool:
    blob = store.get(blob_key(user_id, profile))
    if blob is None:
        return False
    _unpack(_open(master_key, user_id, profile, blob), dest_dir)
    return True


def delete_user_profiles(store, user_id: str) -> int:
    return store.delete_prefix(f"{user_id}/")
