from dataclasses import dataclass, field


@dataclass
class Alert:
    source: str          # limits | baseline | throttle | dtc | system
    rule: str            # id da regra (ex.: R4, ect_high, P2135)
    severity: str        # info | warn | crit
    title: str
    detail: str = ""
    signal: str = ""
    value: float | None = None
    ts: int = 0
    extra: dict = field(default_factory=dict)


SEV_ORDER = {"info": 0, "warn": 1, "crit": 2}
