"""Run a scenario: start every node as its own process, serve the control center,
carry out scripted and live faults, stop everything, report.

    python -m sar.launcher.run scenarios/step2.yaml [--duration SECONDS]
                                                    [--set radio.relay=false ...]
                                                    [--name SUFFIX] [--no-control]

The launcher is outside the system under test. It owns the processes (so it can
kill and restart them) and publishes live faults on a control channel that every
node applies to its link model. Child processes never outlive it.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import math
import sys
import time
from datetime import datetime
from pathlib import Path

import yaml
import zmq
import zmq.asyncio

from ..common.clock import MissionClock
from ..common.config import Runtime, load_scenario
from ..common.events import EventClient
from ..common.node import run_async
from ..control.procs import ProcessGroup
from ..tools.report import summarize
from ..transport.faults import Fault, parse_faults

ROOT = Path(__file__).resolve().parents[2]

MODULES = {
    "logger": "sar.logger.collector",
    "world": "sar.world.server",
    "groundstation": "sar.groundstation.station",
    "drone": "sar.drone.drone",
    "operator": "sar.operator.bot",
}
KIND_MODULES = {"commander": "sar.operator.commander"}  # operator kinds with their own program
INFRASTRUCTURE = {"logger", "world"}  # not part of the system under test; cannot be killed from the UI
START_ORDER = ["logger", "world", "groundstation", "operator", "drone"]


def control_port(scenario: dict) -> int:
    port = scenario["control"]["port"]
    if port == "auto":
        return 8090 + (int(scenario["base_port"]) - 5550) // 100
    return int(port)


def build_runtime(scenario: dict, run_dir: Path) -> Runtime:
    port = int(scenario["base_port"])
    nodes: dict[str, dict] = {
        "logger": {"role": "logger", "port": port, "bus": False},
        "world": {"role": "world", "port": port + 1, "bus": False},
    }
    next_port = port + 10
    base = [float(scenario["base"]["x"]), float(scenario["base"]["y"])]
    count = int(scenario["ground_stations"]["count"])
    for n in range(1, count + 1):
        # Stations stand side by side at the base (a command post) unless placed.
        default = [base[0] + 60.0 * (n - 1 - (count - 1) / 2), base[1]]
        pos = scenario["ground_stations"].get("positions", {}).get(f"gs-{n}", default)
        nodes[f"gs-{n}"] = {"role": "groundstation", "port": next_port, "pos": list(pos)}
        if scenario["ui"]["enabled"]:
            nodes[f"gs-{n}"]["http_port"] = int(scenario["ui"]["port"]) + n - 1
        next_port += 1
    for op in scenario["operators"]:
        nodes[op["id"]] = {"role": "operator", "port": next_port, "kind": op.get("kind", "bot")}
        next_port += 1
    for n in range(1, int(scenario["drones"]["count"]) + 1):
        nodes[f"drone-{n}"] = {"role": "drone", "port": next_port}
        next_port += 1
    return Runtime(
        run_dir=str(run_dir),
        mission_start=time.time() + float(scenario["startup_delay_s"]),
        scenario=scenario,
        nodes=nodes,
        world_addr=f"tcp://127.0.0.1:{nodes['world']['port']}",
        logger_addr=f"tcp://127.0.0.1:{nodes['logger']['port']}",
        control_addr=f"tcp://127.0.0.1:{port + 2}",
    )


def apply_override(scenario: dict, assignment: str) -> None:
    """Apply one --set key.path=value (value parsed as YAML: true, 0.5, [a, b] ...)."""
    path, eq, raw = assignment.partition("=")
    if not path or not eq:
        raise SystemExit(f"--set expects key.path=value, got {assignment!r}")
    keys = path.split(".")
    node = scenario
    for k in keys[:-1]:
        node = node.setdefault(k, {})
    node[keys[-1]] = yaml.safe_load(raw)


class Launcher:
    def __init__(self, scenario: dict, run_dir: Path, control: bool):
        self.scenario = scenario
        self.run_dir = run_dir
        self.log_dir = run_dir / "logs"
        self.log_dir.mkdir(parents=True)
        self.rt = build_runtime(scenario, run_dir)
        self.rt_path = run_dir / "runtime.json"
        self.rt.save(self.rt_path)
        self.clock = MissionClock(self.rt.mission_start, self.rt.speedup)
        self.run_speed = self.rt.speedup  # speed to resume at
        self.events = EventClient(self.rt.logger_addr, "launcher", self.clock)
        self.group = ProcessGroup()
        self.procs: dict = {}
        self.incarnation: dict[str, int] = {}
        self.down: set[str] = set()  # nodes stopped on purpose (UI or scripted kill)
        self.script = parse_faults(scenario.get("faults", []))
        self.live: list[Fault] = []
        self.loss = float(scenario["radio"]["loss"])
        self.version = 0
        self._fault_ids = 0
        self.control = control
        self.observer = None
        self.world = None
        self.stop_event: asyncio.Event | None = None

    # -- processes --------------------------------------------------------------------

    def role(self, nid: str) -> str:
        return self.rt.nodes[nid]["role"]

    def start(self, nid: str) -> None:
        inc = self.incarnation.get(nid, -1) + 1
        self.incarnation[nid] = inc
        module = KIND_MODULES.get(self.rt.nodes[nid].get("kind")) or MODULES[self.role(nid)]
        args = [sys.executable, "-m", module, "--runtime", str(self.rt_path), "--id", nid]
        if self.role(nid) not in INFRASTRUCTURE:
            args += ["--incarnation", str(inc)]
        self.procs[nid] = self.group.spawn(args, ROOT, self.log_dir / f"{nid}.log")

    def alive(self, nid: str) -> bool:
        p = self.procs.get(nid)
        return p is not None and p.poll() is None

    def kill(self, nid: str, reason: str = "user") -> dict:
        if nid not in self.rt.nodes or self.role(nid) in INFRASTRUCTURE:
            return {"ok": False, "reason": f"{nid} cannot be killed"}
        if not self.alive(nid):
            return {"ok": False, "reason": f"{nid} is not running"}
        self.procs[nid].terminate()
        self.down.add(nid)
        self.events.emit("node_killed", target=nid, reason=reason)
        print(f"t={self.clock.now():6.0f}s killed {nid} ({reason})", flush=True)
        return {"ok": True}

    def restart(self, nid: str) -> dict:
        if nid not in self.rt.nodes or self.role(nid) in INFRASTRUCTURE:
            return {"ok": False, "reason": f"{nid} cannot be restarted"}
        if self.alive(nid):
            self.procs[nid].terminate()
            try:
                self.procs[nid].wait(timeout=5)
            except Exception:
                self.procs[nid].kill()
        self.start(nid)
        self.down.discard(nid)
        self.events.emit("node_restarted", target=nid, incarnation=self.incarnation[nid])
        print(f"t={self.clock.now():6.0f}s restarted {nid} (incarnation {self.incarnation[nid]})", flush=True)
        return {"ok": True, "incarnation": self.incarnation[nid]}

    # -- live faults --------------------------------------------------------------------

    def _changed(self) -> None:
        self.version += 1

    def add_fault(self, kind: str, nodes: list[str] | None = None, zone: dict | None = None,
                  duration_min: float | None = None) -> dict:
        if kind not in ("isolate", "cut", "jam"):
            return {"ok": False, "reason": f"unknown fault kind {kind!r}"}
        nodes = list(nodes or [])
        if kind == "isolate" and not nodes:
            return {"ok": False, "reason": "choose at least one node"}
        if kind == "cut" and (len(nodes) != 2 or nodes[0] == nodes[1]):
            return {"ok": False, "reason": "choose two different nodes"}
        if kind == "jam" and not zone:
            return {"ok": False, "reason": "place the jam zone on the map"}
        start = self.clock.now()
        end = math.inf if not duration_min else start + float(duration_min) * 60
        self._fault_ids += 1
        f = Fault(kind, start, end, nodes=nodes, zone=zone, id=f"f{self._fault_ids}")
        self.live.append(f)
        self._changed()
        self.events.emit("fault_start", **f.describe(), source="control")
        print(f"t={start:6.0f}s live fault: {f.describe()}", flush=True)
        return {"ok": True, "id": f.id}

    def end_fault(self, fault_id: str) -> dict:
        now = self.clock.now()
        for f in self.live:
            if f.id == fault_id and f.active(now):
                f.end = now
                self._changed()
                self.events.emit("fault_end", **f.describe(), source="control")
                return {"ok": True}
        return {"ok": False, "reason": "no such active fault"}

    def clear_faults(self) -> dict:
        now = self.clock.now()
        for f in self.live:
            if f.active(now):
                self.end_fault(f.id)
        return {"ok": True}

    def set_loss(self, loss: float) -> dict:
        self.loss = max(0.0, min(0.95, float(loss)))
        self._changed()
        self.events.emit("loss_changed", loss=self.loss)
        return {"ok": True, "loss": self.loss}

    def control_state(self) -> dict:
        now = self.clock.now()
        self.live = [f for f in self.live if f.end > now - 5]  # forget long-finished faults
        return {"version": self.version, "faults": [f.describe() for f in self.live], "loss": self.loss,
                "clock": self.clock.timeline()}

    # -- mission clock: pause and speed -------------------------------------------------------

    def set_speed(self, speed: float) -> dict:
        speed = max(0.0, min(200.0, float(speed)))
        if speed > 0:
            self.run_speed = speed
        self.clock.set_speed(speed)
        self._changed()
        self.events.emit("clock_changed", speed=speed, paused=speed == 0)
        print(f"t={self.clock.now():6.0f}s mission {'paused' if speed == 0 else f'speed x{speed:g}'}", flush=True)
        return {"ok": True, "speed": speed}

    def pause(self) -> dict:
        return self.set_speed(0)

    def resume(self) -> dict:
        return self.set_speed(self.run_speed)

    @property
    def mission_total(self) -> float:
        """The run ends at this mission time (duration_s real seconds at the scenario's speed)."""
        return float(self.scenario["duration_s"]) * self.rt.speedup

    # -- views for the control center ------------------------------------------------------

    def map_view(self) -> dict:
        w = self.world
        return {
            "width": w.grid.width, "height": w.grid.height, "cell_m": w.grid.cell_m,
            "terrain": w.terrain,
            "base": [self.scenario["base"]["x"], self.scenario["base"]["y"]],
            "victims": [{"id": v.id, "x": round(v.x, 1), "y": round(v.y, 1)} for v in w.victims],
            "decoys": [{"id": v.id, "x": round(v.x, 1), "y": round(v.y, 1)} for v in w.decoys],
            "radio": {k: self.scenario["radio"][k] for k in ("drone", "groundstation")},
            "radio_loss": float(self.scenario["radio"]["loss"]),
        }

    def state_view(self) -> dict:
        now = self.clock.now()
        active = [f.describe() for f in self.script + self.live if f.kind not in ("kill", "restart") and f.active(now)]
        nodes = []
        for nid, n in self.rt.nodes.items():
            nodes.append({
                "id": nid, "role": n["role"], "running": self.alive(nid), "down": nid in self.down,
                "incarnation": self.incarnation.get(nid, 0), "infrastructure": n["role"] in INFRASTRUCTURE,
                "isolated": any(f["kind"] == "isolate" and nid in f.get("nodes", []) for f in active),
            })
        obs = self.observer.snapshot() if self.observer else {}
        return {
            "t": round(now, 1),
            "name": self.scenario["name"],
            "speedup": self.clock.speed,
            "run_speed": self.run_speed,
            "nominal_speed": self.rt.speedup,
            "paused": self.clock.paused,
            "duration_s": self.scenario["duration_s"],
            "mission_total": self.mission_total,
            "left_real_s": round((self.mission_total - now) / self.clock.speed, 1) if self.clock.speed else None,
            "loss": self.loss,
            "nodes": nodes,
            "faults": active,
            "ui_ports": {nid: n["http_port"] for nid, n in self.rt.nodes.items() if n.get("http_port")},
            **obs,
        }

    # -- background loops ------------------------------------------------------------------

    async def publish_loop(self) -> None:
        ctx = zmq.asyncio.Context()
        pub = ctx.socket(zmq.PUB)
        pub.setsockopt(zmq.LINGER, 0)
        pub.bind(self.rt.control_addr)
        sent = -1
        last = 0.0
        while True:
            # Full state twice a second (late joiners and restarted nodes catch up),
            # and at once after every change.
            if self.version != sent or time.monotonic() - last > 0.5:
                await pub.send(json.dumps(self.control_state()).encode())
                sent, last = self.version, time.monotonic()
            await asyncio.sleep(0.05)

    async def script_loop(self) -> None:
        started, ended = set(), set()
        live_ended: set[str] = set()
        while True:
            t = self.clock.now()
            for k, f in enumerate(self.script):
                if k not in started and t >= f.start:
                    started.add(k)
                    self.events.emit("fault_start", **f.describe(), source="script")
                    print(f"t={t:6.0f}s scripted fault: {f.describe()}", flush=True)
                    if f.kind == "kill":
                        self.kill(f.nodes[0], reason="scripted")
                    elif f.kind == "restart":
                        self.restart(f.nodes[0])
                if k in started and k not in ended and not math.isinf(f.end) and t >= f.end:
                    ended.add(k)
                    self.events.emit("fault_end", **f.describe(), source="script")
            for f in self.live:  # live faults that ran out on their own
                if f.id not in live_ended and not math.isinf(f.end) and t >= f.end:
                    live_ended.add(f.id)
                    self.events.emit("fault_end", **f.describe(), source="control")
            await asyncio.sleep(0.25)

    async def monitor_loop(self) -> None:
        reported: set[tuple[str, int]] = set()
        while True:
            for nid, p in self.procs.items():
                key = (nid, self.incarnation.get(nid, 0))
                if p.poll() is not None and nid not in self.down and key not in reported:
                    reported.add(key)
                    print(f"process exited unexpectedly: {nid} (see {self.log_dir / (nid + '.log')})", flush=True)
                    self.events.emit("node_crashed", target=nid, code=p.returncode)
                    if self.role(nid) in INFRASTRUCTURE:
                        self.stop_event.set()
            await asyncio.sleep(1.0)

    async def observer_loop(self) -> None:
        while True:
            self.observer.poll()
            await asyncio.sleep(0.5)

    # -- main --------------------------------------------------------------------------

    async def run(self) -> None:
        self.stop_event = asyncio.Event()
        tasks = [asyncio.create_task(self.publish_loop())]
        for role in START_ORDER:
            for nid, n in self.rt.nodes.items():
                if n["role"] == role:
                    self.start(nid)
            if role in INFRASTRUCTURE:
                await asyncio.sleep(0.5)
        duration = float(self.scenario["duration_s"])
        print(f"started {len(self.procs)} processes; mission runs {duration:.0f} s real "
              f"({duration * self.rt.speedup / 60:.0f} mission minutes)", flush=True)
        if self.control:
            from ..control.app import serve
            from ..control.observer import Observer
            from ..world.server import World

            self.world = World(self.scenario)  # same seed -> same world as the world server
            self.observer = Observer(self.run_dir, self.rt.nodes)
            port = control_port(self.scenario)
            tasks += [asyncio.create_task(self.observer_loop()), asyncio.create_task(serve(self, port))]
            print(f"control center: http://127.0.0.1:{port}/", flush=True)
        for nid, n in self.rt.nodes.items():
            if n.get("http_port"):
                print(f"operator UI ({nid}): http://127.0.0.1:{n['http_port']}/", flush=True)
        tasks += [asyncio.create_task(self.script_loop()), asyncio.create_task(self.monitor_loop())]
        # The run ends at a mission time, so pausing makes it longer and speeding up shorter.
        try:
            while not self.stop_event.is_set() and self.clock.now() < self.mission_total:
                try:
                    await asyncio.wait_for(self.stop_event.wait(), timeout=0.5)
                except asyncio.TimeoutError:
                    pass
        finally:
            for t in tasks:
                t.cancel()
            self.shutdown()

    def shutdown(self) -> None:
        # Stop the logger last so it records everything the others sent.
        for nid, p in self.procs.items():
            if nid != "logger" and p.poll() is None:
                p.terminate()
        time.sleep(0.5)
        if "logger" in self.procs and self.procs["logger"].poll() is None:
            self.procs["logger"].terminate()
        self.group.stop_all()


def main() -> None:
    parser = argparse.ArgumentParser(description="Run a SAR scenario")
    parser.add_argument("scenario")
    parser.add_argument("--duration", type=float, help="override duration_s (real seconds)")
    parser.add_argument("--set", action="append", default=[], metavar="KEY=VALUE",
                        help="override a scenario value, e.g. radio.relay=false (repeatable)")
    parser.add_argument("--name", help="suffix added to the run name")
    parser.add_argument("--no-control", action="store_true", help="do not serve the control center")
    parser.add_argument("--run-dir", help="write the run here instead of runs/<time>_<name>")
    args = parser.parse_args()

    scenario = load_scenario(args.scenario)
    if args.duration is not None:
        scenario["duration_s"] = args.duration
    for assignment in args.set:
        apply_override(scenario, assignment)
    if args.name:
        scenario["name"] = f"{scenario['name']}_{args.name}"
    run_dir = Path(args.run_dir) if args.run_dir else ROOT / "runs" / f"{datetime.now():%Y%m%d-%H%M%S}_{scenario['name']}"
    launcher = Launcher(scenario, run_dir, control=bool(scenario["control"]["enabled"]) and not args.no_control)
    print(f"run directory: {run_dir}", flush=True)
    try:
        run_async(launcher.run())
    except KeyboardInterrupt:
        print("interrupted", flush=True)
        launcher.shutdown()
    summarize(run_dir)


if __name__ == "__main__":
    main()
