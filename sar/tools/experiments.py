"""Experiment runner (step 8): every design choice as an A/B test over several seeds.

    python -m sar.tools.experiments                 # all experiments
    python -m sar.tools.experiments tasking launch  # some of them
    python -m sar.tools.experiments --seeds 5 --parallel 4 --speed 50
    python -m sar.tools.experiments --report-only   # tables and charts from finished runs

Each run is a normal launcher run (its own processes, ports and event log) in
results/experiments/<experiment>/runs/<variant>_s<seed>/. The scenario's mission
length is kept; only the clock runs faster (--speed), so an 83-minute mission
takes about 100 real seconds at x50. Seeds change the world (terrain, victims)
and every random roll, so a difference that holds across seeds is not luck.

Outputs per experiment: runs.csv (one row per run), summary.md (mean and range
per variant) and charts (PNG). results/experiments/README.md collects them.
"""
from __future__ import annotations

import argparse
import csv
import json
import statistics
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import yaml

from ..common.config import load_scenario
from .report import _is_victim, compute, load_events

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / "results" / "experiments"

# Each experiment: the question, a scenario, variants (overrides), the numbers that answer it.
EXPERIMENTS: dict[str, dict] = {
    "messaging": {
        "title": "Acknowledgements and retries under radio faults",
        "question": "Do acks, retries and store-and-forward stop sightings from being lost when links fail?",
        "scenario": "scenarios/step2.yaml",
        "variants": {"no acks": {"messaging.reliable": False}, "acks + retries": {"messaging.reliable": True}},
        "metrics": ["sightings_reported", "sightings_lost_in_transit", "delivery_delay_s_p95", "retransmissions",
                    "coverage_pct", "victims_confirmed"],
        "headline": ["sightings_lost_in_transit", "coverage_pct"],
    },
    "radio_loss": {
        "title": "Reliable messaging as the radio gets worse",
        "question": "How do delay and losses grow with base packet loss (acks on)?",
        "scenario": "scenarios/step2.yaml",
        "variants": {"2% loss": {"radio.loss": 0.02}, "15% loss": {"radio.loss": 0.15}, "30% loss": {"radio.loss": 0.30}},
        "metrics": ["sightings_lost_in_transit", "delivery_delay_s_median", "delivery_delay_s_p95", "retransmissions",
                    "packets_relayed", "victims_confirmed"],
        "headline": ["delivery_delay_s_p95", "retransmissions"],
    },
    "dedup": {
        "title": "Deduplication of sightings",
        "question": "How much operator work does clustering save, and does it ever merge two different people?",
        "scenario": "scenarios/step3.yaml",
        # Drone-side suppression off, so the comparison is about the station's clustering alone.
        "common": {"known_victims.suppress": False},
        "variants": {"none": {"dedup.kind": "none"}, "position": {"dedup.kind": "radius"},
                     "position + signature": {"dedup.kind": "radius_signature"}},
        "metrics": ["operator_reviews", "duplicate_reviews_of_a_victim", "repeat_reviews_of_a_decoy", "wrong_merges",
                    "queue_wait_s_median", "victims_confirmed"],
        "headline": ["operator_reviews", "duplicate_reviews_of_a_victim", "wrong_merges"],
    },
    "replication": {
        "title": "Ground-station replication: Raft vs primary-backup",
        "question": "Under the same station faults, which keeps one leader and never loses a committed decision?",
        "scenario": "scenarios/step4.yaml",
        "variants": {"raft (3 stations)": {"consensus.kind": "raft", "ground_stations.count": 3},
                     "primary-backup (2)": {"consensus.kind": "primary_backup", "ground_stations.count": 2}},
        "metrics": ["mission_s_with_two_leaders", "mission_s_without_leader", "committed_entries_lost",
                    "verdict_conflicts", "replica_checkpoint_mismatches", "commit_latency_ms_median", "victims_confirmed"],
        "headline": ["mission_s_with_two_leaders", "committed_entries_lost"],
    },
    "tasking": {
        "title": "Where to search next",
        "question": "Does POA x POD tasking find people sooner than covering the map once, or a fixed grid?",
        "scenario": "scenarios/step5.yaml",
        "variants": {"greedy (POA x POD)": {"planner.strategy": "greedy"},
                     "sweep first": {"planner.strategy": "sweep_first"},
                     "fixed grid": {"planner.kind": "grid", "planner.strategy": "static"}},
        "metrics": ["victims_detected", "minutes_to_50pct_detected", "minutes_to_80pct_detected", "coverage_pct",
                    "research_jobs", "victims_confirmed"],
        "headline": ["victims_detected", "coverage_pct"],
        "curve": True,
    },
    "tasking_large": {
        "title": "Where to search next, on a map too big to cover",
        "question": "When four drones cannot cover the area in time, does searching the likely places first find more people?",
        "scenario": "scenarios/step5.yaml",
        # 1.5 km x 1.5 km (2.25 x the area), same victim and decoy density, base at the middle of the south edge.
        "common": {"world.width_cells": 75, "world.height_cells": 75, "world.victims": 56, "world.decoys": 68,
                   "base.x": 750},
        "variants": {"greedy (POA x POD)": {"planner.strategy": "greedy"},
                     "sweep first": {"planner.strategy": "sweep_first"},
                     "fixed grid": {"planner.kind": "grid", "planner.strategy": "static"}},
        "metrics": ["victims_detected", "minutes_to_50pct_detected", "coverage_pct", "research_jobs",
                    "victims_confirmed"],
        "headline": ["victims_detected", "coverage_pct"],
        "curve": True,
    },
    "launch": {
        "title": "Battery swaps: everyone at once vs staggered launch",
        "question": "Does staggering take-offs remove the swap queue and the gaps with nobody searching?",
        "scenario": "scenarios/step6.yaml",
        "variants": {"naive": {"drones.launch": "naive"}, "staggered": {"drones.launch": "staggered"}},
        "metrics": ["swap_wait_s_max", "swap_queue_peak", "minutes_with_no_drone_searching", "drone_minutes_searching",
                    "victims_detected", "victims_confirmed"],
        "headline": ["swap_wait_s_max", "minutes_with_no_drone_searching"],
        "curve": True,
    },
}

LABELS = {
    "sightings_reported": "Sightings sent", "sightings_lost_in_transit": "Sightings never delivered",
    "delivery_delay_s_median": "Delivery delay, median (s)", "delivery_delay_s_p95": "Delivery delay, 95th pct (s)",
    "retransmissions": "Retransmissions", "packets_relayed": "Packets relayed", "coverage_pct": "Coverage (%)",
    "victims_confirmed": "Victims confirmed", "victims_detected": "Victims detected",
    "operator_reviews": "Operator reviews", "duplicate_reviews_of_a_victim": "Same victim reviewed again",
    "repeat_reviews_of_a_decoy": "Same decoy reviewed again", "wrong_merges": "Two people merged into one",
    "queue_wait_s_median": "Review queue wait, median (s)",
    "mission_s_with_two_leaders": "Time with two leaders (mission s)",
    "mission_s_without_leader": "Time without a leader (mission s)",
    "committed_entries_lost": "Committed decisions overwritten", "verdict_conflicts": "Verdict conflicts",
    "replica_checkpoint_mismatches": "Replicas disagreeing at the end", "commit_latency_ms_median": "Commit latency (ms)",
    "minutes_to_50pct_detected": "Minutes to detect half", "minutes_to_80pct_detected": "Minutes to detect 80%",
    "research_jobs": "Re-search jobs", "swap_wait_s_max": "Longest swap wait (s)", "swap_queue_peak": "Most drones queued",
    "minutes_with_no_drone_searching": "Minutes with no drone searching",
    "drone_minutes_searching": "Drone-minutes searching",
}

# Light-surface categorical slots 1-3 (validated together for any pairing; see the charts' README).
COLORS = ["#2a78d6", "#eb6834", "#1baf7a"]


def slug(name: str) -> str:
    return "".join(c if c.isalnum() else "-" for c in name.lower()).strip("-").replace("--", "-")


# -- running ----------------------------------------------------------------------------------

def plan(names: list[str], seeds: list[int], speed: float) -> list[dict]:
    jobs = []
    for exp in names:
        e = EXPERIMENTS[exp]
        sc = load_scenario(ROOT / e["scenario"])
        mission_s = float(sc["duration_s"]) * float(sc["speedup"])
        for variant, overrides in e["variants"].items():
            for seed in seeds:
                sets = {**e.get("common", {}), **overrides, "seed": seed, "speedup": speed,
                        "duration_s": round(mission_s / speed, 1), "ui.enabled": False}
                jobs.append({"exp": exp, "variant": variant, "seed": seed, "sets": sets,
                             "dir": OUT / exp / "runs" / f"{slug(variant)}_s{seed}", "scenario": e["scenario"]})
    return jobs


def run_one(job: dict, slot: int) -> dict:
    d: Path = job["dir"]
    if (d / "summary.json").exists():
        return {**job, "status": "cached"}
    d.parent.mkdir(parents=True, exist_ok=True)
    args = [sys.executable, "-m", "sar.launcher.run", job["scenario"], "--no-control", "--run-dir", str(d),
            "--set", f"base_port={6000 + 100 * slot}"]
    for k, v in job["sets"].items():
        args += ["--set", f"{k}={yaml.safe_dump(v, default_flow_style=True).strip().removesuffix('...').strip()}"]
    t0 = time.time()
    with open(d.parent / f"{d.name}.log", "w", encoding="utf-8") as log:
        code = subprocess.call(args, cwd=ROOT, stdout=log, stderr=subprocess.STDOUT)
    return {**job, "status": "ok" if code == 0 and (d / "summary.json").exists() else f"failed ({code})",
            "real_s": round(time.time() - t0)}


def run_all(jobs: list[dict], parallel: int) -> None:
    free = list(range(parallel))
    done = 0

    def worker(job):
        slot = free.pop()
        try:
            return run_one(job, slot)
        finally:
            free.append(slot)

    with ThreadPoolExecutor(parallel) as pool:
        for res in pool.map(worker, jobs):
            done += 1
            print(f"[{done}/{len(jobs)}] {res['exp']:12} {res['variant']:22} seed {res['seed']}: {res['status']}"
                  + (f" in {res['real_s']} s" if "real_s" in res else ""), flush=True)


# -- analysis -------------------------------------------------------------------------------------

def found_curves(events: list[dict], step_min: float = 2.0) -> tuple[list[float], list[int], list[int]]:
    """Victims detected and confirmed by each mission minute (ground truth)."""
    det_t: dict[str, float] = {}
    truth_by_det, det_by_sid = {}, {}
    confirm_t: dict[str, float] = {}
    end = 0.0
    for e in events:
        end = max(end, e.get("t", 0))
        ev = e["ev"]
        if ev == "detection":
            truth_by_det[(e["drone"], e["req"], e["det"])] = e["truth"]
            if _is_victim(e["truth"]):
                det_t.setdefault(e["truth"], e["t"])
        elif ev == "sighting_sent":
            det_by_sid[e["sighting_id"]] = (e["node"], e["req"], e["det"])
        elif ev == "verdict_committed" and e["decision"] == "confirmed":
            for s in e["members"]:
                t = truth_by_det.get(det_by_sid.get(s))
                if _is_victim(t):
                    confirm_t.setdefault(t, e["t"])
    minutes = [k * step_min for k in range(int(end / 60 / step_min) + 1)]
    det = [sum(1 for t in det_t.values() if t <= m * 60) for m in minutes]
    conf = [sum(1 for t in confirm_t.values() if t <= m * 60) for m in minutes]
    return minutes, det, conf


def collect(exp: str) -> list[dict]:
    rows = []
    e = EXPERIMENTS[exp]
    for variant in e["variants"]:
        for d in sorted((OUT / exp / "runs").glob(f"{slug(variant)}_s*")):
            if not (d / "summary.json").exists():  # finished runs only
                continue
            events = load_events(d)
            stats = compute(events)
            row = {"variant": variant, "seed": int(d.name.rsplit("_s", 1)[1])}
            row.update({k: v for k, v in stats.items() if isinstance(v, (int, float)) or v is None})
            if e.get("curve"):
                row["_curve"] = found_curves(events)
            rows.append(row)
    return rows


def describe(values: list) -> str:
    vals = [v for v in values if isinstance(v, (int, float))]
    if not vals:
        return "-"
    mean = statistics.fmean(vals)
    fmt = (lambda x: f"{x:.0f}") if max(abs(v) for v in vals) >= 100 else (lambda x: f"{x:.1f}")
    if len(vals) == 1 or min(vals) == max(vals):
        return fmt(mean)
    return f"{fmt(mean)} ({fmt(min(vals))}-{fmt(max(vals))})"


def write_tables(exp: str, rows: list[dict]) -> str:
    e = EXPERIMENTS[exp]
    d = OUT / exp
    keys = sorted({k for r in rows for k in r if not k.startswith("_")} - {"variant", "seed"})
    with open(d / "runs.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["variant", "seed", *keys])
        for r in rows:
            w.writerow([r["variant"], r["seed"], *[r.get(k) for k in keys]])
    seeds = sorted({r["seed"] for r in rows})
    lines = [f"## {e['title']}", "", f"**Question.** {e['question']}", "",
             f"Scenario `{e['scenario']}`, seeds {', '.join(map(str, seeds))}. Mean over seeds, range in brackets.", "",
             "| Measure | " + " | ".join(e["variants"]) + " |", "|---|" + "---|" * len(e["variants"])]
    for m in e["metrics"]:
        cells = [describe([r.get(m) for r in rows if r["variant"] == v]) for v in e["variants"]]
        lines.append(f"| {LABELS.get(m, m)} | " + " | ".join(cells) + " |")
    lines += ["", f"![{e['title']}]({exp}/chart.png)", ""]
    if e.get("curve"):
        lines += [f"![Victims found over time]({exp}/curve.png)", ""]
    text = "\n".join(lines)
    (d / "summary.md").write_text(text.replace(f"]({exp}/", "]("), encoding="utf-8")
    return text


def charts(exp: str, rows: list[dict]) -> None:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    e = EXPERIMENTS[exp]
    variants = list(e["variants"])
    plt.rcParams.update({"font.family": "sans-serif", "font.size": 10, "axes.spines.top": False,
                         "axes.spines.right": False, "axes.edgecolor": "#b9b8b2", "axes.labelcolor": "#52514e",
                         "xtick.color": "#52514e", "ytick.color": "#52514e", "axes.titlesize": 11,
                         "axes.titleweight": "bold", "figure.facecolor": "white", "axes.facecolor": "white"})
    heads = e["headline"]
    fig, axes = plt.subplots(1, len(heads), figsize=(3.6 * len(heads) + 0.6, 3.6), squeeze=False)
    for ax, m in zip(axes[0], heads):
        for k, v in enumerate(variants):
            vals = [r.get(m) for r in rows if r["variant"] == v and isinstance(r.get(m), (int, float))]
            if not vals:
                continue
            mean = statistics.fmean(vals)
            ax.bar(k, mean, width=0.62, color=COLORS[k % len(COLORS)], zorder=2)
            ax.scatter([k] * len(vals), vals, s=14, color="#1f1f1d", zorder=3, linewidths=0)  # each seed
            ax.annotate(f"{mean:.0f}" if mean >= 10 else f"{mean:.1f}", (k, max(vals + [mean])), xytext=(0, 7),
                        textcoords="offset points", ha="center", va="bottom", fontsize=9, color="#0b0b0b", zorder=4)
        ax.set_xticks(range(len(variants)), [v.replace(" (", "\n(") for v in variants], fontsize=8.5)
        ax.set_title(LABELS.get(m, m), loc="left")
        ax.grid(axis="y", color="#ecebe7", zorder=0)
        ax.set_ylim(0, ax.get_ylim()[1] * 1.1)  # room for the value labels
    fig.suptitle(e["title"], x=0.01, ha="left", fontsize=12, fontweight="bold")
    fig.text(0.01, 0.005, "Bars: mean over seeds. Dots: individual seeds.", fontsize=8, color="#52514e")
    fig.tight_layout(rect=(0, 0.03, 1, 0.97))
    fig.savefig(OUT / exp / "chart.png", dpi=160)
    plt.close(fig)

    if e.get("curve"):
        fig, ax = plt.subplots(figsize=(6.4, 3.8))
        for k, v in enumerate(variants):
            curves = [r["_curve"] for r in rows if r["variant"] == v and r.get("_curve")]
            if not curves:
                continue
            n = min(len(c[0]) for c in curves)
            minutes = curves[0][0][:n]
            mean = [statistics.fmean(c[1][i] for c in curves) for i in range(n)]
            ax.plot(minutes, mean, color=COLORS[k % len(COLORS)], linewidth=2, label=v, drawstyle="steps-post")
        ax.set_xlabel("Mission minutes")
        ax.set_ylabel("Victims detected (mean over seeds)")
        ax.set_title("Victims detected over time", loc="left")
        ax.grid(color="#ecebe7")
        ax.legend(frameon=False, fontsize=9, loc="lower right")
        ax.set_ylim(bottom=0)
        fig.tight_layout()
        fig.savefig(OUT / exp / "curve.png", dpi=160)
        plt.close(fig)


def report(names: list[str]) -> None:
    parts = ["# Experiment results", "",
             "Generated by `python -m sar.tools.experiments`. Each number is measured against ground truth from the "
             "world server's event log. Per-run data: `<experiment>/runs.csv`; the runs themselves: "
             "`<experiment>/runs/`.", ""]
    for exp in EXPERIMENTS:
        if not (OUT / exp / "runs").exists():
            continue
        rows = collect(exp)
        if not rows:
            continue
        parts.append(write_tables(exp, rows))
        charts(exp, rows)
        print(f"report: {exp} ({len(rows)} runs)", flush=True)
    (OUT / "README.md").write_text("\n".join(parts), encoding="utf-8")
    print(f"written: {OUT / 'README.md'}", flush=True)


def main() -> None:
    p = argparse.ArgumentParser(description="Run the SAR experiments")
    p.add_argument("experiments", nargs="*", help=f"some of: {', '.join(EXPERIMENTS)} (default: all)")
    p.add_argument("--seeds", type=int, default=3)
    p.add_argument("--first-seed", type=int, default=42)
    p.add_argument("--parallel", type=int, default=4)
    p.add_argument("--speed", type=float, default=50.0, help="mission seconds per real second")
    p.add_argument("--report-only", action="store_true")
    a = p.parse_args()
    names = a.experiments or list(EXPERIMENTS)
    unknown = [n for n in names if n not in EXPERIMENTS]
    if unknown:
        raise SystemExit(f"unknown experiment(s): {unknown}")
    if not a.report_only:
        jobs = plan(names, list(range(a.first_seed, a.first_seed + a.seeds)), a.speed)
        print(f"{len(jobs)} runs, {a.parallel} at a time, clock x{a.speed:g}", flush=True)
        run_all(jobs, a.parallel)
    report(names)


if __name__ == "__main__":
    main()
