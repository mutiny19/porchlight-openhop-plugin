"""A tiny client for an openHop companion's frame port (the MeshCore companion protocol over TCP).

Porchlight uses it only to put #porchlight in its own companion's channel slot and to check that slot before every
post: openHop's REST API lists channel names but never their keys, and the key is what decides where a message goes.
Frames: app -> companion '<' + u16 LE length + payload; companion -> app '>' + u16 LE length + payload.
The port takes one client at a time, so every call connects, does its job and disconnects.
"""

from __future__ import annotations

import socket
import struct

CMD_APP_START, CMD_SEND_CHANNEL_TXT, CMD_GET_CHANNEL, CMD_SET_CHANNEL = 1, 3, 31, 32
RESP_OK, RESP_ERR, RESP_SELF_INFO, RESP_SENT, RESP_CHANNEL_INFO = 0, 1, 5, 6, 18
NAME_SIZE = 32


class FrameError(RuntimeError):
    pass


class FrameClient:
    def __init__(self, port: int, host: str = "127.0.0.1", timeout: float = 5.0):
        self.addr, self.timeout = (host, int(port)), timeout
        self.sock = None

    def __enter__(self):
        self.sock = socket.create_connection(self.addr, timeout=self.timeout)
        self._send(bytes([CMD_APP_START]) + bytes(7) + b"porchlight")
        self._expect(RESP_SELF_INFO)
        return self

    def __exit__(self, *exc):
        try:
            self.sock.close()
        finally:
            self.sock = None

    def _send(self, payload: bytes) -> None:
        self.sock.sendall(b"<" + struct.pack("<H", len(payload)) + payload)

    def _read_exact(self, n: int) -> bytes:
        buf = b""
        while len(buf) < n:
            chunk = self.sock.recv(n - len(buf))
            if not chunk:
                raise FrameError("companion port closed the connection")
            buf += chunk
        return buf

    def _frame(self) -> bytes:
        while True:
            if self._read_exact(1) != b">":
                continue  # resync on stray bytes
            (n,) = struct.unpack("<H", self._read_exact(2))
            return self._read_exact(n)

    def _expect(self, *codes: int) -> bytes:
        """The next reply with one of codes; push notifications (0x80+) and anything else are skipped."""
        for _ in range(64):
            f = self._frame()
            if f and f[0] == RESP_ERR and RESP_ERR not in codes:
                raise FrameError(f"companion refused the command (error {f[1] if len(f) > 1 else '?'})")
            if f and f[0] in codes:
                return f
        raise FrameError("no reply from the companion")

    def get_channel(self, idx: int) -> tuple[str, bytes]:
        self._send(bytes([CMD_GET_CHANNEL, idx]))
        f = self._expect(RESP_CHANNEL_INFO)
        if len(f) < 2 + NAME_SIZE + 16 or f[1] != idx:
            raise FrameError("unexpected channel reply")
        name = f[2:2 + NAME_SIZE].split(b"\0", 1)[0].decode("utf-8", "replace")
        return name, f[2 + NAME_SIZE:2 + NAME_SIZE + 16]

    def set_channel(self, idx: int, name: str, secret: bytes) -> None:
        raw = name.encode("utf-8")[:NAME_SIZE - 1].ljust(NAME_SIZE, b"\0")
        self._send(bytes([CMD_SET_CHANNEL, idx]) + raw + secret[:16])
        self._expect(RESP_OK)

    def send_channel_text(self, idx: int, text: str, timestamp: int) -> None:
        """Post text in channel slot idx with a given timestamp. The same text + timestamp gives the identical packet
        (same hash), which is how a resend avoids showing up twice."""
        self._send(bytes([CMD_SEND_CHANNEL_TXT, 0, idx]) + struct.pack("<I", int(timestamp)) + text.encode("utf-8"))
        self._expect(RESP_OK, RESP_SENT)
