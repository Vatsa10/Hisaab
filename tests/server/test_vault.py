import base64
import io
import os
import tarfile

import pytest

from server import vault
from helpers import A, B


def make_profile(root):
    (root / "Default" / "Cache").mkdir(parents=True)
    (root / "Default" / "Cookies").write_bytes(b"cookie-db")
    (root / "Default" / "Cache" / "x").write_bytes(b"cached")
    (root / "Local State").write_bytes(b"{}")
    return root


def _setup(tmp_path):
    src = make_profile(tmp_path / "src")
    store = vault.LocalStore(tmp_path / "store")
    return src, store, os.urandom(32)


def test_round_trip_restores_files_and_skips_caches(tmp_path):
    src, store, key = _setup(tmp_path)
    k = vault.save_profile(store, key, A, "zepto", src)
    assert k == f"{A}/zepto.tar.gz.enc"
    assert vault.restore_profile(store, key, A, "zepto", tmp_path / "dst")
    assert (tmp_path / "dst/Default/Cookies").read_bytes() == b"cookie-db"
    assert (tmp_path / "dst/Local State").read_bytes() == b"{}"
    assert not (tmp_path / "dst/Default/Cache").exists()


def test_blob_is_encrypted(tmp_path):
    src, store, key = _setup(tmp_path)
    vault.save_profile(store, key, A, "zepto", src)
    raw = store.get(vault.blob_key(A, "zepto"))
    assert b"cookie-db" not in raw and b"Cookies" not in raw


def test_other_users_key_cannot_decrypt(tmp_path):
    src, store, key = _setup(tmp_path)
    vault.save_profile(store, key, A, "zepto", src)
    store.put(vault.blob_key(B, "zepto"), store.get(vault.blob_key(A, "zepto")))
    with pytest.raises(vault.VaultError):
        vault.restore_profile(store, key, B, "zepto", tmp_path / "dst")


def test_tampered_blob_raises(tmp_path):
    src, store, key = _setup(tmp_path)
    k = vault.save_profile(store, key, A, "zepto", src)
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
    vault.save_profile(store, key, A, "zepto", src)
    vault.save_profile(store, key, A, "blinkit", src)
    vault.save_profile(store, key, B, "zepto", src)
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
    vault.save_profile(store, key, A, "zepto", src)
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
    vault.save_profile(store, key, A, "zepto", src)
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
