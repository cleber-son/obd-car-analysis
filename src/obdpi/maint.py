"""Manutenção periódica: backup semanal do banco (VACUUM INTO), limite de tamanho, limpeza."""
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
    backups = sorted(bdir.glob("obd-*.db"))
    if not backups or time.time() - backups[-1].stat().st_mtime > 7 * 86400:
        tmp = bdir / f"obd-{time.strftime('%Y%m%d')}.db.tmp"
        con = sqlite3.connect(dbp, timeout=30)
        con.execute(f"VACUUM INTO '{tmp}'")
        con.close()
        os.replace(tmp, tmp.with_suffix(""))
        print("backup:", tmp.with_suffix(""))
        for old in sorted(bdir.glob("obd-*.db"))[:-2]:
            old.unlink()
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
