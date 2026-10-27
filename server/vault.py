"""Encrypted per-user Chrome profile vault.

A profile directory is packed as tar.gz and sealed with AES-256-GCM using a per-user key
derived from the master key (HKDF-SHA256, info = b"profile:" + user_id). The blob layout is
MAGIC(4) || version(1) || key_check(16) || nonce(12) || ciphertext+tag. The AAD is the
whole header plus f"{user_id}/{profile}", so a flipped header byte fails authentication.
key_check is an HMAC of the per-user key, so a wrong master key is reported distinctly.
Blobs written before versioning (nonce(12) || ciphertext+tag, AAD f"{user_id}/{profile}")
still restore. Each profile carries an owner canary file that save and restore verify.
Plaintext profile content is never logged.
"""
from __future__ import annotations

import base64
import binascii
import hmac
import io
import json
import os
import posixpath
import shutil
import tarfile
from pathlib import Path
from urllib.parse import unquote, urlparse
from urllib.request import url2pathname

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

NONCE_LEN = 12
MAGIC = b"EAPV"
FORMAT_VERSION = 1
KEY_CHECK_LEN = 16
CANARY_NAME = ".ea-profile.json"
EXCLUDED_NAMES = {"Cache", "Code Cache", "GPUCache", "DawnCache", "Crashpad"}
EXCLUDED_SEQUENCES = [("Service Worker", "CacheStorage")]


class VaultError(Exception):
    pass


class VaultKeyMismatch(VaultError):
    pass


class ProfileOwnerMismatch(VaultError):
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

    def delete(self, key: str) -> int:
        p = self._path(key)
        if p.is_file():
            p.unlink()
            return 1
        return 0

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

    def delete(self, key: str) -> int:
        from azure.core.exceptions import ResourceNotFoundError
        try:
            self._container.delete_blob(key)
            return 1
        except ResourceNotFoundError:
            return 0

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


def _key_check(master_key: bytes, user_id: str) -> bytes:
    """Key-check value from its own HKDF subkey, so it reveals nothing about the AES key."""
    return HKDF(algorithm=hashes.SHA256(), length=KEY_CHECK_LEN, salt=None,
                info=b"profile-check:" + user_id.encode()).derive(master_key)


def _seal(master_key: bytes, user_id: str, profile: str, plaintext: bytes) -> bytes:
    header = MAGIC + bytes([FORMAT_VERSION]) + _key_check(master_key, user_id)
    nonce = os.urandom(NONCE_LEN)
    ct = AESGCM(_user_key(master_key, user_id)).encrypt(nonce, plaintext, header + _aad(user_id, profile))
    return header + nonce + ct


def _open_v1(master_key: bytes, user_id: str, profile: str, blob: bytes) -> bytes:
    hlen = len(MAGIC) + 1 + KEY_CHECK_LEN
    if len(blob) < hlen + NONCE_LEN + 16:
        raise VaultError("profile blob is too short")
    if blob[len(MAGIC)] != FORMAT_VERSION:
        raise VaultError("unsupported vault format")
    header = blob[:hlen]
    if not hmac.compare_digest(header[len(MAGIC) + 1:], _key_check(master_key, user_id)):
        raise VaultKeyMismatch("profile blob was sealed with a different key")
    try:
        return AESGCM(_user_key(master_key, user_id)).decrypt(
            blob[hlen:hlen + NONCE_LEN], blob[hlen + NONCE_LEN:], header + _aad(user_id, profile))
    except InvalidTag as e:
        raise VaultError("profile blob failed authentication") from e


def _open_legacy(master_key: bytes, user_id: str, profile: str, blob: bytes) -> bytes:
    if len(blob) < NONCE_LEN + 16:
        raise VaultError("profile blob is too short")
    try:
        return AESGCM(_user_key(master_key, user_id)).decrypt(
            blob[:NONCE_LEN], blob[NONCE_LEN:], _aad(user_id, profile))
    except InvalidTag as e:
        raise VaultError("profile blob failed authentication") from e


def _open(master_key: bytes, user_id: str, profile: str, blob: bytes) -> tuple[bytes, bool]:
    """Return (plaintext, is_legacy). A legacy blob can begin with the magic by chance, so a
    failed v1 parse falls back to a legacy decrypt; GCM authentication keeps that safe."""
    if not blob.startswith(MAGIC):
        return _open_legacy(master_key, user_id, profile, blob), True
    try:
        return _open_v1(master_key, user_id, profile, blob), False
    except VaultError as v1_error:
        try:
            return _open_legacy(master_key, user_id, profile, blob), True
        except VaultError:
            raise v1_error from None


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
        if ti.issym() or ti.islnk() or _excluded(parts):
            return None
        if parts and (parts[-1].startswith("Singleton") or parts[-1] == "lockfile"):
            return None
        return ti

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
            raise VaultError("archive contains a link")
        if not (m.isfile() or m.isdir() or m.issym() or m.islnk()):
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


# ---------------------------------------------------------------- owner canary

def stamp(profile_dir: Path, user_id: str, profile: str) -> None:
    d = Path(profile_dir)
    d.mkdir(parents=True, exist_ok=True)
    (d / CANARY_NAME).write_text(
        json.dumps({"v": 1, "user_id": user_id, "profile": profile}), encoding="utf-8")


def check_owner(profile_dir: Path, user_id: str, profile: str) -> None:
    try:
        data = json.loads((Path(profile_dir) / CANARY_NAME).read_text(encoding="utf-8"))
        ok = data["user_id"] == user_id and data["profile"] == profile
    except (OSError, ValueError, KeyError, TypeError):
        ok = False
    if not ok:
        raise ProfileOwnerMismatch("profile directory is not stamped for this user and profile")


# ---------------------------------------------------------------- public API

def save_profile(store, master_key: bytes, user_id: str, profile: str, profile_dir: Path) -> str:
    check_owner(profile_dir, user_id, profile)
    key = blob_key(user_id, profile)
    store.put(key, _seal(master_key, user_id, profile, _pack(profile_dir)))
    return key


def restore_profile(store, master_key: bytes, user_id: str, profile: str, dest_dir: Path) -> bool:
    blob = store.get(blob_key(user_id, profile))
    if blob is None:
        return False
    plaintext, legacy = _open(master_key, user_id, profile, blob)
    _unpack(plaintext, dest_dir)
    if legacy and not (Path(dest_dir) / CANARY_NAME).exists():
        stamp(dest_dir, user_id, profile)
    try:
        check_owner(dest_dir, user_id, profile)
    except ProfileOwnerMismatch:
        shutil.rmtree(dest_dir, ignore_errors=True)
        raise
    return True


def delete_profile(store, user_id: str, profile: str) -> int:
    return store.delete(blob_key(user_id, profile))


def delete_user_profiles(store, user_id: str) -> int:
    return store.delete_prefix(f"{user_id}/")
