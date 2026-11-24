"""Observer: the all-seeing view of a run, built by reading the event log live.

This is NOT what any node knows. Stations only know what reached them; the
observer sees true drone positions, every link's delivery rate, real victims and
decoys, and every node's events. Showing both side by side (the station's belief
vs. the truth) is the point of the dashboard.
"""
from __future__ import annotations

import json
import time
from collections import deque
from pathlib import Path

from ..tools.report import compute

FEED_SIZE = 300
LINK_TTL_S = 6.0  # real seconds a link measurement stays on the map
METRICS_EVERY_S = 3.0


def _mmss(t: float) -> str:
    t = max(0.0, t)
    return f"{int(t // 60)}:{int(t % 60):02d}"


class Observer:
    def __init__(self, run_dir: Path, nodes: dict):
        self.path = Path(run_dir) / "events.jsonl"
        self.nodes = nodes
        self._offset = 0
        self._partial = ""
        self.events: list[dict] = []
        self.drones: dict[str, dict] = {}
        self.trails: dict[str, deque] = {}
        self.job_rects: dict[int, list] = {}  # job -> [i0, j0, i1, j1] (from assignments)
        self.cell_m = 20.0
        self.stations: dict[str, dict] = {
            nid: {"x": n["pos"][0], "y": n["pos"][1]} for nid, n in nodes.items() if n["role"] == "groundstation"
        }
        self.links: dict[tuple[str, str], dict] = {}
        self.clusters: dict[str, dict] = {}
        self.feed: deque = deque(maxlen=FEED_SIZE)
        self.truth_by_det: dict[tuple, str | None] = {}
        self.det_by_sighting: dict[str, tuple] = {}
        self.confirmed_victims: set[str] = set()
        self.detected_victims: set[str] = set()
        self.metrics: dict = {}
        self._metrics_at = 0.0

    # -- reading the log -------------------------------------------------------------

    def poll(self) -> None:
        if not self.path.exists():
            return
        with open(self.path, encoding="utf-8") as f:
            f.seek(self._offset)
            chunk = f.read()
            self._offset = f.tell()
        if not chunk:
            return
        text = self._partial + chunk
        lines = text.split("\n")
        self._partial = lines.pop()  # an incomplete last line waits for the next poll
        for line in lines:
            if not line.strip():
                continue
            try:
                e = json.loads(line)
            except json.JSONDecodeError:
                continue
            self.events.append(e)
            self._apply(e)
        if time.monotonic() - self._metrics_at > METRICS_EVERY_S:
            self.metrics = compute(self.events)
            self._metrics_at = time.monotonic()

    def _say(self, e: dict, kind: str, text: str, level: str = "info") -> None:
        self.feed.append({"t": e.get("t", 0), "time": _mmss(e.get("t", 0)), "node": e.get("node"),
                          "kind": kind, "level": level, "text": text})

    def _apply(self, e: dict) -> None:
        ev, node = e["ev"], e.get("node")
        if ev == "drone_state":
            self.drones[node] = {k: e.get(k) for k in ("x", "y", "state", "sector", "outbox", "station_link", "known_victims",
                                                       "battery", "battery_pct", "job_kind")}
            self.drones[node]["t"] = e["t"]
            trail = self.trails.setdefault(node, deque(maxlen=60))
            if not trail or abs(trail[-1][0] - e["x"]) + abs(trail[-1][1] - e["y"]) > 15:
                trail.append((round(e["x"]), round(e["y"])))
            self.drones[node]["trail"] = list(trail)
            self.drones[node]["rect"] = self.job_rects.get(e.get("sector"))
        elif ev == "node_stats":
            now = time.monotonic()
            for src, counts in (e.get("links") or {}).items():
                total = sum(counts.values())
                if total:
                    self.links[(src, node)] = {"rate": counts.get("ok", 0) / total, "n": total, "at": now,
                                               "reasons": counts}
        elif ev == "gs_status":
            self.stations.setdefault(node, {}).update(
                queue=e.get("queue"), clusters=e.get("clusters"), suspected=e.get("suspected", []),
                pool=e.get("pool"), covered=e.get("covered_cells"), total=e.get("total_cells"),
                consensus=e.get("consensus"), provisional=e.get("provisional", 0))
        elif ev == "sector_assigned" and e.get("rect"):
            self.job_rects[e["sector"]] = e["rect"]
        elif ev == "consensus_role":
            st = self.stations.setdefault(node, {})
            cons = dict(st.get("consensus") or {})
            cons.update(role=e["role"], term=e["term"], leader=e.get("leader"), kind=e.get("kind"))
            st["consensus"] = cons
        elif ev == "cluster_snapshot":
            self.clusters = {cid: {"x": c.get("x"), "y": c.get("y"), "status": c["status"], "count": len(c["members"])}
                             for cid, c in e["clusters"].items()}
        elif ev == "detection":
            self.truth_by_det[(e["drone"], e["req"], e["det"])] = e["truth"]
            if isinstance(e["truth"], str) and e["truth"].startswith("v"):
                self.detected_victims.add(e["truth"])
        elif ev == "sighting_sent":
            self.det_by_sighting[e["sighting_id"]] = (node, e["req"], e["det"])
        # -- feed ------------------------------------------------------------------
        if ev == "fault_start":
            what = e.get("nodes") or (f"zone at ({e['zone']['x']:.0f}, {e['zone']['y']:.0f}) r={e['zone']['radius_m']:.0f} m"
                                      if e.get("zone") else "")
            until = "until cleared" if e.get("end") is None else f"for {(e['end'] - e['start']) / 60:.1f} min"
            self._say(e, "fault", f"Fault: {e['kind']} {what} {until}", "warn")
        elif ev == "fault_end":
            self._say(e, "fault", f"Fault ended: {e['kind']} {e.get('nodes') or ''}")
        elif ev == "clock_changed":
            self._say(e, "fault", "Mission paused" if e.get("paused") else f"Mission running at x{e['speed']:g}",
                      "warn" if e.get("paused") else "info")
        elif ev == "loss_changed":
            self._say(e, "fault", f"Radio loss set to {100 * e['loss']:.0f}%", "warn")
        elif ev == "node_killed":
            self._say(e, "fault", f"{e['target']} killed ({e.get('reason', 'user')})", "bad")
        elif ev == "node_restarted":
            self._say(e, "fault", f"{e['target']} restarted (incarnation {e['incarnation']})", "warn")
        elif ev == "node_suspected":
            self._say(e, "membership", f"{node} lost contact with {e['peer']}", "bad")
        elif ev == "node_recovered":
            self._say(e, "membership", f"{node} regained contact with {e['peer']}", "good")
        elif ev == "failsafe_seek_link":
            self._say(e, "membership", f"{node} cannot reach its station; flying toward base", "warn")
        elif ev == "sector_assigned":
            what = ("inspect job" if e.get("kind") == "inspect" else
                    f"re-search (pass {e['pass_'] + 1}) of" if e.get("pass_") else
                    "leftover of" if e.get("start") else "sector")
            self._say(e, "tasks", f"{node} assigned {what} {e['sector']} to {e['drone']} "
                                  f"(lease token {e.get('token')}, {e.get('cells', '?')} cells)")
        elif ev == "sector_done":
            self._say(e, "tasks", f"{e['drone']} finished sector {e['sector']}", "good")
        elif ev == "landed":
            self._say(e, "tasks", f"{node} landed at base")
        elif ev == "bingo":
            self._say(e, "tasks", f"{node} battery low: gives back sector {e['sector']} from cell {e['next_cell']}, "
                                  f"flying home", "warn")
        elif ev == "sector_released":
            self._say(e, "tasks", f"Sector {e['sector']} back in the pool: {e['left']} cells left by {e['drone']}")
        elif ev == "lease_expired":
            self._say(e, "tasks", f"Lease on sector {e['sector']} ({e['drone']}) ran out: {e['left']} cells back "
                                  f"in the pool", "warn")
        elif ev == "lost_aircraft":
            self._say(e, "tasks", f"LOST AIRCRAFT: {e['drone']} silent past its battery limit", "bad")
        elif ev == "lease_revoked":
            self._say(e, "tasks", f"{node} revoked {e['drone']}'s old lease on sector {e['sector']}: {e['why']}", "warn")
        elif ev == "completion_stale":
            self._say(e, "tasks", f"Late completion of sector {e['sector']} by {e['drone']} refused (old token)", "warn")
        elif ev == "inspect_job_created":
            self._say(e, "tasks", f"Closer look requested at cluster {e['cluster']} (job {e['sector']})")
        elif ev == "swap_granted":
            self._say(e, "tasks", f"{e['drone']} at the swap point (waited {e['waited_s']:.0f} s, "
                                  f"{e['queue']} still queued)")
        elif ev == "command_applied":
            a = e.get("args") or {}
            text = {"transfer": f"{e['by']} offers command to {a.get('to')}",
                    "accept": f"{e['by']} accepted command" + (" (acting IC)" if e.get("acting") else ""),
                    "decline": f"{e['by']} declined command",
                    "succession": f"IC {a.get('absent')} silent: command offered to {a.get('to')}",
                    "zone": f"IC {e['by']} set priority zone at ({a.get('x')}, {a.get('y')}) r={a.get('radius_m')} m x{a.get('weight')}",
                    "clear_zone": f"IC {e['by']} cleared zone {a.get('id')}",
                    "ack": f"IC {e['by']} acknowledged {a.get('alert')}",
                    "resolve": f"IC {e['by']} settled conflict on {a.get('cluster')}: {a.get('decision')}",
                    }.get(e["action"], f"{e['by']} {e['action']}")
            self._say(e, "command", text, "good" if e["action"] in ("accept", "ack", "resolve") else "info")
        elif ev == "command_refused":
            self._say(e, "command", f"Refused: {e['by']} {e['action']} - {e['reason']}", "warn")
        elif ev == "battery_depleted":
            self._say(e, "tasks", f"{node} ran out of battery in the air", "bad")
        elif ev == "sighting_received":
            extra = " (joined a confirmed victim, no review needed)" if e.get("auto_attached") else ""
            via = " via relay" if e.get("relayed") else ""
            self._say(e, "review", f"{node} got sighting {e['sighting_id']}{via} -> cluster {e['cluster']}{extra}")
        elif ev == "verdict_committed":
            truths = {self.truth_by_det.get(self.det_by_sighting.get(s, ())) for s in e["members"]}
            real = {t for t in truths if isinstance(t, str) and t.startswith("v")}
            if e["decision"] == "confirmed":
                self.confirmed_victims |= real
            verdict = "correct" if (e["decision"] == "confirmed") == bool(real) else "WRONG"
            self._say(e, "review", f"{e['operator']} {e['decision']} cluster {e['cluster']} "
                                   f"({len(e['members'])} sightings) - truth: {verdict}",
                      "good" if verdict == "correct" else "bad")
        elif ev == "verdict_refused":
            self._say(e, "review", f"Verdict by {e['operator']} on {e['cluster']} refused: {e['reason']}", "warn")
        elif ev == "claim_expired":
            self._say(e, "review", f"{e['operator']}'s claim on {e['cluster']} expired", "warn")
        elif ev == "consensus_role":
            if e["role"] == "leader":
                self._say(e, "replication", f"{node} became leader (term {e['term']}, {e.get('kind')})", "good")
            elif e["role"] == "candidate":
                self._say(e, "replication", f"{node} started an election for term {e['term']}")
            else:
                who = f"follows {e['leader']}" if e.get("leader") else "has no leader"
                self._say(e, "replication", f"{node} is a follower in term {e['term']} and {who}")
        elif ev == "consensus_lost_quorum":
            self._say(e, "replication", f"{node} lost its majority and stepped down", "warn")
        elif ev == "consensus_takeover":
            self._say(e, "replication", f"{node} took over as primary (epoch {e['term']}) from {e.get('previous')}", "warn")
        elif ev == "consensus_committed_lost":
            self._say(e, "replication", f"{node} lost {e['count']} committed entries (overwritten by a newer primary)", "bad")
        elif ev == "orphans_resubmitted":
            self._say(e, "replication", f"{node} re-submits {e['count']} discarded decisions for merging", "warn")
        elif ev == "decision_provisional":
            self._say(e, "replication", f"{node}: no leader reachable, {e['operator']}'s verdict on {e['cluster']} saved provisionally", "warn")
        elif ev == "provisional_merged":
            self._say(e, "replication", f"{node} merged a provisional {e['op']} into the log"
                      + (f" ({e['conflicts']} conflict)" if e.get("conflicts") else ""), "good" if not e.get("conflicts") else "bad")
        elif ev == "verdict_conflict":
            self._say(e, "replication", f"Conflict on cluster {e['cluster']}: decided both ways; confirm wins, flagged", "bad")
        elif ev == "stale_assignment_ignored":
            self._say(e, "replication", f"{node} ignored an assignment from old epoch {e['epoch']} (current {e['current']})", "warn")
        elif ev == "plan_ready" and e.get("recovered_commit"):
            self._say(e, "replication", f"{node} recovered from its log: {e['recovered_commit']} committed entries, "
                                        f"{e.get('recovered_sightings', 0)} sightings", "good")
        elif ev in ("task_error", "handler_error"):
            self._say(e, "error", f"{node}: {ev} - {str(e.get('error', ''))[-160:]}", "bad")

    # -- view -------------------------------------------------------------------------

    def snapshot(self) -> dict:
        now = time.monotonic()
        links = [
            {"src": s, "dst": d, "rate": round(v["rate"], 2), "n": v["n"], "reasons": v["reasons"]}
            for (s, d), v in self.links.items() if now - v["at"] <= LINK_TTL_S
        ]
        m = self.metrics
        keep = ("victims", "victims_detected", "victims_confirmed", "operator_reviews", "duplicate_reviews_of_a_victim",
                "false_confirmations", "real_victims_rejected", "sightings_lost_in_transit", "delivery_delay_s_median",
                "delivery_delay_s_p95", "retransmissions", "packets_relayed", "queue_wait_s_median", "coverage_pct",
                "sectors_done", "errors_logged", "minutes_to_50pct_detected", "research_jobs", "bingo_returns",
                "leases_expired", "lost_aircraft", "swap_wait_s_max", "minutes_with_no_drone_searching")
        return {
            "drones": self.drones,
            "stations": self.stations,
            "links": links,
            "clusters": self.clusters,
            "victims_confirmed": sorted(self.confirmed_victims),
            "victims_detected": sorted(self.detected_victims),
            "feed": list(self.feed)[-150:],
            "metrics": {k: m.get(k) for k in keep},
            "events": len(self.events),
        }
