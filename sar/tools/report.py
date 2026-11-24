"""Summarise a run from its event log, measured against ground truth.

    python -m sar.tools.report runs/<run directory>

Ground truth comes from the world server's `detection` events: each detection
records which object caused it (victim "v..", decoy "d..", or None for a one-off
false alarm). Sightings and clusters are mapped back to those objects.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path


def load_events(run_dir: Path) -> list[dict]:
    path = Path(run_dir) / "events.jsonl"
    if not path.exists():
        return []
    events = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    events.append(json.loads(line))
                except json.JSONDecodeError:
                    pass  # a line cut off by a hard stop
    return events


def _is_victim(truth) -> bool:
    return isinstance(truth, str) and truth.startswith("v")


def _is_decoy(truth) -> bool:
    return isinstance(truth, str) and truth.startswith("d")


def _pct(values: list[float], p: float):
    values = sorted(values)
    return round(values[min(len(values) - 1, int(p * len(values)))], 1) if values else None


def replication_metrics(events: list[dict]) -> dict:
    """Leaders over time, conflicts, lost commits and whether replicas agree."""
    stations = sorted({e["node"] for e in events if e["ev"] == "gs_status"})
    if len(stations) < 2:
        return {}
    ordered = sorted((e for e in events if e["ev"] in ("consensus_role", "node_killed", "node_restarted", "node_crashed")),
                     key=lambda e: e["t"])
    end_t = max((e["t"] for e in events), default=0.0)
    role = {s: "follower" for s in stations}
    up = {s: True for s in stations}
    leaders_now = lambda: [s for s in stations if up[s] and role[s] == "leader"]
    t_prev, none_s, multi_s, gaps, gap_start, changes = 0.0, 0.0, 0.0, [], None, 0
    started = False
    for e in ordered + [{"t": end_t, "ev": "_end", "node": ""}]:
        if started:
            n = len(leaders_now())
            dt = max(0.0, e["t"] - t_prev)
            if n == 0:
                none_s += dt
            elif n > 1:
                multi_s += dt
        t_prev = e["t"]
        if e["ev"] == "consensus_role":
            if e["role"] == "leader":
                changes += 1
                started = True
            role[e["node"]] = e["role"]
        elif e["ev"] in ("node_killed", "node_crashed") and e.get("target") in role:
            up[e["target"]] = False
        elif e["ev"] == "node_restarted" and e.get("target") in role:
            up[e["target"]], role[e["target"]] = True, "follower"
        if started:
            n = len(leaders_now())
            if n == 0 and gap_start is None:
                gap_start = e["t"]
            elif n > 0 and gap_start is not None:
                gaps.append(e["t"] - gap_start)
                gap_start = None
    last = {}
    for e in events:
        if e["ev"] == "gs_status":
            last[e["node"]] = e
    mismatches = 0
    cps = [l.get("checkpoints") or {} for l in last.values()]
    for a in range(len(cps)):
        for b in range(a + 1, len(cps)):
            mismatches += sum(1 for k in set(cps[a]) & set(cps[b]) if cps[a][k] != cps[b][k])
    lat = [e["latency_ms"] for e in events if e["ev"] == "cmd_committed"]
    count = lambda ev: sum(1 for e in events if e["ev"] == ev)
    total = lambda ev, k: sum(e.get(k, 0) for e in events if e["ev"] == ev)
    return {
        "consensus": next((l["consensus"]["kind"] for l in last.values() if l.get("consensus")), None),
        "stations": len(stations),
        "leader_elections": changes,
        "mission_s_without_leader": round(none_s, 1),
        "longest_gap_without_leader_s": round(max(gaps), 1) if gaps else 0.0,
        "mission_s_with_two_leaders": round(multi_s, 1),
        "commit_latency_ms_median": _pct(lat, 0.5),
        "commit_latency_ms_p95": _pct(lat, 0.95),
        "commit_timeouts": count("commit_timeout"),
        "decisions_made_provisionally": count("decision_provisional"),
        "orphaned_entries_resubmitted": total("orphans_resubmitted", "count"),
        "provisional_merged": count("provisional_merged"),
        "verdict_conflicts": count("verdict_conflict"),
        "committed_entries_lost": total("consensus_committed_lost", "count"),
        "replica_checkpoint_mismatches": mismatches,
        "stale_assignments_ignored_by_drones": count("stale_assignment_ignored"),
        "final_commit_index": {k: (v.get("consensus") or {}).get("commit") for k, v in sorted(last.items())},
    }


SEARCHING = ("search", "inspect")


def planning_metrics(events: list[dict], n_victims: int) -> dict:
    """Jobs, leases, battery and how much of the time drones were actually searching."""
    c = {k: 0 for k in ("jobs_assigned", "research_jobs", "leftover_jobs", "inspect_jobs", "bingo_returns",
                        "leases_expired", "lost_aircraft", "leases_revoked", "stale_completions",
                        "battery_swaps", "battery_depleted")}
    swap_waits, queue_peak = [], 0
    first_detect: dict[str, float] = {}
    states: dict[str, str] = {}
    last_t = None
    started = False
    no_search = search_time = 0.0
    planner = None
    for e in sorted(events, key=lambda e: e.get("t", 0)):
        ev = e["ev"]
        if ev == "plan_ready" and planner is None:
            planner = f"{e.get('planner')}/{e.get('strategy')} ({e.get('sectors')} jobs)"
        elif ev == "sector_assigned":
            c["jobs_assigned"] += 1
            c["research_jobs"] += bool(e.get("pass_"))
            c["leftover_jobs"] += bool(e.get("start"))
            c["inspect_jobs"] += e.get("kind") == "inspect"
        elif ev == "bingo":
            c["bingo_returns"] += 1
        elif ev == "lease_expired":
            c["leases_expired"] += 1
        elif ev == "lost_aircraft":
            c["lost_aircraft"] += 1
        elif ev == "lease_revoked":
            c["leases_revoked"] += 1
        elif ev == "completion_stale":
            c["stale_completions"] += 1
        elif ev == "battery_swapped":
            c["battery_swaps"] += 1
        elif ev == "battery_depleted":
            c["battery_depleted"] += 1
        elif ev == "swap_granted":
            swap_waits.append(e["waited_s"])
        elif ev == "swap_queue":
            queue_peak = max(queue_peak, e.get("position", 0))
        elif ev == "detection" and _is_victim(e.get("truth")):
            first_detect.setdefault(e["truth"], e["t"])
        elif ev == "drone_state":
            t = e["t"]
            if last_t is not None and started and t > last_t:
                busy = sum(1 for s in states.values() if s in SEARCHING)
                search_time += busy * (t - last_t)
                if busy == 0:
                    no_search += t - last_t
            states[e["node"]] = e.get("state")
            started = started or e.get("state") in SEARCHING
            last_t = t
        elif ev in ("node_killed", "node_crashed"):
            states.pop(e.get("target", e.get("node")), None)
    times = sorted(first_detect.values())

    def detected_by(frac):
        k = int(round(frac * n_victims + 0.4999))
        return round(times[k - 1] / 60, 1) if n_victims and 0 < k <= len(times) else None

    out = {"planner": planner, **c,
           "swap_wait_s_median": _pct(swap_waits, 0.5), "swap_wait_s_max": max(swap_waits) if swap_waits else None,
           "swap_queue_peak": queue_peak,
           "minutes_with_no_drone_searching": round(no_search / 60, 1),
           "drone_minutes_searching": round(search_time / 60, 1),
           "minutes_to_50pct_detected": detected_by(0.5),
           "minutes_to_80pct_detected": detected_by(0.8)}
    return out


def command_metrics(events: list[dict]) -> dict:
    """Chain of command: what was done, what was refused, how fast alerts were handled."""
    applied = [e for e in events if e["ev"] == "command_applied"]
    if not applied and not any(e["ev"] == "command_refused" for e in events):
        return {}
    raised = {e["alert"]: e["t"] for e in events if e["ev"] == "lost_aircraft" and e.get("alert")}
    acks = [e["t"] - raised[e["args"]["alert"]] for e in applied
            if e["action"] == "ack" and e["args"].get("alert") in raised]
    holders = [(round(e["t"] / 60, 1), e["by"], "acting" if e.get("acting") else "") for e in applied
               if e["action"] == "accept"]
    return {
        "command_actions": len(applied),
        "command_refused": sum(1 for e in events if e["ev"] == "command_refused"),
        "command_handovers": [f"{m} min {who} {kind}".strip() for m, who, kind in holders],
        "successions_offered": sum(1 for e in applied if e["action"] == "succession"),
        "priority_zones_set": sum(1 for e in applied if e["action"] == "zone"),
        "lost_aircraft_alerts": len(raised),
        "alerts_acknowledged": len(acks),
        "alert_ack_s_max": round(max(acks), 1) if acks else None,
        "conflicts_settled_by_ic": sum(1 for e in applied if e["action"] == "resolve"),
    }


def compute(events: list[dict]) -> dict:
    victims, decoys = [], []
    truth_by_det: dict[tuple, str | None] = {}
    det_by_sighting: dict[str, tuple] = {}
    verdicts: list[dict] = []
    claims_waited: list[float] = []
    suppressed: list[tuple[tuple, float]] = []
    received = auto_attached = refused = expired = 0
    received_ids: set[str] = set()
    last_status = last_snapshot = None
    errors = 0
    delays: list[float] = []
    relayed_sightings = retried_sightings = 0
    link_reasons: dict[str, int] = {}
    last_stats: dict[str, dict] = {}
    suspicions: list[str] = []
    seek_link = 0
    faults: list[str] = []
    for e in events:
        ev = e["ev"]
        if ev == "world_ready":
            victims, decoys = e["victims"], e.get("decoys", [])
        elif ev == "detection":
            truth_by_det[(e["drone"], e["req"], e["det"])] = e["truth"]
        elif ev == "sighting_sent":
            det_by_sighting[e["sighting_id"]] = (e["node"], e["req"], e["det"])
        elif ev == "sighting_suppressed":
            suppressed.append(((e["node"], e["req"], e["det"]), e["t"]))
        elif ev == "sighting_received":
            received += 1
            received_ids.add(e["sighting_id"])
            auto_attached += bool(e.get("auto_attached"))
            if "obs_t" in e:
                delays.append(e["t"] - e["obs_t"])
            relayed_sightings += bool(e.get("relayed"))
            retried_sightings += bool(e.get("attempt"))
        elif ev == "review_claimed":
            claims_waited.append(e["waited_s"])
        elif ev == "verdict_committed":
            verdicts.append(e)
        elif ev == "verdict_refused":
            refused += 1
        elif ev == "claim_expired":
            expired += 1
        elif ev == "cluster_snapshot":
            last_snapshot = e
        elif ev == "node_stats":
            last_stats[e["node"]] = e
            for counts in (e.get("links") or {}).values():
                for reason, n in counts.items():
                    link_reasons[reason] = link_reasons.get(reason, 0) + n
        elif ev == "node_suspected":
            suspicions.append(f"{e['node']} suspected {e['peer']} at {e['t'] / 60:.1f} min")
        elif ev == "failsafe_seek_link":
            seek_link += 1
        elif ev == "fault_start":
            faults.append(f"{e['kind']} {e.get('nodes') or e.get('zone')} at {e['start'] / 60:.1f} min")
        elif ev == "gs_status":
            last_status = e
        elif ev in ("task_error", "handler_error", "bad_message"):
            errors += 1

    def truth_of(sid: str):
        key = det_by_sighting.get(sid)
        return truth_by_det.get(key) if key else None

    # -- what the operators reviewed, against ground truth --------------------------
    first_confirm: dict[str, float] = {}
    reviewed: set[str] = set()
    false_confirmed = rejected_real = duplicate_reviews = repeat_decoy_reviews = one_off_reviews = 0
    for v in verdicts:
        if v.get("merged"):
            # A provisional or re-submitted decision: counts for confirmation, not as a new review.
            if v["decision"] == "confirmed":
                for t in {truth_of(s) for s in v["members"]}:
                    if _is_victim(t):
                        first_confirm.setdefault(t, v["t"])
            continue
        truths = {truth_of(s) for s in v["members"]}
        real = {t for t in truths if _is_victim(t)}
        objects = {t for t in truths if t}
        if v["decision"] == "confirmed":
            if not real:
                false_confirmed += 1
            for t in real:
                first_confirm.setdefault(t, v["t"])
        elif real:
            rejected_real += 1
        if real and real <= reviewed:
            duplicate_reviews += 1  # every victim in it had already been in front of an operator
        elif not real and objects and objects <= reviewed:
            repeat_decoy_reviews += 1  # the same decoy again
        elif not objects:
            one_off_reviews += 1
        reviewed |= objects

    wrong_merges = unreviewed = 0
    if last_snapshot:
        for c in last_snapshot["clusters"].values():
            if len({t for t in (truth_of(s) for s in c["members"]) if _is_victim(t)}) > 1:
                wrong_merges += 1  # two different people shown as one
            unreviewed += c["status"] in ("pending", "recheck")

    wrong_suppressions = 0
    for key, t in suppressed:
        truth = truth_by_det.get(key)
        if _is_victim(truth) and (truth not in first_confirm or first_confirm[truth] > t):
            wrong_suppressions += 1  # a drone kept quiet about a person not yet confirmed

    detected = {t for t in truth_by_det.values() if _is_victim(t)}
    sightings_real = sum(1 for sid in det_by_sighting if _is_victim(truth_of(sid)))
    sightings_decoy = sum(1 for sid in det_by_sighting if _is_decoy(truth_of(sid)))
    times = sorted(first_confirm.values())
    n = len(victims)

    def time_to(frac: float):
        k = int(round(frac * n + 0.4999))
        return round(times[k - 1] / 60, 1) if n and 0 < k <= len(times) else None

    pool = (last_status or {}).get("pool") or {}
    review = {
        "operator_reviews": sum(1 for v in verdicts if not v.get("merged")),
        "reviews_per_confirmed_victim": round(sum(1 for v in verdicts if not v.get("merged")) / len(first_confirm), 2)
        if first_confirm else None,
        "duplicate_reviews_of_a_victim": duplicate_reviews,
        "repeat_reviews_of_a_decoy": repeat_decoy_reviews,
        "reviews_of_one_off_false_alarms": one_off_reviews,
        "sightings_auto_attached": auto_attached,
        "sightings_suppressed_on_drones": len(suppressed),
        "wrong_suppressions": wrong_suppressions,
        "wrong_merges": wrong_merges,
        "verdicts_refused": refused,
        "claims_expired": expired,
        "queue_wait_s_median": _pct(claims_waited, 0.5),
        "queue_wait_s_p95": _pct(claims_waited, 0.95),
        "unreviewed_clusters_at_end": unreviewed,
    }
    network = {
        "sightings_lost_in_transit": len(set(det_by_sighting) - received_ids),
        "sightings_relayed": relayed_sightings,
        "sightings_needing_retry": retried_sightings,
        "delivery_delay_s_median": _pct(delays, 0.5),
        "delivery_delay_s_p95": _pct(delays, 0.95),
        "delivery_delay_s_max": round(max(delays), 1) if delays else None,
        "packets_relayed": sum(s.get("relayed", 0) for s in last_stats.values()),
        "retransmissions": sum(s.get("retries", 0) for s in last_stats.values()),
        "outbox_peak": max((s.get("outbox_max", 0) for s in last_stats.values()), default=0),
        "duplicates_suppressed": sum(s.get("duplicates", 0) for s in last_stats.values()),
        "link_outcomes": dict(sorted(link_reasons.items())),
        "failsafe_seek_link": seek_link,
        "suspicions": suspicions,
        "faults": faults,
    }
    return {
        "victims": n,
        "decoys": len(decoys),
        "victims_detected": len(detected),
        "victims_confirmed": len(first_confirm),
        "sightings_reported": len(det_by_sighting),
        "sightings_received_by_gs": len(received_ids),
        "sightings_of_real_victims": sightings_real,
        "sightings_of_decoys": sightings_decoy,
        "one_off_false_alarm_sightings": len(det_by_sighting) - sightings_real - sightings_decoy,
        "false_confirmations": false_confirmed,
        "real_victims_rejected": rejected_real,
        "minutes_to_first_confirm": round(times[0] / 60, 1) if times else None,
        "minutes_to_50pct_confirmed": time_to(0.5),
        "coverage_pct": round(100 * last_status["covered_cells"] / last_status["total_cells"], 1)
        if last_status and last_status["total_cells"] else None,
        "sectors_done": f"{pool.get('done')}/{pool.get('total')}" if pool else None,
        "errors_logged": errors,
        "events": len(events),
        **review,
        **planning_metrics(events, n),
        **command_metrics(events),
        **network,
        **replication_metrics(events),
    }


def summarize(run_dir) -> dict:
    stats = compute(load_events(Path(run_dir)))
    print("\nRun summary")
    width = max(len(k) for k in stats)
    for k, v in stats.items():
        if isinstance(v, list):
            print(f"  {k.ljust(width)}  {len(v)}")
            for item in v[:12]:
                print(f"  {''.ljust(width)}    - {item}")
            if len(v) > 12:
                print(f"  {''.ljust(width)}    ... {len(v) - 12} more")
        else:
            print(f"  {k.ljust(width)}  {v}")
    (Path(run_dir) / "summary.json").write_text(json.dumps(stats, indent=2), encoding="utf-8")
    return stats


if __name__ == "__main__":
    summarize(Path(sys.argv[1]))
