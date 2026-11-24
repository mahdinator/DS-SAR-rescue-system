"""Scenario files and the per-run runtime description."""
from __future__ import annotations

import copy
import json
from dataclasses import asdict, dataclass
from pathlib import Path

import yaml

# Terrain parameters. POA = starting weight for "a victim is here",
# POD = probability one search pass detects a victim that is here,
# PFA = probability of a false alarm per pass, sigma = position error (m).
# These are scenario settings, not measured values.
# Default PFA averages about 1.1% per pass over the default terrain mix;
# scenarios/step1_high_fa.yaml doubles it as an operator-overload stress test.
DEFAULT_TERRAIN = {
    "rubble": {"poa": 1.0, "pod": 0.40, "pfa": 0.015, "sigma": 6.0},
    "damaged_building": {"poa": 0.8, "pod": 0.30, "pfa": 0.015, "sigma": 8.0},
    "road": {"poa": 0.6, "pod": 0.85, "pfa": 0.005, "sigma": 4.0},
    "forest": {"poa": 0.4, "pod": 0.35, "pfa": 0.02, "sigma": 12.0},
    "open_field": {"poa": 0.3, "pod": 0.90, "pfa": 0.005, "sigma": 3.0},
    "water": {"poa": 0.2, "pod": 0.60, "pfa": 0.01, "sigma": 6.0},
}

DEFAULTS: dict = {
    "name": "scenario",
    "seed": 1,
    "duration_s": 120,  # real seconds the launcher lets the mission run
    "speedup": 20,  # mission seconds per real second
    "startup_delay_s": 4,  # real seconds between process start and mission start
    "base_port": 5550,
    "world": {
        "width_cells": 50,
        "height_cells": 50,
        "cell_m": 20.0,
        "regions": 18,
        "victims": 25,
        "signature_dims": 16,
        "signature_noise": 0.35,
        "transit_pod_factor": 0.5,  # detection rate multiplier in transit
        "transit_pfa_factor": 0.5,  # false-alarm rate multiplier in transit
        "decoys": 0,  # fixed look-alike objects that cause repeat false alarms
        "decoy_pod_factor": 0.6,  # decoy detection rate = terrain POD x this
        "inspect_pod_factor": 1.8,  # closer look (inspect job): POD x this, at most 0.98
        "terrain_mix": {
            "rubble": 0.12,
            "damaged_building": 0.15,
            "road": 0.15,
            "forest": 0.20,
            "open_field": 0.30,
            "water": 0.08,
        },
    },
    "terrain": DEFAULT_TERRAIN,
    "base": {"x": 500.0, "y": 10.0},
    "drones": {
        "count": 3,
        "cruise_mps": 12.0,
        "search_mps": 6.0,
        "telemetry_period_real_s": 0.5,
        "coverage_period_real_s": 1.0,
        "job_retry_real_s": 2.0,
        # Battery (step 5 basic model; step 6 adds pads and charging). Mission seconds.
        # A drone flies home when what is left = way home + reserve, and swaps its battery there.
        "battery": {"enabled": True, "endurance_s": 1200.0, "reserve_s": 120.0, "swap_s": 120.0},
        # Battery swaps happen one at a time at base, first come first served (step 6).
        # launch: naive (all take off together) | staggered (drone k waits k x swap_s first).
        "launch": "naive",
    },
    "ground_stations": {"count": 1},
    "operators": [
        {"id": "bot-1", "kind": "bot", "accuracy": 0.95, "delay_s": 30, "match_sigmas": 2.5}
    ],
    # Planning (architecture doc, section 11). kind: sortie (sized to one battery) | grid.
    # strategy: greedy | sweep_first | static (default: static for grid, greedy for sortie).
    "planner": {
        "kind": "sortie",
        "sector_cells": 10,  # grid planner: tile side in cells
        "max_cells": 200,  # sortie planner: largest job
        "fill": 0.9,  # sortie planner: fraction of the battery budget a job from base may use
        "strategy": None,
        "lease_grace_s": 60.0,  # lease deadline = assignment + battery left + this
        "min_search_s": 180.0,  # a job is offered if the battery allows at least this much searching on it
        "research": {"max_passes": 4, "stop_frac": 0.1},
        "boost": {"factor": 0.5, "radius_m": 80.0},  # near confirmed victims
        "leftover_boost": 0.5,  # unfinished work a drone gave back is worth (1 + this)
        "inspect": {"enabled": True, "value": 5.0, "hover_s": 20.0, "statuses": ["recheck", "conflict"]},
    },
    # Emulated radio (architecture doc, section 7.2). Delivery is certain below
    # r0_m, falls linearly to zero at rmax_m, and has a base loss on top. A link
    # uses the larger range of its two ends (a station antenna helps both ways).
    "radio": {
        "enabled": True,  # False = perfect links (step-1 behaviour)
        "loss": 0.02,
        "drone": {"r0_m": 300.0, "rmax_m": 600.0},
        "groundstation": {"r0_m": 500.0, "rmax_m": 900.0},
        "latency_ms": 20.0,  # real milliseconds
        "jitter_ms": 10.0,
        "bytes_per_ms": 250.0,  # about 2 Mbit/s
        "relay": True,
        "relay_ttl": 3,
        "relay_margin_m": 10.0,
    },
    "ground": {"latency_ms": 2.0},
    "messaging": {
        "reliable": True,  # False = no acks or retries (for comparison runs)
        "retry_real_s": 1.0,
        "retry_max_real_s": 8.0,
        "outbox_limit": 10000,
    },
    "failure_detector": {"kind": "timeout", "timeout_real_s": 3.0},
    # Deduplication (architecture doc, section 10): none | radius | radius_signature
    "dedup": {"kind": "radius_signature", "link_sigmas": 2.5, "signature_threshold": 0.7},
    "review": {"claim_lease_real_s": 30.0, "poll_real_s": 0.5},
    "known_victims": {"broadcast_real_s": 5.0, "suppress": True},
    # Ground-station replication (architecture doc, section 9): raft | primary_backup.
    # All times are real seconds (the ground network does not run on mission time).
    "consensus": {
        "kind": "raft",
        "heartbeat_real_s": 0.15,
        "election_timeout_real_s": [1.0, 2.0],
        "pb_takeover_real_s": 1.5,
        "commit_timeout_real_s": 2.0,
        "leader_timeout_real_s": 2.0,
    },
    "ui": {"enabled": False, "port": 8080},
    # Control center: fault injection and the observer dashboard, served by the
    # launcher. "auto" port = 8090 + (base_port - 5550) // 100, so parallel runs differ.
    "control": {"enabled": True, "port": "auto"},
    # Chain of command (step 7). Empty roster = no checks. roster: [{id, role}], role is
    # commander | analyst | observer. initial: first incident commander. succession: who is
    # offered command, in order, when the IC is silent for absence_timeout_real_s.
    # script: [{t, by, action, args}] actions a commander bot performs at mission time t.
    "command": {"roster": [], "initial": None, "succession": [], "absence_timeout_real_s": 5.0, "script": []},
    "heartbeat_real_s": 0.5,
    "faults": [],
}


def deep_merge(base: dict, override: dict) -> dict:
    out = copy.deepcopy(base)
    for key, value in (override or {}).items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = deep_merge(out[key], value)
        else:
            out[key] = copy.deepcopy(value)
    return out


def load_scenario(path: str | Path) -> dict:
    with open(path, encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    return deep_merge(DEFAULTS, data)


@dataclass
class Runtime:
    """Everything a node needs to start: written by the launcher per run."""

    run_dir: str
    mission_start: float
    scenario: dict
    nodes: dict  # node id -> {"role": str, "port": int, ...}
    world_addr: str
    logger_addr: str
    control_addr: str = ""  # launcher's PUB socket for live faults ("" = none)

    @property
    def speedup(self) -> float:
        return float(self.scenario["speedup"])

    @property
    def seed(self) -> int:
        return int(self.scenario["seed"])

    def ids(self, role: str) -> list[str]:
        return [nid for nid, n in self.nodes.items() if n["role"] == role]

    def save(self, path: str | Path) -> None:
        Path(path).write_text(json.dumps(asdict(self), indent=2), encoding="utf-8")

    @classmethod
    def load(cls, path: str | Path) -> "Runtime":
        return cls(**json.loads(Path(path).read_text(encoding="utf-8")))
