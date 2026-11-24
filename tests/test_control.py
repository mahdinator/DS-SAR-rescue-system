import json
import math

from sar.control.observer import Observer
from sar.transport.faults import Fault, FaultSchedule, fault_from_dict


def test_live_faults_replace_previous_snapshot():
    fs = FaultSchedule([{"t": 0, "duration": 100, "cut": ["a", "b"]}])
    live = Fault("isolate", 10, 50, nodes=["c"], id="f1")
    fs.set_dynamic([live.describe()])
    assert fs.blocks("c", "x", 20, "radio", None, None) == "isolate"
    assert fs.blocks("a", "b", 20, "radio", None, None) == "cut"  # scripted faults stay
    fs.set_dynamic([])  # the next snapshot no longer has it
    assert fs.blocks("c", "x", 20, "radio", None, None) is None


def test_fault_roundtrip_keeps_open_end():
    f = Fault("jam", 5, math.inf, zone={"x": 1, "y": 2, "radius_m": 3}, id="f9")
    back = fault_from_dict(json.loads(json.dumps(f.describe())))
    assert back == f


def test_observer_reads_log_incrementally(tmp_path):
    log = tmp_path / "events.jsonl"
    nodes = {"gs-1": {"role": "groundstation", "pos": [0, 0]}, "drone-1": {"role": "drone"}}
    obs = Observer(tmp_path, nodes)
    rows = [
        {"t": 1, "node": "drone-1", "ev": "drone_state", "x": 10, "y": 20, "state": "search", "sector": 3,
         "outbox": 0, "station_link": True, "known_victims": 0},
        {"t": 2, "node": "gs-1", "ev": "node_stats", "links": {"drone-1": {"ok": 3, "loss": 1}}},
        {"t": 3, "node": "launcher", "ev": "fault_start", "kind": "isolate", "start": 3, "end": None, "nodes": ["drone-1"]},
    ]
    with open(log, "w", encoding="utf-8") as f:
        f.write(json.dumps(rows[0]) + "\n" + json.dumps(rows[1]) + "\n" + json.dumps(rows[2])[:20])  # last line cut off
    obs.poll()
    assert obs.drones["drone-1"]["state"] == "search"
    assert obs.links[("drone-1", "gs-1")]["rate"] == 0.75
    assert not obs.feed  # the half-written line is not read yet
    with open(log, "a", encoding="utf-8") as f:
        f.write(json.dumps(rows[2])[20:] + "\n")
    obs.poll()
    assert len(obs.events) == 3 and "isolate" in obs.feed[-1]["text"]
    snap = obs.snapshot()
    assert snap["links"][0]["src"] == "drone-1"
