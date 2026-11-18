"""Driver ELM327 mínimo e robusto (cabeçalhos desligados, formatação CAN do próprio ELM)."""
from __future__ import annotations

import json
import logging
import re
import time
from pathlib import Path

from .transport import Transport, TransportError

log = logging.getLogger("elm")

HEX_RE = re.compile(r"^[0-9A-F]+$")
FRAME_IDX_RE = re.compile(r"^[0-9A-F]:")

# Erros que indicam que a ECU não está respondendo (ignição desligada, protocolo errado)
BUS_ERRORS = ("UNABLE TO CONNECT", "CAN ERROR", "BUS INIT", "BUS ERROR", "BUS BUSY", "FB ERROR", "DATA ERROR", "<RX ERROR", "ERR")
RESET_ERRORS = ("LV RESET", "STOPPED", "BUFFER FULL", "ACT ALERT")


class ElmError(Exception):
    pass


class EcuSilent(ElmError):
    """ELM ok, mas a ECU não responde (ignição desligada?)."""


class ElmNeedsReset(ElmError):
    pass


class ELM327:
    def __init__(self, transport: Transport, protocol: str = "auto", raw_log: str | None = None):
        self.t = transport
        self.protocol = protocol
        self.version = ""
        self.protocol_desc = ""
        self.protocol_num = ""
        self.multi_pid = False
        self.is_can = False
        self.raw_path = Path(raw_log) if raw_log else None
        self.n_requests = 0

    # ---------------- baixo nível ----------------
    def _raw(self, direction: str, text: str) -> None:
        if self.raw_path:
            with self.raw_path.open("a") as f:
                f.write(json.dumps({"t": round(time.time(), 3), "d": direction, "x": text}) + "\n")

    def cmd(self, c: str, timeout: float = 2.0) -> list[str]:
        """Envia um comando e devolve as linhas (sem vazias, sem eco, sem SEARCHING)."""
        self._raw(">", c)
        self.t.write((c + "\r").encode("ascii"))
        txt = self.t.read_until_prompt(timeout)
        self.n_requests += 1
        self._raw("<", txt)
        lines = []
        for ln in re.split(r"[\r\n]+", txt):
            ln = ln.strip()
            if not ln or ln == c or ln.replace(" ", "") == c.replace(" ", ""):
                continue
            if ln.startswith("SEARCHING"):
                continue
            lines.append(ln)
        return lines

    # ---------------- inicialização ----------------
    def init(self) -> None:
        self.t.drain()
        try:
            lines = self.cmd("ATZ", timeout=4)
        except TimeoutError:
            self.t.write(b"\r")
            time.sleep(0.5)
            self.t.drain()
            lines = self.cmd("ATZ", timeout=4)
        self.version = next((l for l in lines if "ELM" in l.upper()), " ".join(lines))
        for c in ("ATE0", "ATL0", "ATS0", "ATH0", "ATAT1", "ATST64"):
            self.cmd(c)
        sp = "0" if self.protocol in ("auto", "0", "") else str(self.protocol)
        self.cmd(f"ATSP{sp}")
        # Abre o barramento
        r = self.cmd("0100", timeout=12 if sp == "0" else 5)
        self._check_errors(r)
        self.protocol_num = (self.cmd("ATDPN") or [""])[0].lstrip("A")
        self.protocol_desc = (self.cmd("ATDP") or [""])[0]
        self.is_can = self.protocol_num in ("6", "7", "8", "9")
        self.multi_pid = self._probe_multi()
        log.info("ELM %s | protocolo %s (%s) | multi-PID %s", self.version, self.protocol_num, self.protocol_desc, self.multi_pid)

    def _probe_multi(self) -> bool:
        if not self.is_can:
            return False
        try:
            r = self.request(0x01, [0x0C, 0x0D])
            return 0x0C in r and 0x0D in r
        except ElmError:
            return False

    def voltage(self) -> float | None:
        try:
            v = self.cmd("ATRV")
            m = re.search(r"(\d+(?:\.\d+)?)", v[0] if v else "")
            return float(m.group(1)) if m else None
        except (TimeoutError, TransportError):
            raise
        except Exception:
            return None

    # ---------------- requisições OBD ----------------
    @staticmethod
    def _check_errors(lines: list[str]) -> None:
        joined = " ".join(lines).upper()
        if any(e in joined for e in RESET_ERRORS):
            raise ElmNeedsReset(joined)
        if any(e in joined for e in BUS_ERRORS):
            raise EcuSilent(joined)
        if joined.strip() == "?":
            raise ElmError("comando não reconhecido")

    def messages(self, c: str, timeout: float = 2.0) -> list[bytes]:
        """Envia uma requisição OBD e devolve a lista de mensagens (bytes), já remontadas."""
        lines = self.cmd(c, timeout)
        if any("NO DATA" in l.upper() for l in lines):
            return []
        self._check_errors(lines)
        msgs: list[bytes] = []
        multi: bytearray | None = None
        expected = 0
        for ln in lines:
            ln = ln.replace(" ", "").upper()
            if FRAME_IDX_RE.match(ln):                     # quadro de multi-frame CAN "0:..."
                if multi is None:
                    multi = bytearray()
                multi += bytes.fromhex(ln[2:]) if len(ln[2:]) % 2 == 0 else b""
                continue
            if len(ln) == 3 and HEX_RE.match(ln):          # comprimento do multi-frame (ex.: 014)
                if multi:
                    msgs.append(bytes(multi[:expected] if expected else multi))
                multi, expected = bytearray(), int(ln, 16)
                continue
            if HEX_RE.match(ln) and len(ln) % 2 == 0:
                msgs.append(bytes.fromhex(ln))
        if multi:
            msgs.append(bytes(multi[:expected] if expected else multi))
        return msgs

    def request(self, mode: int, pids: list[int], lengths: dict[int, int] | None = None) -> dict[int, bytes]:
        """Modo 01/02 com um ou mais PIDs (multi-PID só em CAN). Retorna {pid: bytes_de_dados}."""
        from ..obd.pids import PID_LEN

        lengths = lengths or PID_LEN
        c = f"{mode:02X}" + "".join(f"{p:02X}" for p in pids)
        out: dict[int, bytes] = {}
        for m in self.messages(c, timeout=3.0 if len(pids) > 1 else 2.0):
            if not m or m[0] != mode + 0x40:
                continue
            i = 1
            if mode == 0x02:                    # freeze frame: 42 <pid> <frame> <dados>
                if len(m) >= 3 and m[1] in pids:
                    n = lengths.get(m[1], len(m) - 3)
                    out[m[1]] = bytes(m[3:3 + n])
                continue
            while i < len(m):
                pid = m[i]
                n = lengths.get(pid)
                if n is None:
                    if len(pids) == 1:          # PID desconhecido: pega o resto
                        out[pid] = bytes(m[i + 1:])
                    break
                if pid in pids:
                    out[pid] = bytes(m[i + 1:i + 1 + n])
                i += 1 + n
                if mode == 0x02:                # freeze frame: pid, frame, dados
                    break
        return out

    def raw_mode(self, c: str) -> list[bytes]:
        return self.messages(c, timeout=4.0)
