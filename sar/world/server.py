"""World server: holds ground truth and answers sense / map / truth requests.

Protocol: clients use a DEALER socket and send msgpack dicts with a request id
`rid`; the server (ROUTER) replies with a dict carrying the same `rid`.
  op "map"   -> grid size, terrain rows, terrain table
  op "sense" -> detections for one pass of a drone over a cell
  op "truth" -> nearest victim within a radius (bot operator and evaluation only)
"""
from __future__ import annotations

import asyncio
import json
import math
import random
import sys
import traceback
from collections import defaultdict

import msgpack
import zmq
import zmq.asyncio

from ..common.clock import MissionClock
from ..common.config import Runtime
from ..common.events import EventClient
from ..common.grid import Grid, stable_int
from ..common.node import node_args, run_async
from .sensor import TerrainSensor
from .terrain import generate_terrain, place_victims


class World:
    """Pure world model, independent of networking (used directly by tests)."""

    def __init__(self, scenario: dict):
        w = scenario["world"]
        self.seed = int(scenario["seed"])
        self.grid = Grid(int(w["width_cells"]), int(w["height_cells"]), float(w["cell_m"]))
        self.table = scenario["terrain"]
        self.terrain = generate_terrain(self.grid, self.seed, int(w["regions"]), w["terrain_mix"])
        self.victims = place_victims(
            self.grid, self.terrain, self.table, self.seed, int(w["victims"]), int(w["signature_dims"])
        )
        self.decoys = place_victims(
            self.grid, self.terrain, self.table, self.seed, int(w.get("decoys", 0)), int(w["signature_dims"]),
            prefix="d", weighted=False,
        )
        self.visits: dict = defaultdict(int)  # (cell, mode) -> passes so far
        self.by_cell = defaultdict(list)
        for v in self.victims + self.decoys:
            self.by_cell[(v.i, v.j)].append(v)
        self.sensor = TerrainSensor(
            self.grid, self.table, float(w["transit_pod_factor"]), float(w["signature_noise"]), int(w["signature_dims"]),
            transit_pfa_factor=float(w["transit_pfa_factor"]),
            decoy_pod_factor=float(w.get("decoy_pod_factor", 0.6)),
            inspect_pod_factor=float(w.get("inspect_pod_factor", 1.8)),
        )

    def sense(self, drone: str, cell: tuple[int, int], mode: str, req: int) -> list[dict]:
        i, j = cell
        if not (0 <= i < self.grid.width and 0 <= j < self.grid.height):
            return []
        # Randomness depends on (seed, cell, mode, n-th pass of that mode), not
        # on which drone flew it: sector assignment depends on network timing,
        # and must not change what is found. Victims and false alarms use
        # separate streams, so changing one rate leaves the other untouched.
        self.visits[(cell, mode)] += 1
        n = self.visits[(cell, mode)]
        rng = random.Random(stable_int(self.seed, i, j, mode, n, "victims"))
        rng_false = random.Random(stable_int(self.seed, i, j, mode, n, "false"))
        return self.sensor.sense(cell, self.terrain[j][i], mode, self.by_cell.get(cell, []), rng, rng_false)

    def truth(self, x: float, y: float, radius: float) -> dict:
        best, best_d = None, None
        for v in self.victims:
            d = math.hypot(v.x - x, v.y - y)
            if d <= radius and (best_d is None or d < best_d):
                best, best_d = v, d
        return {"victim": best.id if best else None, "distance": round(best_d, 2) if best else None}


class WorldServer:
    def __init__(self, rt: Runtime, node_id: str):
        self.rt = rt
        self.id = node_id
        self.clock = MissionClock(rt.mission_start, rt.speedup)
        self.events = EventClient(rt.logger_addr, node_id, self.clock)
        self.world = World(rt.scenario)
        self.ctx = zmq.asyncio.Context()

    def handle(self, req: dict) -> dict:
        op = req.get("op")
        if op == "sense":
            cell = (int(req["cell"][0]), int(req["cell"][1]))
            dets = self.world.sense(req["drone"], cell, req["mode"], int(req["req"]))
            terrain = self.world.terrain[cell[1]][cell[0]]
            for k, d in enumerate(dets):
                self.events.emit(
                    "detection", drone=req["drone"], req=req["req"], det=k, truth=d["truth"],
                    terrain=terrain, mode=req["mode"], i=cell[0], j=cell[1],
                )
            return {"detections": [{k: v for k, v in d.items() if k != "truth"} for d in dets]}
        if op == "map":
            g = self.world.grid
            return {"width": g.width, "height": g.height, "cell_m": g.cell_m,
                    "terrain": self.world.terrain, "table": self.world.table}
        if op == "truth":
            return self.world.truth(float(req["x"]), float(req["y"]), float(req["radius"]))
        return {"error": f"unknown op {op!r}"}

    async def follow_clock(self) -> None:
        """Apply pause / speed changes, so detection times are stamped in mission time."""
        sub = self.ctx.socket(zmq.SUB)
        sub.setsockopt(zmq.LINGER, 0)
        sub.setsockopt(zmq.SUBSCRIBE, b"")
        sub.connect(self.rt.control_addr)
        while True:
            state = json.loads(await sub.recv())
            if state.get("clock"):
                self.clock.apply(state["clock"])

    async def run(self) -> None:
        if self.rt.control_addr:
            asyncio.get_running_loop().create_task(self.follow_clock())
        sock = self.ctx.socket(zmq.ROUTER)
        sock.setsockopt(zmq.LINGER, 0)
        sock.bind(self.rt.world_addr)
        self.events.emit(
            "world_ready",
            width=self.world.grid.width, height=self.world.grid.height, cell_m=self.world.grid.cell_m,
            victims=[{"id": v.id, "x": round(v.x, 2), "y": round(v.y, 2), "terrain": v.terrain} for v in self.world.victims],
            decoys=[{"id": v.id, "x": round(v.x, 2), "y": round(v.y, 2), "terrain": v.terrain} for v in self.world.decoys],
            terrain_counts={t: sum(row.count(t) for row in self.world.terrain) for t in self.world.table},
        )
        while True:
            ident, payload = await sock.recv_multipart()
            try:
                req = msgpack.unpackb(payload, raw=False)
                res = self.handle(req)
            except Exception as exc:
                traceback.print_exc(file=sys.stderr)
                req, res = {}, {"error": str(exc)}
            res["rid"] = req.get("rid")
            await sock.send_multipart([ident, msgpack.packb(res, use_bin_type=True)])


def main() -> None:
    args = node_args()
    server = WorldServer(Runtime.load(args.runtime), args.id)
    try:
        run_async(server.run())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
