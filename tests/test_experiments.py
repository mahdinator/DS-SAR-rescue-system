"""Step 8: experiment runner helpers (no missions are run here)."""
from sar.tools.experiments import EXPERIMENTS, describe, found_curves, plan


def test_every_experiment_plans_one_run_per_variant_and_seed_with_the_same_mission_length():
    jobs = plan(list(EXPERIMENTS), [1, 2], speed=50)
    assert len(jobs) == 2 * sum(len(e["variants"]) for e in EXPERIMENTS.values())
    step5 = [j for j in jobs if j["exp"] == "tasking"][0]
    assert step5["sets"]["speedup"] == 50 and step5["sets"]["duration_s"] == 100.0  # 200 s at x25 = 5000 mission s
    assert len({j["dir"] for j in jobs}) == len(jobs)  # every run has its own folder


def test_describe_gives_mean_and_range():
    assert describe([1.0, 2.0, 3.0]) == "2.0 (1.0-3.0)"
    assert describe([5, 5]) == "5.0"
    assert describe([None]) == "-"


def test_found_curve_counts_each_victim_once_at_its_first_detection():
    ev = [{"ev": "detection", "t": 30, "drone": "d", "req": 1, "det": 0, "truth": "v1"},
          {"ev": "detection", "t": 200, "drone": "d", "req": 2, "det": 0, "truth": "v1"},
          {"ev": "detection", "t": 300, "drone": "d", "req": 3, "det": 0, "truth": "d4"},
          {"ev": "detection", "t": 250, "drone": "d", "req": 4, "det": 0, "truth": "v2"}]
    minutes, det, conf = found_curves(ev, step_min=2)
    assert minutes == [0, 2, 4] and det == [0, 1, 1] and conf == [0, 0, 0]  # v2 at 250 s is after minute 4
