"""Local relay that adds upstream proxy credentials for Chrome.

Chrome cannot take proxy credentials on its command line, so Chrome points at this relay
(127.0.0.1 only) and the relay adds `Proxy-Authorization: Basic ...` toward the one configured
upstream. Credentials are never logged; debug lines carry only the target host:port.
"""
import asyncio
import base64
import logging
import socket
import threading
from urllib.parse import unquote, urlsplit

log = logging.getLogger(__name__)

_MAX_HEAD = 64 * 1024
_CONNECT_TIMEOUT_S = 15


class ProxyUnavailable(Exception):
    """Upstream proxy rejected our credentials ("auth") or cannot be reached ("unreachable")."""


def proxy_url_for(template: str, session_id: str) -> str:
    return template.format(session=session_id)


def _parse_upstream(url: str) -> tuple[str, int, str]:
    parts = urlsplit(url)
    if not parts.hostname or not parts.port:
        raise ValueError("upstream proxy URL needs host and port")
    cred = f"{unquote(parts.username or '')}:{unquote(parts.password or '')}"
    token = base64.b64encode(cred.encode("utf-8")).decode("ascii")
    return parts.hostname, parts.port, f"Basic {token}"


def _status(head: bytes) -> int:
    try:
        return int(head.split(b" ", 2)[1])
    except (IndexError, ValueError):
        return 0


async def _pipe(reader, writer):
    try:
        while data := await reader.read(65536):
            writer.write(data)
            await writer.drain()
    except (OSError, asyncio.IncompleteReadError):
        pass
    finally:
        try:
            writer.close()
        except Exception:
            pass


class Relay:
    def __init__(self, upstream_url: str):
        self._host, self._port, self._auth = _parse_upstream(upstream_url)
        self.port: int = 0
        self.url: str = ""
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._server = None

    # lifecycle ---------------------------------------------------------
    def __enter__(self):
        self._loop = asyncio.new_event_loop()
        ready = threading.Event()
        err: list[BaseException] = []

        def run():
            asyncio.set_event_loop(self._loop)
            try:
                self._server = self._loop.run_until_complete(
                    asyncio.start_server(self._handle, "127.0.0.1", 0))
            except BaseException as e:  # noqa: BLE001 - reported to the caller
                err.append(e)
                ready.set()
                return
            ready.set()
            self._loop.run_forever()

        self._thread = threading.Thread(target=run, daemon=True, name="proxy-relay")
        self._thread.start()
        ready.wait(10)
        if err or self._server is None:
            raise err[0] if err else RuntimeError("proxy relay failed to start")
        self.port = self._server.sockets[0].getsockname()[1]
        self.url = f"http://127.0.0.1:{self.port}"
        return self

    def __exit__(self, *exc):
        loop, server = self._loop, self._server
        if loop is not None and server is not None:
            async def _shutdown():
                server.close()
                await server.wait_closed()
                for t in asyncio.all_tasks():
                    if t is not asyncio.current_task():
                        t.cancel()
            try:
                asyncio.run_coroutine_threadsafe(_shutdown(), loop).result(5)
            except Exception:
                pass
            loop.call_soon_threadsafe(loop.stop)
            if self._thread:
                self._thread.join(5)
            try:
                loop.close()
            except Exception:
                pass
        return False

    # health check ------------------------------------------------------
    def check(self, timeout_s: float = 10, probe_host: str = "www.google.com:443") -> None:
        """One CONNECT to probe_host through the upstream. Raises ProxyUnavailable."""
        import time
        deadline = time.monotonic() + timeout_s
        try:
            s = socket.create_connection((self._host, self._port), timeout=timeout_s)
        except OSError:
            raise ProxyUnavailable("unreachable") from None
        try:
            s.sendall(f"CONNECT {probe_host} HTTP/1.1\r\nHost: {probe_host}\r\n"
                      f"Proxy-Authorization: {self._auth}\r\n\r\n".encode("latin-1"))
            buf = b""
            while b"\r\n" not in buf and len(buf) < _MAX_HEAD:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise ProxyUnavailable("unreachable")
                s.settimeout(remaining)
                chunk = s.recv(4096)
                if not chunk:
                    break
                buf += chunk
        except socket.timeout:
            raise ProxyUnavailable("unreachable") from None
        except OSError:
            raise ProxyUnavailable("unreachable") from None
        finally:
            s.close()
        code = _status(buf)
        if code == 407:
            raise ProxyUnavailable("auth")
        if code != 200:
            raise ProxyUnavailable("unreachable")

    # connection handling -----------------------------------------------
    async def _handle(self, reader, writer):
        up_writer = None
        try:
            try:
                head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 30)
            except (asyncio.IncompleteReadError, asyncio.LimitOverrunError,
                    asyncio.TimeoutError, OSError):
                return
            if len(head) > _MAX_HEAD:
                return
            first, _, rest = head.partition(b"\r\n")
            try:
                method, dest, version = first.decode("latin-1").split(" ", 2)
            except ValueError:
                writer.write(b"HTTP/1.1 400 Bad Request\r\nContent-Length: 0\r\n\r\n")
                return
            target = dest if method.upper() == "CONNECT" else (urlsplit(dest).netloc or "?")
            log.debug("relay %s %s", method.upper(), target)
            try:
                up_reader, up_writer = await asyncio.wait_for(
                    asyncio.open_connection(self._host, self._port), _CONNECT_TIMEOUT_S)
            except (OSError, asyncio.TimeoutError):
                writer.write(b"HTTP/1.1 502 Bad Gateway\r\nContent-Length: 0\r\n\r\n")
                return
            auth = f"Proxy-Authorization: {self._auth}\r\n".encode("latin-1")
            if method.upper() == "CONNECT":
                up_writer.write(f"CONNECT {dest} HTTP/1.1\r\nHost: {dest}\r\n".encode("latin-1")
                                + auth + b"\r\n")
                await up_writer.drain()
                try:
                    resp = await asyncio.wait_for(up_reader.readuntil(b"\r\n\r\n"), _CONNECT_TIMEOUT_S)
                except (asyncio.IncompleteReadError, asyncio.LimitOverrunError,
                        asyncio.TimeoutError, OSError):
                    writer.write(b"HTTP/1.1 502 Bad Gateway\r\nContent-Length: 0\r\n\r\n")
                    return
                code = _status(resp)
                if code != 200:
                    log.debug("relay upstream refused %s: %s", target, code)
                    writer.write(b"HTTP/1.1 %d Upstream Refused\r\nContent-Length: 0\r\n\r\n"
                                 % (code if 400 <= code < 600 else 502))
                    return
                writer.write(b"HTTP/1.1 200 Connection Established\r\n\r\n")
                await writer.drain()
            else:
                # plain HTTP, absolute-URI form: drop any client-sent proxy credentials, add ours,
                # and close after one exchange so every request carries our header.
                keep = [ln for ln in rest.split(b"\r\n")
                        if ln and not ln.lower().startswith((b"proxy-authorization:", b"proxy-connection:",
                                                             b"connection:"))]
                up_writer.write(first + b"\r\n" + b"\r\n".join(keep) + b"\r\n" + auth
                                + b"Connection: close\r\n\r\n")
                await up_writer.drain()
            await asyncio.gather(_pipe(reader, up_writer), _pipe(up_reader, writer))
        except asyncio.CancelledError:
            raise
        except Exception:
            log.debug("relay connection error", exc_info=False)
        finally:
            for w in (up_writer, writer):
                if w is not None:
                    try:
                        w.close()
                    except Exception:
                        pass
