"""Local proxy relay tests: stdlib fake upstream proxy + fake target, no database."""
import asyncio
import logging
import socket
import threading
import time

import pytest

import browser
from server.proxy_relay import ProxyUnavailable, Relay, proxy_url_for

GOOD_AUTH = "Basic dXNlci14eXo6cGFzcw=="  # user-xyz:pass


class _Loop:
    """An asyncio loop in a daemon thread hosting servers on 127.0.0.1; stop() tears everything down."""

    def __init__(self):
        self.loop = asyncio.new_event_loop()
        self.servers = []
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()

    def _run(self):
        asyncio.set_event_loop(self.loop)
        try:
            self.loop.run_forever()
        finally:
            pending = asyncio.all_tasks(self.loop)
            for t in pending:
                t.cancel()
            if pending:
                self.loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
            self.loop.run_until_complete(self.loop.shutdown_asyncgens())
            self.loop.close()

    def serve(self, handler):
        fut = asyncio.run_coroutine_threadsafe(asyncio.start_server(handler, "127.0.0.1", 0), self.loop)
        server = fut.result(5)
        self.servers.append(server)
        return server, server.sockets[0].getsockname()[1]

    def stop(self):
        async def _close():
            for s in self.servers:
                s.close()
            tasks = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
            for t in tasks:
                t.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
        asyncio.run_coroutine_threadsafe(_close(), self.loop).result(5)
        self.loop.call_soon_threadsafe(self.loop.stop)
        self.thread.join(5)
        assert not self.thread.is_alive()


@pytest.fixture
def loop():
    lp = _Loop()
    yield lp
    lp.stop()


async def _pipe(r, w):
    try:
        while data := await r.read(65536):
            w.write(data)
            await w.drain()
    except OSError:
        pass
    finally:
        w.close()


@pytest.fixture
def target(loop):
    async def handle(r, w):
        await r.readuntil(b"\r\n\r\n")
        body = b"hello from target"
        w.write(b"HTTP/1.1 200 OK\r\nContent-Length: %d\r\nConnection: close\r\n\r\n%s" % (len(body), body))
        await w.drain()
        w.close()
    _, port = loop.serve(handle)
    return port


@pytest.fixture
def fake_upstream(loop):
    """Upstream proxy requiring GOOD_AUTH; records every request head it receives."""
    seen = []

    async def handle(r, w):
        head = (await r.readuntil(b"\r\n\r\n")).decode("latin-1")
        lines = head.split("\r\n")
        seen.append(head)
        auth = next((ln.split(":", 1)[1].strip() for ln in lines if ln.lower().startswith("proxy-authorization:")), None)
        if auth != GOOD_AUTH:
            w.write(b"HTTP/1.1 407 Proxy Authentication Required\r\nContent-Length: 0\r\n\r\n")
            await w.drain()
            w.close()
            return
        method, dest, _ = lines[0].split(" ", 2)
        if method == "CONNECT":
            host, port = dest.rsplit(":", 1)
            tr, tw = await asyncio.open_connection(host, int(port))
            w.write(b"HTTP/1.1 200 Connection established\r\n\r\n")
            await w.drain()
        else:  # absolute-URI plain HTTP
            host, port = dest.split("//", 1)[1].split("/", 1)[0].rsplit(":", 1)
            tr, tw = await asyncio.open_connection(host, int(port))
            tw.write(head.encode("latin-1"))
        await asyncio.gather(_pipe(r, tw), _pipe(tr, w))

    _, port = loop.serve(handle)
    return port, seen


@pytest.fixture
def fake_upstream_rejecting(loop):
    async def handle(r, w):
        await r.readuntil(b"\r\n\r\n")
        w.write(b"HTTP/1.1 407 Proxy Authentication Required\r\nContent-Length: 0\r\n\r\n")
        await w.drain()
        w.close()
    _, port = loop.serve(handle)
    return port


def _url(port):
    return f"http://user-xyz:pass@127.0.0.1:{port}"


def _read_all(s):
    out = b""
    while chunk := s.recv(4096):
        out += chunk
    return out


def _get_via_connect(relay_port, target_port):
    s = socket.create_connection(("127.0.0.1", relay_port), timeout=5)
    s.sendall(f"CONNECT 127.0.0.1:{target_port} HTTP/1.1\r\nHost: 127.0.0.1:{target_port}\r\n\r\n".encode())
    buf = b""
    while b"\r\n\r\n" not in buf:
        buf += s.recv(4096)
    assert buf.startswith(b"HTTP/1.1 200")
    s.sendall(b"GET / HTTP/1.1\r\nHost: t\r\nConnection: close\r\n\r\n")
    out = _read_all(s)
    s.close()
    return out


def test_relay_tunnels_with_upstream_credentials(fake_upstream, target):
    port, seen = fake_upstream
    with Relay(_url(port)) as relay:
        out = _get_via_connect(relay.port, target)
    assert b"hello from target" in out
    assert any(GOOD_AUTH in h and h.startswith(f"CONNECT 127.0.0.1:{target} ") for h in seen)


def test_relay_forwards_plain_http_with_credentials(fake_upstream, target):
    port, seen = fake_upstream
    with Relay(_url(port)) as relay:
        s = socket.create_connection(("127.0.0.1", relay.port), timeout=5)
        s.sendall(f"GET http://127.0.0.1:{target}/ HTTP/1.1\r\nHost: 127.0.0.1:{target}\r\n\r\n".encode())
        out = _read_all(s)
        s.close()
    assert b"hello from target" in out
    assert any(GOOD_AUTH in h and h.startswith("GET http://") for h in seen)


def test_relay_check_ok(fake_upstream, target):
    port, seen = fake_upstream
    with Relay(_url(port)) as relay:
        relay.check(probe_host=f"127.0.0.1:{target}")
    assert any(GOOD_AUTH in h for h in seen)


def test_relay_reports_upstream_auth_failure(fake_upstream_rejecting):
    with Relay(_url(fake_upstream_rejecting)) as relay:
        with pytest.raises(ProxyUnavailable, match="auth"):
            relay.check(probe_host="127.0.0.1:9")


def test_relay_reports_unreachable_upstream():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    closed = s.getsockname()[1]
    s.close()
    with Relay(_url(closed)) as relay:
        t0 = time.monotonic()
        with pytest.raises(ProxyUnavailable, match="unreachable"):
            relay.check()
        assert time.monotonic() - t0 < 10


def test_relay_check_times_out_on_silent_upstream(loop):
    async def handle(r, w):
        await asyncio.sleep(30)
    _, port = loop.serve(handle)
    with Relay(_url(port)) as relay:
        t0 = time.monotonic()
        with pytest.raises(ProxyUnavailable, match="unreachable"):
            relay.check(timeout_s=1)
        assert time.monotonic() - t0 < 5


def test_relay_binds_loopback_only(fake_upstream):
    port, _ = fake_upstream
    with Relay(_url(port)) as relay:
        assert relay.url == f"http://127.0.0.1:{relay.port}"
        socks = relay._server.sockets
        assert len(socks) == 1 and socks[0].getsockname()[0] == "127.0.0.1"


def test_relay_stops_on_exit(fake_upstream):
    port, _ = fake_upstream
    with Relay(_url(port)) as relay:
        p = relay.port
    with pytest.raises(OSError):
        socket.create_connection(("127.0.0.1", p), timeout=2).close()


def test_relay_exit_with_open_tunnel_does_not_block(fake_upstream, target):
    port, _ = fake_upstream
    with Relay(_url(port)) as relay:
        s = socket.create_connection(("127.0.0.1", relay.port), timeout=5)
        s.sendall(f"CONNECT 127.0.0.1:{target} HTTP/1.1\r\nHost: x\r\n\r\n".encode())
        buf = b""
        while b"\r\n\r\n" not in buf:
            buf += s.recv(4096)
        assert buf.startswith(b"HTTP/1.1 200")
        thread = relay._thread
        t0 = time.monotonic()
    try:  # client socket is still open here
        assert time.monotonic() - t0 < 2
        assert not thread.is_alive()
    finally:
        s.close()


def test_relay_does_not_log_credentials(fake_upstream, target, caplog):
    caplog.set_level(logging.DEBUG)
    port, _ = fake_upstream
    with Relay(_url(port)) as relay:
        _get_via_connect(relay.port, target)
        relay.check(probe_host=f"127.0.0.1:{target}")
    text = caplog.text
    assert "pass" not in text and "dXNlci14eXo6cGFzcw" not in text and "user-xyz" not in text


def test_proxy_url_for_formats_session():
    assert proxy_url_for("http://user-{session}:p@h:1", "abc123") == "http://user-abc123:p@h:1"


class _Dead:
    def poll(self):
        return 1

    def wait(self, *a, **k):
        pass

    def kill(self):
        pass


def _open_dead(monkeypatch, tmp_path, **kw):
    seen = {}

    def fake_popen(args, **k):
        seen["args"] = args
        return _Dead()

    monkeypatch.setattr(browser.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(browser, "find_chrome", lambda: "chrome")
    with pytest.raises(RuntimeError):
        with browser.open_site("zepto", profile_dir=tmp_path / "p", **kw):
            pass
    return seen["args"]


def test_open_site_passes_proxy_server(monkeypatch, tmp_path):
    args = _open_dead(monkeypatch, tmp_path, proxy_server="http://127.0.0.1:9999")
    assert "--proxy-server=http://127.0.0.1:9999" in args
    assert "--proxy-bypass-list=<-loopback>" in args


def test_open_site_without_proxy_has_no_proxy_flags(monkeypatch, tmp_path):
    args = _open_dead(monkeypatch, tmp_path)
    assert not any(a.startswith("--proxy") for a in args)
