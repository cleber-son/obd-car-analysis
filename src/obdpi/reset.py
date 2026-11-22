"""Reset geral (botão "Resetar tudo" em Sistema): o Pi volta a tratar o carro como recém-instalado.

O painel só deixa o pedido (arquivo reset.pending) e manda o coletor reiniciar; quem aplica é o coletor ao subir,
antes de abrir o banco, porque ele é o único escritor e guarda muita coisa em memória (viagem aberta, ids dos sinais,
linha de base). Nada é destruído: o banco e os backups diários vão para backup/antes-do-reset-<data>/ (só a cópia do
último reset fica guardada) e um banco novo nasce vazio. Cada passo pode ser repetido: se a energia cair no meio, o
pedido continua lá e termina na próxima partida.

Continuam como estão: adaptador Bluetooth e protocolo descobertos, WiFi, chaves de IA, notificações e nuvem. O banco novo
recebe do antigo os lugares, os trechos de referência e os modelos do GPS (configuração do dono, não histórico).
"""
from __future__ import annotations

import fcntl
import json
import os
import shutil
import signal
import sqlite3
import threading
import time
from pathlib import Path

MARK, DONE = "reset.pending", "reset.done"
STATE_KEYS = ("throttle", "trip_pushed")     # aprendizado da borboleta e "viagem já avisada" (os ids recomeçam do 1)


def _base(cfg: dict) -> Path:
    return Path(cfg["storage"]["db_path"]).parent


def pending(cfg: dict) -> bool:
    return (_base(cfg) / MARK).exists()


def last(cfg: dict) -> int | None:
    """Hora (ms) do último reset concluído. O obd-sync usa para apagar a cópia antiga na nuvem."""
    try:
        return int(json.loads((_base(cfg) / DONE).read_text())["ts"])
    except (OSError, ValueError, KeyError, TypeError):
        return None


def apply(cfg: dict) -> bool:
    """Executa um reset pedido. Devolve True se resetou; False se não havia pedido (ou outro processo já está fazendo)."""
    base, dbp = _base(cfg), Path(cfg["storage"]["db_path"])
    mark = base / MARK
    try:
        fd = os.open(mark, os.O_RDONLY)
    except FileNotFoundError:
        return False
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            if os.fstat(fd).st_ino != os.stat(mark).st_ino:
                return False                      # o pedido que abrimos já foi concluído por outro processo
            ts = int(mark.read_text().strip() or 0) or int(time.time() * 1000)
        except (OSError, ValueError):
            return False
        bdir = Path(cfg["storage"]["backup_dir"])
        keep = bdir / ("antes-do-reset-" + time.strftime("%Y%m%d-%H%M%S", time.localtime(ts / 1000)))
        for old in bdir.glob("antes-do-reset-*"):
            if old != keep:
                shutil.rmtree(old, ignore_errors=True)
        keep.mkdir(parents=True, exist_ok=True)
        if dbp.exists() and not (keep / dbp.name).exists():
            try:                                  # junta o -wal no arquivo principal: a cópia guardada fica inteira
                con = sqlite3.connect(dbp, timeout=10)
                con.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                con.close()
            except sqlite3.Error:
                pass
            for suf in ("-wal", "-shm", ""):      # o principal por último: é ele que marca "já foi guardado"
                f = dbp.with_name(dbp.name + suf)
                if f.exists():
                    shutil.move(str(f), str(keep / f.name))
        else:                                     # retomada: o banco já foi guardado; o que há agora é um toco vazio
            for suf in ("", "-wal", "-shm"):
                dbp.with_name(dbp.name + suf).unlink(missing_ok=True)
        for b in list(bdir.glob("obd-*.db")) + list(bdir.glob("obd-*.db.tmp")):
            shutil.move(str(b), str(keep / b.name))   # senão um banco corrompido amanhã traria os dados antigos de volta
        carry_config(keep / dbp.name, dbp)
        sp = Path(cfg["storage"]["state_path"])
        try:
            st = json.loads(sp.read_text())
            if any(k in st for k in STATE_KEYS):
                tmp = sp.with_suffix(".tmp")
                tmp.write_text(json.dumps({k: v for k, v in st.items() if k not in STATE_KEYS}, indent=1))
                os.replace(tmp, sp)
        except (OSError, ValueError):
            pass
        tmp = base / (DONE + ".tmp")
        tmp.write_text(json.dumps({"ts": ts, "copy": str(keep)}))
        os.replace(tmp, base / DONE)
        mark.unlink(missing_ok=True)
        return True
    finally:
        os.close(fd)


CARRY_TABLES = ("place", "segment", "gps_model")   # configuração do dono (lugares, trechos, modelos do GPS), não histórico


def carry_config(old: Path, new: Path) -> None:
    """Leva lugares, trechos de referência e gps_model do banco guardado para o banco novo (que nasce aqui, com o
    esquema completo). Repetível: INSERT OR REPLACE pelas chaves. Qualquer erro só deixa o banco novo sem eles."""
    if not old.exists():
        return
    from . import db
    con = None
    try:
        con = db.init(str(new))
        con.execute("ATTACH DATABASE ? AS old", (str(old),))
        for t in CARRY_TABLES:
            if not con.execute("SELECT 1 FROM old.sqlite_master WHERE type='table' AND name=?", (t,)).fetchone():
                continue
            have = [r[1] for r in con.execute(f"PRAGMA main.table_info({t})")]
            cols = [c for c in (r[1] for r in con.execute(f"PRAGMA old.table_info({t})")) if c in have]
            if cols:
                cl = ", ".join(cols)
                con.execute(f"INSERT OR REPLACE INTO main.{t}({cl}) SELECT {cl} FROM old.{t}")
        con.commit()
        con.execute("DETACH DATABASE old")
    except sqlite3.Error:
        pass
    finally:
        if con is not None:
            con.close()


def _collector_pids() -> list[int]:
    out = []
    for p in Path("/proc").glob("[0-9]*"):
        try:
            args = (p / "cmdline").read_bytes().split(b"\0")
            if int(p.name) != os.getpid() and b"-m" in args and args[args.index(b"-m") + 1:][:1] == [b"obdpi.collector.main"]:
                out.append(int(p.name))
        except (OSError, ValueError):
            pass
    return out


def _kill(pids: list[int], sig: int) -> None:
    for pid in pids:
        try:
            os.kill(pid, sig)
        except OSError:
            pass


def _finish(cfg: dict, old: list[int]) -> None:
    """Acompanha o pedido: força a saída do coletor se ele não largar e, sem coletor para aplicar, aplica aqui."""
    t0 = time.monotonic()
    while pending(cfg) and time.monotonic() - t0 < 90:
        time.sleep(1)
        alive = _collector_pids()
        if time.monotonic() - t0 > 8:
            _kill([p for p in old if p in alive], signal.SIGKILL)     # preso numa chamada do Bluetooth
        if not alive and time.monotonic() - t0 > 15:
            apply(cfg)                                                # coletor não é relançado (fora do systemd)


def request(cfg: dict) -> dict:
    """Chamado pelo painel. Deixa o pedido e reinicia o coletor (mesmo usuário; o systemd o relança em segundos)."""
    base = _base(cfg)
    base.mkdir(parents=True, exist_ok=True)
    tmp = base / (MARK + ".tmp")
    tmp.write_text(str(int(time.time() * 1000)))
    os.replace(tmp, base / MARK)
    pids = _collector_pids()
    if not pids:
        apply(cfg)
    else:
        _kill(pids, signal.SIGTERM)
        threading.Thread(target=_finish, args=(cfg, pids), daemon=True, name="reset").start()
    return {"pending": pending(cfg)}
