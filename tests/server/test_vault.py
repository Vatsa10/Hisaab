import base64
import io
import os
import json
import tarfile

import pytest
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from server import vault
from helpers import A, B


def make_profile(root):
    (root / "Default" / "Cache").mkdir(parents=True)
    (root / "Default" / "Cookies").write_bytes(b"cookie-db")
    (root / "Default" / "Cache" / "x").write_bytes(b"cached")
    (root / "Local State").write_bytes(b"{}")
    return root


def save(store, key, user, profile, src):
    vault.stamp(src, user, profile)
    return vault.save_profile(store, key, user, profile, src)


def _legacy_blob(key, user, profile, plaintext):
    nonce = os.urandom(12)
    ct = AESGCM(vault._user_key(key, user)).encrypt(nonce, plaintext, f"{user}/{profile}".encode())
    return nonce + ct


def _setup(tmp_path):
    src = make_profile(tmp_path / "src")
    store = vault.LocalStore(tmp_path / "store")
    return src, store, os.urandom(32)


def test_round_trip_restores_files_and_skips_caches(tmp_path):
    src, store, key = _setup(tmp_path)
    k = save(store, key, A, "zepto", src)
    assert k == f"{A}/zepto.tar.gz.enc"
    assert vault.restore_profile(store, key, A, "zepto", tmp_path / "dst")
    assert (tmp_path / "dst/Default/Cookies").read_bytes() == b"cookie-db"
    assert (tmp_path / "dst/Local State").read_bytes() == b"{}"
    assert not (tmp_path / "dst/Default/Cache").exists()


def test_blob_is_encrypted(tmp_path):
    src, store, key = _setup(tmp_path)
    save(store, key, A, "zepto", src)
    raw = store.get(vault.blob_key(A, "zepto"))
    assert b"cookie-db" not in raw and b"Cookies" not in raw


def test_other_users_key_cannot_decrypt(tmp_path):
    src, store, key = _setup(tmp_path)
    save(store, key, A, "zepto", src)
    store.put(vault.blob_key(B, "zepto"), store.get(vault.blob_key(A, "zepto")))
    with pytest.raises(vault.VaultError):
        vault.restore_profile(store, key, B, "zepto", tmp_path / "dst")


def test_tampered_blob_raises(tmp_path):
    src, store, key = _setup(tmp_path)
    k = save(store, key, A, "zepto", src)
    raw = bytearray(store.get(k))
    raw[len(raw) // 2] ^= 1
    store.put(k, bytes(raw))
    with pytest.raises(vault.VaultError):
        vault.restore_profile(store, key, A, "zepto", tmp_path / "dst")


def test_short_blob_raises(tmp_path):
    store = vault.LocalStore(tmp_path / "store")
    store.put(vault.blob_key(A, "z"), b"abc")
    with pytest.raises(vault.VaultError):
        vault.restore_profile(store, os.urandom(32), A, "z", tmp_path / "dst")


def test_missing_blob_returns_false(tmp_path):
    store = vault.LocalStore(tmp_path / "store")
    assert vault.restore_profile(store, os.urandom(32), A, "zepto", tmp_path / "dst") is False


def test_v1_blob_layout(tmp_path):
    src, store, key = _setup(tmp_path)
    save(store, key, A, "zepto", src)
    raw = store.get(vault.blob_key(A, "zepto"))
    assert raw.startswith(b"EAPV")
    assert vault.restore_profile(store, key, A, "zepto", tmp_path / "dst")
    assert (tmp_path / "dst/Default/Cookies").read_bytes() == b"cookie-db"


def test_legacy_blob_still_restores_and_is_stamped(tmp_path):
    src, store, key = _setup(tmp_path)
    store.put(vault.blob_key(A, "zepto"), _legacy_blob(key, A, "zepto", vault._pack(src)))
    dst = tmp_path / "dst"
    assert vault.restore_profile(store, key, A, "zepto", dst)
    assert (dst / "Default/Cookies").read_bytes() == b"cookie-db"
    vault.check_owner(dst, A, "zepto")  # stamped, does not raise
    assert json.loads((dst / vault.CANARY_NAME).read_text())["user_id"] == A


def test_wrong_master_key_is_key_mismatch(tmp_path):
    src, store, key = _setup(tmp_path)
    save(store, key, A, "zepto", src)
    with pytest.raises(vault.VaultKeyMismatch):
        vault.restore_profile(store, os.urandom(32), A, "zepto", tmp_path / "dst")


@pytest.mark.parametrize("idx", [0, 3, 5, 10, 20])
def test_flipped_header_byte_fails_auth(tmp_path, idx):
    src, store, key = _setup(tmp_path)
    k = save(store, key, A, "zepto", src)
    raw = bytearray(store.get(k))
    raw[idx] ^= 1
    store.put(k, bytes(raw))
    with pytest.raises(vault.VaultError):
        vault.restore_profile(store, key, A, "zepto", tmp_path / "dst")


def test_flipped_nonce_byte_fails_auth(tmp_path):
    src, store, key = _setup(tmp_path)
    k = save(store, key, A, "zepto", src)
    raw = bytearray(store.get(k))
    raw[len(vault.MAGIC) + 1 + 16] ^= 1
    store.put(k, bytes(raw))
    with pytest.raises(vault.VaultError):
        vault.restore_profile(store, key, A, "zepto", tmp_path / "dst")


def test_unknown_version_rejected(tmp_path):
    src, store, key = _setup(tmp_path)
    k = save(store, key, A, "zepto", src)
    raw = bytearray(store.get(k))
    raw[4] = 2
    store.put(k, bytes(raw))
    with pytest.raises(vault.VaultError, match="unsupported vault format"):
        vault.restore_profile(store, key, A, "zepto", tmp_path / "dst")


def test_save_refuses_other_users_profile(tmp_path):
    src, store, key = _setup(tmp_path)
    vault.stamp(src, B, "zepto")
    with pytest.raises(vault.ProfileOwnerMismatch):
        vault.save_profile(store, key, A, "zepto", src)
    assert store.get(vault.blob_key(A, "zepto")) is None
    assert not any(store.root.rglob("*")) if store.root.exists() else True


def test_save_refuses_unstamped_dir(tmp_path):
    src, store, key = _setup(tmp_path)
    with pytest.raises(vault.ProfileOwnerMismatch):
        vault.save_profile(store, key, A, "zepto", src)
    assert store.get(vault.blob_key(A, "zepto")) is None


def test_restore_with_foreign_canary_wipes_dir(tmp_path):
    src, store, key = _setup(tmp_path)
    vault.stamp(src, B, "zepto")
    store.put(vault.blob_key(A, "zepto"), _legacy_blob(key, A, "zepto", vault._pack(src)))
    dst = tmp_path / "dst"
    with pytest.raises(vault.ProfileOwnerMismatch):
        vault.restore_profile(store, key, A, "zepto", dst)
    assert not dst.exists() or not any(dst.iterdir())


def test_delete_profile_removes_only_that_profile(tmp_path):
    src, store, key = _setup(tmp_path)
    save(store, key, A, "swiggy", src)
    save(store, key, A, "zomato", src)
    assert vault.delete_profile(store, A, "swiggy") == 1
    assert store.get(vault.blob_key(A, "swiggy")) is None
    assert store.get(vault.blob_key(A, "zomato")) is not None


def _evil_blob(key, member_setup):
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        member_setup(tf)
    return vault._seal(key, A, "zepto", buf.getvalue())


def _file(tf, name, data=b"pwn"):
    ti = tarfile.TarInfo(name)
    ti.size = len(data)
    tf.addfile(ti, io.BytesIO(data))


def _link(tf, name, target, symlink=True):
    ti = tarfile.TarInfo(name)
    ti.type = tarfile.SYMTYPE if symlink else tarfile.LNKTYPE
    ti.linkname = target
    tf.addfile(ti)


@pytest.mark.parametrize("setup", [
    lambda tf: _file(tf, "../evil"),
    lambda tf: _file(tf, "/abs/evil"),
    lambda tf: _link(tf, "ln", "../../outside"),
    lambda tf: _link(tf, "ln", "/etc/passwd"),
    lambda tf: _link(tf, "ln", "../outside", symlink=False),
])
def test_unsafe_archive_is_refused(tmp_path, setup):
    store = vault.LocalStore(tmp_path / "store")
    key = os.urandom(32)
    store.put(vault.blob_key(A, "zepto"), _evil_blob(key, setup))
    dst = tmp_path / "work" / "dst"
    with pytest.raises(vault.VaultError):
        vault.restore_profile(store, key, A, "zepto", dst)
    assert not (tmp_path / "work" / "evil").exists()
    assert not (tmp_path / "evil").exists()
    assert not dst.exists() or not any(dst.iterdir())


def test_archive_with_parent_path_is_refused(tmp_path):
    store = vault.LocalStore(tmp_path / "store")
    key = os.urandom(32)
    store.put(vault.blob_key(A, "zepto"), _evil_blob(key, lambda tf: _file(tf, "../evil")))
    dst = tmp_path / "work" / "dst"
    with pytest.raises(vault.VaultError):
        vault.restore_profile(store, key, A, "zepto", dst)
    assert not (tmp_path / "work" / "evil").exists()
    assert not dst.exists() or not any(dst.iterdir())
    outside = [p for p in tmp_path.rglob("*") if p.is_file() and store.root not in p.parents]
    assert outside == []


def test_delete_user_profiles_only_touches_that_user(tmp_path):
    src, store, key = _setup(tmp_path)
    save(store, key, A, "zepto", src)
    save(store, key, A, "blinkit", src)
    save(store, key, B, "zepto", src)
    assert vault.delete_user_profiles(store, A) == 2
    assert store.get(vault.blob_key(A, "zepto")) is None
    assert store.get(vault.blob_key(B, "zepto")) is not None


def test_master_key_must_be_32_bytes():
    good = base64.b64encode(os.urandom(32)).decode()
    assert len(vault.master_key_from_env(good)) == 32
    for bad in (base64.b64encode(os.urandom(16)).decode(), "not base64!!"):
        with pytest.raises(ValueError):
            vault.master_key_from_env(bad)


def test_store_from_url(tmp_path):
    assert isinstance(vault.store_from_url(f"file://{tmp_path}"), vault.LocalStore)


@pytest.mark.skipif(not os.environ.get("AZURITE_CONNECTION_STRING"), reason="no Azurite")
def test_blob_store_round_trip(tmp_path):
    src = make_profile(tmp_path / "src")
    store = vault.BlobStore(os.environ["AZURITE_CONNECTION_STRING"], container="profiles-test")
    key = os.urandom(32)
    save(store, key, A, "zepto", src)
    assert vault.restore_profile(store, key, A, "zepto", tmp_path / "dst")
    assert vault.delete_user_profiles(store, A) == 1


def test_pack_skips_singleton_lockfile_and_symlinks(tmp_path):
    src, store, key = _setup(tmp_path)
    (src / "SingletonLock").write_bytes(b"lock")
    (src / "Default" / "lockfile").write_bytes(b"lock")
    try:
        os.symlink("/tmp/.org.chromium.Chromium.x/SingletonSocket", src / "SingletonSocket")
        os.symlink("Local State", src / "rel_link")
    except (OSError, NotImplementedError):
        pass  # no symlink privilege (Windows): regular-file exclusions are still checked
    save(store, key, A, "zepto", src)
    dst = tmp_path / "dst"
    assert vault.restore_profile(store, key, A, "zepto", dst)
    assert (dst / "Default/Cookies").read_bytes() == b"cookie-db"
    for name in ("SingletonLock", "SingletonSocket", "rel_link", "Default/lockfile"):
        assert not os.path.lexists(dst / name)


def test_archive_with_any_symlink_is_refused(tmp_path):
    store = vault.LocalStore(tmp_path / "store")
    key = os.urandom(32)
    store.put(vault.blob_key(A, "zepto"),
              _evil_blob(key, lambda tf: (_file(tf, "ok"), _link(tf, "inside", "ok"))))
    dst = tmp_path / "dst"
    with pytest.raises(vault.VaultError):
        vault.restore_profile(store, key, A, "zepto", dst)
    assert not dst.exists() or not any(dst.iterdir())


def test_chained_symlink_escape_is_refused(tmp_path):
    store = vault.LocalStore(tmp_path / "store")
    key = os.urandom(32)

    def setup(tf):
        _link(tf, "d/l", "..")
        _link(tf, "l2", "d/l/..")
    store.put(vault.blob_key(A, "zepto"), _evil_blob(key, setup))
    with pytest.raises(vault.VaultError):
        vault.restore_profile(store, key, A, "zepto", tmp_path / "dst")
