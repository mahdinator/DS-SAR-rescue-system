"""Chain of command (architecture doc, section 12), modelled on ICS.

Who may do what is part of the replicated state, so it is checked when a
command is *applied*, not when it is submitted. Between submitting and
committing, command may have changed hands; every station applies the log in
the same order, so every station refuses the same commands.

Roles (from the scenario roster):
  commander   may become incident commander (IC). The IC alone may: hand over
              command, set priority zones, acknowledge lost aircraft, settle
              conflicting verdicts.
  analyst     reviews sightings (claims and verdicts).
  observer    reads only.
An empty roster switches all checks off (steps 1-6 scenarios).

Handover needs acceptance: the IC offers command, the other person accepts.
Until then the IC stays in command, so command is never with someone who is
not there. If the IC goes silent, the leader station offers command to the next
present person in the succession list; accepting makes them *acting* IC.
"""
from __future__ import annotations

import math

ROLES = ("commander", "analyst", "observer")


class CommandState:
    def __init__(self, cfg: dict):
        self.roster: dict[str, str] = {p["id"]: p["role"] for p in cfg.get("roster", [])}
        self.initial = cfg.get("initial")
        self.succession: list[str] = list(cfg.get("succession", []))
        self.reset()

    def reset(self) -> None:
        self.commander: str | None = self.initial
        self.acting = False
        self.offer: dict | None = None  # {"to", "by", "kind"}
        self.zones: dict[int, dict] = {}
        self.n_zones = 0
        self.alerts: dict[str, dict] = {}  # id -> {"kind", "drone", "sector", "acked_by", "index"}
        self.audit: list[dict] = []

    @property
    def enabled(self) -> bool:
        return bool(self.roster)

    def role_of(self, person: str) -> str | None:
        return self.roster.get(person)

    def may_review(self, person: str) -> bool:
        return not self.enabled or self.role_of(person) in ("analyst", "commander")

    # -- apply (every replica, log order) ----------------------------------------------

    def apply(self, index: int, cmd: dict) -> tuple[bool, str]:
        """Apply one command action. Returns (ok, reason); the audit trail keeps both."""
        ok, reason = self._apply(cmd)
        self.audit.append({"index": index, "by": cmd.get("by"), "action": cmd.get("action"),
                           "args": cmd.get("args", {}), "ok": ok, "reason": reason, "t": cmd.get("t")})
        return ok, reason

    def _apply(self, cmd: dict) -> tuple[bool, str]:
        by, action, a = cmd.get("by"), cmd.get("action"), cmd.get("args", {})
        if action == "succession":  # proposed by the leader station when the IC is silent
            to = a.get("to")
            if self.role_of(to) != "commander":
                return False, f"{to} cannot hold command"
            if to == self.commander:
                return False, f"{to} already holds command"
            self.offer = {"to": to, "by": "system", "kind": "succession", "absent": a.get("absent")}
            return True, ""
        if action in ("accept", "decline"):
            if not self.offer or self.offer["to"] != by:
                return False, "no offer for you"
            if action == "accept":
                self.acting = self.offer["kind"] == "succession"
                self.commander = by
            self.offer = None
            return True, ""
        # Everything else is for the incident commander only (anyone, with no roster).
        if self.enabled and by != self.commander:
            return False, f"{by} is not in command (IC is {self.commander})"
        if action == "transfer":
            to = a.get("to")
            if self.role_of(to) != "commander" or to == by:
                return False, f"cannot hand command to {to}"
            self.offer = {"to": to, "by": by, "kind": "transfer"}
            return True, ""
        if action == "zone":
            self.n_zones += 1
            self.zones[self.n_zones] = {"id": self.n_zones, "x": float(a["x"]), "y": float(a["y"]),
                                        "radius_m": float(a["radius_m"]), "weight": float(a["weight"]), "by": by}
            return True, ""
        if action == "clear_zone":
            return (True, "") if self.zones.pop(int(a["id"]), None) else (False, "no such zone")
        if action == "ack":
            alert = self.alerts.get(a.get("alert"))
            if alert is None:
                return False, "no such alert"
            if alert["acked_by"]:
                return False, f"already acknowledged by {alert['acked_by']}"
            alert["acked_by"] = by
            return True, ""
        if action == "resolve":
            return True, ""  # the verdict part is applied by the review board (station._apply)
        return False, f"unknown action {action!r}"

    def raise_alert(self, alert_id: str, kind: str, **info) -> bool:
        if alert_id in self.alerts:
            return False
        self.alerts[alert_id] = {"id": alert_id, "kind": kind, "acked_by": None, **info}
        return True

    # -- reads ---------------------------------------------------------------------------

    def zone_weight(self, x: float, y: float) -> float:
        w = 1.0
        for z in self.zones.values():
            if math.hypot(x - z["x"], y - z["y"]) <= z["radius_m"]:
                w = max(w, z["weight"])
        return w

    def view(self) -> dict:
        return {"commander": self.commander, "acting": self.acting, "offer": self.offer,
                "zones": list(self.zones.values()), "alerts": list(self.alerts.values()),
                "roster": self.roster, "audit_len": len(self.audit)}
