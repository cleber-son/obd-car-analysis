# obd-car-analysis

Telemetria contínua do Honda Civic 2008 1.8 flex com Raspberry Pi 3 + ELM327 Bluetooth.

- `docs/PROCESSO.md` — diário de tudo que foi feito
- `docs/ARQUITETURA.md` — arquitetura do sistema
- `os/` — gravação do cartão SD e configuração headless (cloud-init)
- `secrets/` — credenciais locais (NÃO versionado)

## Uso rápido
- Painel: http://obd.local (ou o IP que chega na notificação do ntfy)
- Diagnóstico no Pi: `cd /opt/obd && PYTHONPATH=src venv/bin/python -m obdpi.cli probe|scan|status|test-push`
- Logs: `journalctl -u obd-collector -f`
- Atualizar o Pi a partir do PC: `scripts/deploy.sh obd.local`
- Testes: `python3 tests/test_parsing.py`
