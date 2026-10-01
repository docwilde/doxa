# SPDX-License-Identifier: AGPL-3.0-only
"""Kernel-attested Unix transport for the optional browser renderer.

Only this Uvicorn protocol adapter may set the private ASGI attestation flag.
An HTTP header, TCP loopback address, or caller supplied ASGI extension is
never evidence of the Tailscale proxy's identity.
"""

from __future__ import annotations

import ctypes
import os
import socket
import struct
import sys
from pathlib import Path

ATTESTED_KEY = "doxa.verified_unix_proxy"


def proxy_peer_uid(sock: socket.socket) -> int | None:
    if sock.family != socket.AF_UNIX:
        return None
    try:
        if sys.platform == "linux":
            _pid, uid, _gid = struct.unpack(
                "3i", sock.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12)
            )
            return uid
        if sys.platform == "darwin":
            uid = ctypes.c_uint()
            gid = ctypes.c_uint()
            libc = ctypes.CDLL(None, use_errno=True)
            if libc.getpeereid(sock.fileno(), ctypes.byref(uid), ctypes.byref(gid)) != 0:
                return None
            return uid.value
    except (OSError, ValueError, struct.error):
        return None
    return None


def attested_socket(sock: socket.socket | None) -> bool:
    from .peernet import proxy_uid

    expected = proxy_uid()
    return sock is not None and expected >= 0 and proxy_peer_uid(sock) == expected


def protocols():
    """Build protocol classes lazily; a normal DOXA launch needs no Uvicorn."""
    from uvicorn.protocols.http.h11_impl import H11Protocol
    from uvicorn.protocols.websockets.websockets_sansio_impl import WebSocketsSansIOProtocol

    class VerifiedConnection:
        def connection_made(self, transport):
            # Uvicorn stores its ASGI app on each protocol instance. Capture a
            # connection-local verdict before it parses any HTTP or WS scope.
            verified = attested_socket(transport.get_extra_info("socket"))
            app = self.app

            async def verified_app(scope, receive, send):
                scope[ATTESTED_KEY] = verified
                await app(scope, receive, send)

            self.app = verified_app
            super().connection_made(transport)

    class VerifiedHTTP(VerifiedConnection, H11Protocol):
        pass

    class VerifiedWebSocket(VerifiedConnection, WebSocketsSansIOProtocol):
        pass

    return VerifiedHTTP, VerifiedWebSocket


def private_listener(path: Path) -> socket.socket:
    """Bind exclusively below the user's private runtime directory."""
    from .peers import runtime_dir

    runtime = runtime_dir()
    runtime.mkdir(parents=True, exist_ok=True, mode=0o700)
    metadata = runtime.lstat()
    if not runtime.is_dir() or runtime.is_symlink() or metadata.st_uid != os.geteuid() or metadata.st_mode & 0o077:
        raise PermissionError("remote runtime directory must be private and owned")
    if path.parent != runtime or len(os.fsencode(path)) >= 100:
        raise ValueError("browser socket must be directly under the private runtime directory")
    if path.exists() or path.is_symlink():
        raise FileExistsError(f"browser socket entry already exists: {path}")
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        listener.bind(str(path))
        os.chmod(path, 0o600)
        listener.listen(128)
        listener.setblocking(False)
        return listener
    except BaseException:
        listener.close()
        raise
