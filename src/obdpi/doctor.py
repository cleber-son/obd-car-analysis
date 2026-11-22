"""obd-doctor: vigia e conserta o próprio sistema (roda como root a cada 2 min via systemd timer).

Nível 1 — consertos conhecidos, sem IA: serviço parado ou em loop, coletor travado com o motor ligado, Bluetooth sumido,
sincronização atrasada, pouco espaço em disco.

Nível 2 — bug no código (o mesmo erro/traceback se repetindo): envia os logs e os arquivos envolvidos ao Claude Fable, que
propõe uma correção. A correção é testada numa cópia (compilação + testes); se passar, é aplicada com backup, o serviço é
reiniciado e observado por 3 min. Se piorar, volta o backup sozinho. Tudo fica em repair_log, no painel e no iPhone.
O Nível 2 precisa da chave da Anthropic (aba Sistema → Análise por IA) e tem limite de tentativas.
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
import shutil
import sqlite3
import subprocess
import sys
import tarfile
import time
from pathlib import Path

from . import config, notify

log = logging.getLogger("doctor")

APP = Path("/opt/obd")
SRC = APP / "src"
REPAIR_DIR = Path("/data/obd/repair")
STATE = REPAIR_DIR / "doctor_state.json"
SERVICES = ["obd-collector", "obd-web", "obd-sync", "obd-btagent", "obd-netprefer"]
OPTIONAL = ["gpsd", "chrony"]          # GPS: só vigiados se instalados (Pi sem GPS não registra falha à toa)
FIX_MODEL = "claude-fable-5-2"
MAX_AI_PER_DAY = 3
TRACE_RE = re.compile(r"Traceback \(most recent call last\):(?:\n.*?)+?\n(\w[\w.]*(?:Error|Exception|Exit)\b.*)", re.M)


def sh(*cmd, timeout=30) -> tuple[int, str]:
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return r.returncode, (r.stdout + r.stderr)
    except (subprocess.TimeoutExpired, FileNotFoundError) as e:
        return 1, str(e)


class Doctor:
    def __init__(self):
        self.cfg = config.load()
        self.db = self.cfg["storage"]["db_path"]
        REPAIR_DIR.mkdir(parents=True, exist_ok=True)
        try:
            self.state = json.loads(STATE.read_text())
        except Exception:
            self.state = {}
        self.state.setdefault("ai", {})          # assinatura -> ts da última tentativa
        self.state.setdefault("restarts", {})    # serviço -> NRestarts visto

    def save(self):
        STATE.write_text(json.dumps(self.state))

    def record(self, kind, target, action, result, detail="", signature=""):
        log.info("%s %s: %s → %s %s", kind, target, action, result, detail[:200])
        try:
            con = sqlite3.connect(self.db, timeout=30)
            con.execute("""CREATE TABLE IF NOT EXISTS repair_log (id INTEGER PRIMARY KEY, ts INTEGER, kind TEXT, target TEXT,
                           signature TEXT, action TEXT, result TEXT, detail TEXT)""")
            con.execute("INSERT INTO repair_log(ts, kind, target, signature, action, result, detail) VALUES (?,?,?,?,?,?,?)",
                        (int(time.time() * 1000), kind, target, signature, action, result, detail[:8000]))
            if kind == "ia" or result in ("falhou", "revertido"):
                n = self.cfg["notify"]
                prio = notify.PRIO["warn"] if result != "ok" else notify.PRIO["info"]
                con.execute("INSERT INTO outbox(ts, title, body, priority, tags) VALUES (?,?,?,?,?)",
                            (int(time.time() * 1000), f"Auto-reparo: {target} ({result})", f"{action}. {detail[:300]}", prio, "wrench,car"))
            con.commit()
            con.close()
        except sqlite3.Error as e:
            log.warning("não gravou repair_log: %s", e)

    # -------------------------------------------------------------- nível 1
    def check_services(self) -> list[str]:
        broken = []
        for u in SERVICES + [o for o in OPTIONAL if sh("systemctl", "cat", o, timeout=10)[0] == 0]:
            _, act = sh("systemctl", "is-active", u)
            _, nr = sh("systemctl", "show", u, "-p", "NRestarts", "--value")
            act, nr = act.strip(), int((nr.strip() or "0") if nr.strip().isdigit() else 0)
            prev = self.state["restarts"].get(u, nr)
            self.state["restarts"][u] = nr
            if u == "gpsd" and act == "inactive" and sh("systemctl", "is-active", "gpsd.socket")[1].strip() == "active":
                continue                               # gpsd por socket: parado à espera de cliente é normal
            if act in ("failed", "inactive"):
                sh("systemctl", "reset-failed", u)
                code, _ = sh("systemctl", "restart", u, timeout=60)
                self.record("servico", u, f"estava '{act}', reiniciado", "ok" if code == 0 else "falhou")
                broken.append(u)
            elif nr - prev >= 5:                       # reiniciando em loop: provável bug
                broken.append(u)
                self.record("servico", u, f"{nr - prev} reinícios desde a última verificação", "investigando")
        return broken

    def check_bluetooth(self):
        code, out = sh("bluetoothctl", "list", timeout=10)
        if "Controller" not in out:
            code, _ = sh(str(APP / "deploy" / "bt-ensure.sh"), timeout=60)
            self.record("bluetooth", "hci0", "controlador ausente, driver recarregado", "ok" if code == 0 else "falhou")

    def check_collector_progress(self):
        live = Path(self.cfg["runtime"]["live_sock"]).parent / "live.json"
        try:
            age = time.time() - live.stat().st_mtime
            d = json.loads(live.read_text())
        except Exception:
            return
        # o coletor reescreve live.json a cada ciclo; parado há 3 min = travado (o watchdog do systemd é a 1ª linha)
        if age > 180:
            sh("systemctl", "restart", "obd-collector", timeout=60)
            self.record("coletor", "obd-collector", f"sem atualizar há {int(age)} s, reiniciado", "ok")
        elif d.get("engine_on") and d.get("state") == "coletando":
            try:
                con = sqlite3.connect(f"file:{self.db}?mode=ro", uri=True, timeout=10)
                last = con.execute("SELECT MAX(ts) FROM fast_sample").fetchone()[0] or 0
                con.close()
                if d.get("ts", 0) - last > 120_000:
                    sh("systemctl", "restart", "obd-collector", timeout=60)
                    self.record("coletor", "obd-collector", "motor ligado mas sem gravar amostras há 2 min, reiniciado", "ok")
            except sqlite3.Error:
                pass

    def check_sync(self):
        c = self.cfg.get("cloud", {})
        if not c.get("url"):
            return
        code, out = sh("curl", "-s", "-m", "8", "-o", "/dev/null", "-w", "%{http_code}", "https://1.1.1.1", timeout=15)
        if out.strip() not in ("200", "301", "302"):
            return                                   # sem internet: atraso é esperado
        code, out = sh("curl", "-s", "-m", "8", c["url"].rstrip("/") + "/api/env", timeout=15)
        try:
            last = json.loads(out).get("last_sync_ts") or 0
        except ValueError:
            return
        if time.time() * 1000 - last > 5 * 60_000:
            sh("systemctl", "restart", "obd-sync", timeout=60)
            self.record("sync", "obd-sync", "com internet mas sem enviar há >5 min, reiniciado", "ok")

    def check_disk(self):
        du = shutil.disk_usage("/data" if Path("/data").exists() else "/")
        if du.free / du.total < 0.10:
            sh("journalctl", "--vacuum-size=10M")
            sh("sudo", "-u", "obd", str(APP / "venv/bin/python"), "-m", "obdpi.maint", timeout=600)
            self.record("disco", "/data", f"{du.free / du.total:.0%} livre, limpeza e retenção aplicadas", "ok")

    # -------------------------------------------------------------- nível 2 (IA)
    def recent_errors(self) -> list[dict]:
        """Tracebacks dos serviços obd-* nos últimos 15 min, agrupados por assinatura."""
        found = {}
        for u in ("obd-collector", "obd-web", "obd-sync"):
            _, out = sh("journalctl", "-u", u, "--since", "-15min", "--no-pager", "-o", "cat", timeout=30)
            for m in TRACE_RE.finditer(out):
                block = m.group(0)
                frames = re.findall(r'File "(/opt/obd/src/[^"]+)", line (\d+)', block)
                if not frames:
                    continue                          # erro fora do nosso código (biblioteca/sistema): não é caso de patch
                last_file, last_line = frames[-1]
                sig = hashlib.sha1(f"{u}|{m.group(1).split(':')[0]}|{last_file}|{last_line}".encode()).hexdigest()[:12]
                e = found.setdefault(sig, {"unit": u, "error": m.group(1).strip(), "trace": block[-4000:], "files": set(), "n": 0})
                e["files"].update(f for f, _ in frames)
                e["n"] += 1
        return [dict(v, signature=k, files=sorted(v["files"])) for k, v in found.items() if v["n"] >= 2]

    def ai_client(self):
        from . import ai
        try:
            import anthropic
        except ImportError:
            return None, None
        key = ai.settings().get("anthropic_key") or ai._secrets_key()
        return (anthropic.Anthropic(api_key=key, max_retries=2, timeout=900), anthropic) if key else (None, None)

    def ai_repair(self, err: dict):
        sig = err["signature"]
        today = [t for t in self.state["ai"].values() if time.time() - t < 86400]
        if time.time() - self.state["ai"].get(sig, 0) < 6 * 3600 or len(today) >= MAX_AI_PER_DAY:
            return
        client, anthropic = self.ai_client()
        if client is None:
            self.record("ia", err["unit"], "bug detectado, mas sem chave da Anthropic para auto-reparo", "pulado",
                        err["error"], sig)
            self.state["ai"][sig] = time.time()
            return
        self.state["ai"][sig] = time.time()
        self.save()
        files = {}
        for f in err["files"][-4:]:
            p = Path(f)
            if p.is_file() and SRC in p.parents:
                files[str(p.relative_to(APP))] = p.read_text()
        _, logs = sh("journalctl", "-u", err["unit"], "-n", "120", "--no-pager", "-o", "cat", timeout=30)
        prompt = (f"O serviço {err['unit']} de um registrador OBD-II em Raspberry Pi (Python, coleta de dados de um Honda Civic via "
                  f"ELM327) está falhando repetidamente.\n\nErro: {err['error']}\n\nTraceback:\n{err['trace']}\n\nÚltimos logs:\n{logs[-6000:]}\n\n"
                  "Arquivos envolvidos (caminho relativo a /opt/obd):\n" +
                  "\n".join(f"=== {k} ===\n{v}" for k, v in files.items()) +
                  "\n\nProponha a menor correção segura usando a ferramenta propose_fix. Cada edição substitui um trecho exato "
                  "(old_str deve existir exatamente uma vez no arquivo). Não mude comportamento além do necessário, não apague "
                  "dados, não toque em segredos. Se não tiver confiança, chame propose_fix com edits vazio e explique.")
        tool = {"name": "propose_fix", "strict": True,
                "description": "Propõe edições exatas de texto nos arquivos para corrigir o bug.",
                "input_schema": {"type": "object", "additionalProperties": False, "required": ["explanation", "confidence", "edits"],
                                 "properties": {
                                     "explanation": {"type": "string", "description": "Causa e correção, em português."},
                                     "confidence": {"type": "string", "enum": ["baixa", "media", "alta"]},
                                     "edits": {"type": "array", "items": {"type": "object", "additionalProperties": False,
                                               "required": ["path", "old_str", "new_str"],
                                               "properties": {"path": {"type": "string"}, "old_str": {"type": "string"},
                                                              "new_str": {"type": "string"}}}}}}}
        try:
            with client.beta.messages.stream(
                model=FIX_MODEL, max_tokens=32000, output_config={"effort": "high"},
                betas=["server-side-fallback-2026-07-01"], fallbacks="default",
                tools=[tool], tool_choice={"type": "auto"},
                messages=[{"role": "user", "content": prompt}],
            ) as stream:
                msg = stream.get_final_message()
        except anthropic.APIConnectionError:
            self.record("ia", err["unit"], "sem internet para pedir correção", "pulado", err["error"], sig)
            self.state["ai"].pop(sig, None)
            return
        except anthropic.APIStatusError as e:
            self.record("ia", err["unit"], f"API recusou ({e.status_code})", "falhou", str(e.message), sig)
            return
        if msg.stop_reason == "refusal":
            self.record("ia", err["unit"], "modelo recusou", "falhou", err["error"], sig)
            return
        call = next((b for b in msg.content if b.type == "tool_use" and b.name == "propose_fix"), None)
        if not call:
            txt = "".join(b.text for b in msg.content if b.type == "text")
            self.record("ia", err["unit"], "sem proposta de correção", "falhou", txt[:2000], sig)
            return
        fix = call.input if isinstance(call.input, dict) else json.loads(call.input)
        (REPAIR_DIR / f"proposta-{sig}-{int(time.time())}.json").write_text(json.dumps({"erro": err["error"], **fix}, ensure_ascii=False, indent=1))
        if not fix.get("edits") or fix.get("confidence") == "baixa":
            self.record("ia", err["unit"], "Fable não teve confiança para corrigir", "pulado", fix.get("explanation", ""), sig)
            return
        self.apply_fix(err, fix, sig)

    def apply_fix(self, err: dict, fix: dict, sig: str):
        stage = Path("/tmp/obd-repair")
        shutil.rmtree(stage, ignore_errors=True)
        shutil.copytree(APP / "src", stage / "src")
        shutil.copytree(APP / "tests", stage / "tests") if (APP / "tests").exists() else None
        for e in fix["edits"]:
            rel = e["path"].lstrip("/").removeprefix("opt/obd/")
            p = stage / rel
            if not rel.startswith("src/obdpi/") or ".." in rel or not p.is_file():
                self.record("ia", err["unit"], f"edição recusada (fora do código: {rel})", "falhou", fix["explanation"], sig)
                return
            txt = p.read_text()
            if txt.count(e["old_str"]) != 1:
                self.record("ia", err["unit"], f"edição não se aplica em {rel}", "falhou", fix["explanation"], sig)
                return
            p.write_text(txt.replace(e["old_str"], e["new_str"]))
        py = str(APP / "venv/bin/python")
        code, out = sh(py, "-m", "compileall", "-q", str(stage / "src"), timeout=120)
        if code == 0 and (stage / "tests/test_parsing.py").exists():
            code, out = sh(py, str(stage / "tests/test_parsing.py"), timeout=300)
        if code != 0:
            self.record("ia", err["unit"], "correção reprovada nos testes, não aplicada", "falhou", f"{fix['explanation']}\n{out[-1500:]}", sig)
            return
        backup = REPAIR_DIR / f"backup-{int(time.time())}.tar.gz"
        with tarfile.open(backup, "w:gz") as t:
            t.add(APP / "src", arcname="src")
        for e in fix["edits"]:
            rel = e["path"].lstrip("/").removeprefix("opt/obd/")
            shutil.copy2(stage / rel, APP / rel)
        sh("chown", "-R", "obd:obd", str(APP / "src"))
        sh("systemctl", "restart", err["unit"], timeout=60)
        time.sleep(180)
        _, act = sh("systemctl", "is-active", err["unit"])
        still = any(x["signature"] == sig for x in self.recent_errors_since(180))
        if act.strip() != "active" or still:
            with tarfile.open(backup) as t:
                shutil.rmtree(APP / "src")
                t.extractall(APP, filter="data")
            sh("chown", "-R", "obd:obd", str(APP / "src"))
            sh("systemctl", "restart", err["unit"], timeout=60)
            self.record("ia", err["unit"], "correção aplicada mas o erro continuou; backup restaurado", "revertido", fix["explanation"], sig)
            return
        # com o sistema somente leitura a correção ficaria só na memória: grava no cartão (sem proteção não faz nada)
        sh(str(APP / "deploy/overlay.sh"), "persist", *[str(APP / e["path"].lstrip("/").removeprefix("opt/obd/")) for e in fix["edits"]], timeout=60)
        self.record("ia", err["unit"], f"correção do Fable aplicada ({len(fix['edits'])} edição(ões)); serviço estável",
                    "ok", json.dumps(fix, ensure_ascii=False)[:7000], sig)

    def recent_errors_since(self, seconds: int) -> list[dict]:
        found = []
        for u in ("obd-collector", "obd-web", "obd-sync"):
            _, out = sh("journalctl", "-u", u, "--since", f"-{seconds}s", "--no-pager", "-o", "cat", timeout=30)
            for m in TRACE_RE.finditer(out):
                frames = re.findall(r'File "(/opt/obd/src/[^"]+)", line (\d+)', m.group(0))
                if frames:
                    lf, ll = frames[-1]
                    found.append({"signature": hashlib.sha1(f"{u}|{m.group(1).split(':')[0]}|{lf}|{ll}".encode()).hexdigest()[:12]})
        return found

    # -------------------------------------------------------------- execução
    def run(self):
        for step in (self.check_services, self.check_bluetooth, self.check_collector_progress, self.check_sync, self.check_disk):
            try:
                step()
            except Exception as e:
                log.warning("%s falhou: %s", step.__name__, e)
        try:
            for err in self.recent_errors():
                self.ai_repair(err)
        except Exception as e:
            log.warning("auto-reparo por IA falhou: %s", e)
        self.save()


def main():
    logging.basicConfig(level="INFO", stream=sys.stdout, format="%(levelname)s doctor: %(message)s")
    Doctor().run()


if __name__ == "__main__":
    main()
