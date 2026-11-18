"""Transportes para o ELM327: RFCOMM (Bluetooth clássico) e TCP (emulador / adaptadores WiFi)."""
from __future__ import annotations

import socket
import time


class TransportError(Exception):
    pass


class Transport:
    sock: socket.socket | None = None
    label = "?"

    def open(self) -> None: ...

    def close(self) -> None:
        if self.sock:
            try:
                self.sock.close()
            except OSError:
                pass
        self.sock = None

    def write(self, data: bytes) -> None:
        if not self.sock:
            raise TransportError("não conectado")
        try:
            self.sock.sendall(data)
        except OSError as e:
            raise TransportError(f"escrita: {e}") from e

    def read_until_prompt(self, timeout: float) -> str:
        """Lê até o prompt '>' do ELM. Retorna o texto sem o prompt."""
        if not self.sock:
            raise TransportError("não conectado")
        buf = bytearray()
        deadline = time.monotonic() + timeout
        while True:
            left = deadline - time.monotonic()
            if left <= 0:
                raise TimeoutError(f"sem prompt em {timeout:.1f}s (recebido {bytes(buf)[-60:]!r})")
            self.sock.settimeout(left)
            try:
                chunk = self.sock.recv(512)
            except socket.timeout:
                continue
            except OSError as e:
                raise TransportError(f"leitura: {e}") from e
            if not chunk:
                raise TransportError("conexão fechada pelo adaptador")
            buf += chunk
            if b">" in chunk:
                return bytes(buf).split(b">")[0].decode("ascii", "replace")

    def drain(self) -> None:
        if not self.sock:
            return
        self.sock.settimeout(0.05)
        try:
            while self.sock.recv(512):
                pass
        except OSError:
            pass


class RfcommTransport(Transport):
    def __init__(self, mac: str, channel: int = 1):
        self.mac, self.channel = mac, channel
        self.label = f"bt:{mac}/{channel}"

    def open(self) -> None:
        self.close()
        s = socket.socket(socket.AF_BLUETOOTH, socket.SOCK_STREAM, socket.BTPROTO_RFCOMM)
        s.settimeout(12)
        try:
            s.connect((self.mac, self.channel))
        except OSError as e:
            s.close()
            raise TransportError(f"bluetooth {self.mac} canal {self.channel}: {e}") from e
        self.sock = s


class TcpTransport(Transport):
    def __init__(self, host: str, port: int):
        self.host, self.port = host, port
        self.label = f"tcp:{host}:{port}"

    def open(self) -> None:
        self.close()
        try:
            self.sock = socket.create_connection((self.host, self.port), timeout=5)
        except OSError as e:
            raise TransportError(f"tcp {self.host}:{self.port}: {e}") from e
