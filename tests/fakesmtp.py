"""A throwaway SMTP server for the mailer tests.

Python removed `smtpd` in 3.12, so there is nothing in the standard library to
receive a message into. This is the smallest thing that speaks enough SMTP for
smtplib to complete a conversation: EHLO, STARTTLS, AUTH, MAIL, RCPT, DATA,
RSET, NOOP and QUIT.

It exists so the tests can prove the real protocol path works -- headers, dot
stuffing, the 250-after-DATA handshake -- without a single message leaving the
machine. Nothing here talks to a relay.

The optional TLS support needs a certificate, which the standard library cannot
generate, so the tests shell out to `openssl` once and skip the TLS cases when
it is unavailable.
"""

from __future__ import annotations

import base64
import socket
import ssl
import subprocess
import tempfile
import threading
from pathlib import Path


class _LineReader:
    """Buffered line reader over a socket.

    Deliberately not socket.makefile(): once the connection is upgraded with
    wrap_socket, a file object made from the *raw* socket keeps its own buffer
    and the session deadlocks. Owning the buffer means the upgrade is just a
    pointer swap.
    """

    def __init__(self, sock: socket.socket) -> None:
        self.sock = sock
        self.buf = b""

    def reattach(self, sock: socket.socket) -> None:
        self.sock = sock
        self.buf = b""

    def readline(self) -> bytes:
        while b"\r\n" not in self.buf:
            chunk = self.sock.recv(4096)
            if not chunk:
                raise ConnectionError("client hung up")
            self.buf += chunk
        line, self.buf = self.buf.split(b"\r\n", 1)
        return line


class FakeSmtpServer:
    """Listens on 127.0.0.1 and records every message it accepts.

    Attributes let a test steer the outcome:
      reject_next   -- reply 550 to the next DATA, to exercise the failure path
      greet_code    -- override the 220 banner
      fail_auth     -- reject AUTH
      defer_reject  -- number of 4xx replies before accepting a message
    """

    def __init__(self, *, require_auth: bool = False,
                 advertise_starttls: bool = False,
                 implicit_tls: bool = False,
                 certfile: str | None = None,
                 keyfile: str | None = None) -> None:
        self.messages: list[dict] = []
        self.require_auth = require_auth
        self.advertise_starttls = advertise_starttls
        self.implicit_tls = implicit_tls
        self.certfile = certfile
        self.keyfile = keyfile
        self.reject_next = 0
        self.fail_auth = False
        self.defer_reject = 0
        self.greet_code = 220
        self.lock = threading.Lock()

        self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock.bind(("127.0.0.1", 0))
        self._sock.listen(5)
        self.host, self.port = self._sock.getsockname()
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    # ------------------------------------------------------------------ setup

    @staticmethod
    def make_cert(directory: str) -> tuple[str, str] | None:
        """Generate a self-signed cert, or None if openssl is unavailable."""
        cert = Path(directory) / "cert.pem"
        key = Path(directory) / "key.pem"
        try:
            subprocess.run(
                ["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes",
                 "-keyout", str(key), "-out", str(cert), "-days", "1",
                 "-subj", "/CN=127.0.0.1",
                 "-addext", "subjectAltName=IP:127.0.0.1"],
                check=True, capture_output=True, timeout=60,
            )
        except (OSError, subprocess.SubprocessError):
            return None
        return str(cert), str(key)

    # ---------------------------------------------------------------- control

    def close(self) -> None:
        self._stop.set()
        try:
            self._sock.close()
        except OSError:
            pass
        self._thread.join(timeout=5)

    def __enter__(self) -> "FakeSmtpServer":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # -------------------------------------------------------------- internals

    def _serve(self) -> None:
        while not self._stop.is_set():
            try:
                conn, _ = self._sock.accept()
            except OSError:
                return
            if self.implicit_tls:
                # Port 465 style: the TLS handshake happens before any SMTP
                # bytes are exchanged, so wrap here rather than on STARTTLS.
                if not self.certfile:
                    continue
                context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
                context.load_cert_chain(self.certfile, self.keyfile)
                try:
                    conn = context.wrap_socket(conn, server_side=True)
                except ssl.SSLError:
                    continue
            threading.Thread(target=self._session, args=(conn,), daemon=True).start()

    def _session(self, conn: socket.socket) -> None:
        # Hoisted so the finally below can close it even when the client
        # disconnects mid-conversation or leaves via QUIT.
        tls_socket: ssl.SSLSocket | None = None
        try:
            with conn:
                reader = _LineReader(conn)
                conn.sendall(f"{self.greet_code} fake.smtp ESMTP ready\r\n".encode())

                mail_from = ""
                rcpts: list[str] = []
                authenticated = not self.require_auth
                while True:
                    line = reader.readline()
                    parts = line.decode("utf-8", "replace").split()
                    if not parts:
                        continue
                    verb = parts[0].upper()

                    if verb in ("EHLO", "HELO"):
                        extensions = ["250-fake greets you"]
                        if self.advertise_starttls and self.certfile:
                            extensions.append("250-STARTTLS")
                        # Advertised unconditionally: a server that can
                        # authenticate must say so, and require_auth exists
                        # precisely to make the client use it.
                        extensions.append("250-AUTH PLAIN LOGIN")
                        extensions.append("250 SIZE 10485760")
                        conn.sendall(("\r\n".join(extensions) + "\r\n").encode())

                    elif verb == "STARTTLS":
                        if self.implicit_tls:
                            conn.sendall(b"503 already using TLS\r\n")
                            continue
                        if not self.certfile:
                            conn.sendall(b"454 TLS not available\r\n")
                            continue
                        conn.sendall(b"220 ready to start TLS\r\n")
                        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
                        context.load_cert_chain(self.certfile, self.keyfile)
                        try:
                            conn = context.wrap_socket(conn, server_side=True)
                        except ssl.SSLError:
                            return
                        # rebind: the `with conn:` header still closes the raw
                        # socket, and the wrapped one outlives this loop body,
                        # so it gets closed explicitly when the session ends.
                        reader.reattach(conn)
                        tls_socket = conn

                    elif verb == "AUTH":
                        if self.fail_auth:
                            conn.sendall(b"535 authentication failed\r\n")
                            continue
                        blob = ""
                        if len(parts) > 2 and parts[1].upper() == "PLAIN":
                            blob = parts[2]
                        elif len(parts) > 1 and parts[1].upper() == "LOGIN":
                            conn.sendall(b"334 VXNlcm5hbWU6\r\n")
                            reader.readline()
                            conn.sendall(b"334 UGFzc3dvcmQ6\r\n")
                            blob = reader.readline().decode()
                        try:
                            base64.b64decode(blob)
                            authenticated = True
                            conn.sendall(b"235 accepted\r\n")
                        except Exception:
                            conn.sendall(b"535 authentication failed\r\n")

                    elif verb == "MAIL":
                        mail_from = line.decode("utf-8", "replace")
                        conn.sendall(b"250 sender ok\r\n")

                    elif verb == "RCPT":
                        if self.require_auth and not authenticated:
                            conn.sendall(b"530 authentication required\r\n")
                            continue
                        rcpts.append(line.decode("utf-8", "replace"))
                        conn.sendall(b"250 recipient ok\r\n")

                    elif verb == "DATA":
                        if not rcpts:
                            conn.sendall(b"503 need a recipient first\r\n")
                            continue
                        if self.reject_next > 0:
                            self.reject_next -= 1
                            conn.sendall(b"550 no thanks\r\n")
                            mail_from, rcpts = "", []
                            continue
                        if self.defer_reject > 0:
                            self.defer_reject -= 1
                            conn.sendall(b"451 try again later\r\n")
                            mail_from, rcpts = "", []
                            continue
                        conn.sendall(b"354 go ahead\r\n")
                        body = self._read_data(reader)
                        conn.sendall(b"250 queued\r\n")
                        with self.lock:
                            self.messages.append({
                                "mail_from": mail_from,
                                "rcpt": list(rcpts),
                                "data": body,
                            })
                        mail_from, rcpts = "", []

                    elif verb == "RSET":
                        mail_from, rcpts = "", []
                        conn.sendall(b"250 ok\r\n")

                    elif verb == "NOOP":
                        conn.sendall(b"250 ok\r\n")

                    elif verb == "QUIT":
                        conn.sendall(b"221 bye\r\n")
                        return

                    else:
                        conn.sendall(b"500 unrecognised command\r\n")
        except (ConnectionError, OSError, ssl.SSLError):
            return
        finally:
            # wrap_socket hands the descriptor to the SSL socket, so closing
            # the original object does not close this one.
            if tls_socket is not None:
                try:
                    tls_socket.close()
                except OSError:
                    pass

    def _read_data(self, reader) -> str:
        """Read until the lone dot, undoing dot-stuffing as RFC 5321 requires."""
        lines = []
        while True:
            line = reader.readline()
            if line == b".":
                break
            if line.startswith(b".."):
                line = line[1:]
            lines.append(line.decode("utf-8", "replace"))
        return "\n".join(lines)


def run_fake_server(**kwargs) -> FakeSmtpServer:
    return FakeSmtpServer(**kwargs)