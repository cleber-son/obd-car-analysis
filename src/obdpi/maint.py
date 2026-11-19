"""Manutenção periódica: backup diário do banco (VACUUM INTO, conferido, 7 cópias), limite de tamanho, limpeza."""
from __future__ import annotations

import os
import shutil
import sqlite3
import time
from pathlib import Path

from . import config


def main():
    cfg = config.load()
    dbp = cfg["storage"]["db_path"]
    bdir = Path(cfg["storage"]["backup_dir"])
    if not Path(dbp).exists():
        return
    bdir.mkdir(parents=True, exist_ok=True)
    from . import reports
    try:
        for lbl in reports.queue_due(dbp):
            print("relatório na fila:", lbl)
    except Exception as e:
        print("relatório falhou:", e)
    # backup diário: o Pi perde a energia junto com o carro, então a cópia íntegra mais recente é o plano B do banco.
    # Só vale a cópia que passa na conferência; ficam as 7 últimas.
    backups = sorted(bdir.glob("obd-*.db"))
    if not backups or time.time() - backups[-1].stat().st_mtime > 20 * 3600:
        tmp = bdir / f"obd-{time.strftime('%Y%m%d')}.db.tmp"
        tmp.unlink(missing_ok=True)
        con = sqlite3.connect(dbp, timeout=30)
        con.execute(f"VACUUM INTO '{tmp}'")
        con.close()
        chk = sqlite3.connect(tmp)
        ok = chk.execute("PRAGMA quick_check").fetchone()[0]
        chk.close()
        if ok == "ok":
            fd = os.open(tmp, os.O_RDONLY)
            os.fsync(fd)                          # no cartão de verdade antes de valer como backup
            os.close(fd)
            os.replace(tmp, tmp.with_suffix(""))
            print("backup:", tmp.with_suffix(""))
            for old in sorted(bdir.glob("obd-*.db"))[:-7]:
                old.unlink()
        else:
            tmp.unlink(missing_ok=True)
            print("backup descartado (cópia não passou na conferência):", ok)
    # guarda-corpo de espaço: se o disco ficar com < 15% livre, apaga amostras rápidas com mais de 12 meses
    du = shutil.disk_usage(Path(dbp).parent)
    if du.free / du.total < 0.15:
        con = sqlite3.connect(dbp, timeout=30)
        cut = int((time.time() - 365 * 86400) * 1000)
        con.execute("DELETE FROM fast_sample WHERE ts < ?", (cut,))
        con.commit()
        con.execute("PRAGMA incremental_vacuum")
        con.close()
        print("retenção aplicada")


if __name__ == "__main__":
    main()
