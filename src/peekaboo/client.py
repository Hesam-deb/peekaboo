"""Talking to the daemon."""
from __future__ import annotations

import json
import socket
import struct
from collections.abc import Iterator

from . import config


class DaemonError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


class Request:
    """Iterate over (kind, payload). Raises DaemonError on FAIL."""

    def __init__(self, op: str, **args):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            self.sock.connect(str(config.SOCKET))
        except OSError:
            self.sock.close()
            raise DaemonError("unavailable", "The Peekaboo service is not running") from None
        owner = struct.unpack("3i", self.sock.getsockopt(
            socket.SOL_SOCKET, socket.SO_PEERCRED, 12))[1]
        if owner != 0 and not config.DEV:
            self.sock.close()
            raise DaemonError("unavailable", "The Peekaboo socket is not owned by root")
        body = {"op": op, **{k: v for k, v in args.items() if v is not None}}
        self.sock.sendall(json.dumps(body).encode() + b"\n")

    def __iter__(self) -> Iterator[tuple[str, str]]:
        with self.sock, self.sock.makefile("r", encoding="utf-8") as stream:
            for line in stream:
                kind, _, payload = line.rstrip("\n").partition(" ")
                if kind == "FAIL":
                    code, _, message = payload.partition(" ")
                    raise DaemonError(code, message)
                yield kind, payload
                if kind == "OK":
                    return
        raise DaemonError("unavailable", "The Peekaboo service closed the connection")

    def cancel(self) -> None:
        try:
            self.sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass


def call(op: str, **args) -> str:
    for kind, payload in Request(op, **args):
        if kind == "OK":
            return payload
    raise DaemonError("unavailable", "No answer")
