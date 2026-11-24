"""Simulated camera plus detector.

A real system would get a position estimate, an appearance embedding and an
image crop from its detector. We produce all three:
  - position: the object's true position plus noise that depends on terrain
  - signature: the object's hidden signature plus noise
  - thumbnail: a 32 x 32 simulated thermal crop; a person (head, body, limbs) for
    real victims, a warm lump with no head or limbs for decoys, small random spots
    for one-off false alarms, blurrier and noisier when the terrain makes the view poor.

Three kinds of detection:
  - victims (truth "v..."): detected with the terrain's POD
  - decoys (truth "d..."): fixed objects that look like people (debris, clothing);
    detected with POD x decoy_pod_factor on every pass, so they come back
  - one-off false alarms (truth None): random, with the terrain's PFA
"""
from __future__ import annotations

import math
import random

from ..common.grid import Grid

THUMB = 32  # pixels per side of the evidence crop


def _noisy_unit(vec: list[float], noise: float, rng: random.Random) -> list[float]:
    out = [v + rng.gauss(0, noise / math.sqrt(len(vec))) for v in vec]
    norm = math.sqrt(sum(v * v for v in out)) or 1.0
    return [round(v / norm, 4) for v in out]


def _clamp(v: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, v))


def _seg_dist(px: float, py: float, ax: float, ay: float, bx: float, by: float) -> float:
    """Distance from point p to segment a-b."""
    dx, dy = bx - ax, by - ay
    t = max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / (dx * dx + dy * dy or 1.0)))
    return math.hypot(px - ax - t * dx, py - ay - t * dy)


def _person(rng: random.Random) -> list[tuple]:
    """A person lying down, as capsules (ax, ay, bx, by, half-width) in body coordinates
    (head up, 1 px ~ 10 cm): head, torso, two arms, two legs, limbs in a random pose."""
    parts = [(0, -7.5, 0, -7.5, 2.3), (0, -4.5, 0, 2.5, 2.6)]
    for side in (-1, 1):
        a = math.radians(rng.uniform(20, 110))
        parts.append((side * 1.8, -3.8, side * (1.8 + 6.5 * math.sin(a)), -3.8 + 6.5 * math.cos(a), 1.1))
        spread = rng.uniform(0.5, 3.5)
        parts.append((side * 1.0, 2.5, side * spread, 10.0, 1.3))
    return parts


def _decoy(rng: random.Random) -> list[tuple]:
    """Debris or a pile of clothing: one warm lump, straight-edged or round, no head or limbs."""
    w, h = rng.uniform(3.0, 5.5), rng.uniform(2.0, 4.0)
    if rng.random() < 0.5:
        return [(-w, 0, w, 0, h)]  # elongated lump
    return [(-w / 2, -h / 2, w / 2, h / 2, rng.uniform(3.0, 4.5)), (w / 2, -h, w / 2 + 1, h, 2.0)]


def _blobs(rng: random.Random) -> list[tuple]:
    """A one-off false alarm: sun-warmed stones, a puddle edge: 1-3 small irregular spots."""
    out = []
    for _ in range(rng.randint(1, 3)):
        x, y = rng.uniform(-8, 8), rng.uniform(-8, 8)
        out.append((x, y, x + rng.uniform(-2, 2), y + rng.uniform(-2, 2), rng.uniform(1.0, 2.8)))
    return out


def _blur3(img: list[list[float]]) -> list[list[float]]:
    """3 x 3 box blur, done as a row pass and a column pass (edges repeat)."""
    def line(v):
        p = [v[0]] + v + [v[-1]]
        return [(p[k] + p[k + 1] + p[k + 2]) / 3 for k in range(len(v))]
    rows = [line(r) for r in img]
    cols = [line(list(c)) for c in zip(*rows)]
    return [list(r) for r in zip(*cols)]


def thumbnail(kind: str, quality: float, rng: random.Random) -> list[int]:
    """Simulated thermal crop around a detection: 32 x 32 grey values, row-major.
    kind: "victim", "decoy" or "noise". Warm objects are bright. A poor view
    (low quality: forest canopy, rubble, smoke) lowers contrast, blurs and adds noise,
    so some crops stay doubtful, as they do in real footage."""
    shapes = {"victim": _person, "decoy": _decoy}.get(kind, _blobs)(rng)
    angle = rng.uniform(0, 2 * math.pi)
    ca, sa = math.cos(angle), math.sin(angle)
    ox, oy = rng.uniform(-3, 3), rng.uniform(-3, 3)
    warm = {"victim": 225, "decoy": rng.uniform(165, 200)}.get(kind, rng.uniform(150, 205))
    contrast = 0.45 + 0.55 * quality
    # Ground: cool, with a broad gradient (sun, shade) so the background is not flat.
    gx, gy, g0 = rng.uniform(-1.2, 1.2), rng.uniform(-1.2, 1.2), rng.uniform(70, 100)
    c = (THUMB - 1) / 2
    reach = max(max(math.hypot(s[0], s[1]), math.hypot(s[2], s[3])) + s[4] for s in shapes) + 1.0
    img = []
    for r in range(THUMB):
        row = []
        for col in range(THUMB):
            bg = g0 + gx * (col - c) + gy * (r - c)
            # pixel -> body coordinates (undo shift and rotation)
            x, y = col - c - ox, r - c - oy
            if x * x + y * y > reach * reach:
                row.append(bg)  # far from the object: ground only
                continue
            bx, by = ca * x + sa * y, -sa * x + ca * y
            d = min(_seg_dist(bx, by, *s[:4]) - s[4] for s in shapes)
            cover = _clamp(0.5 - d)  # soft edge, 1 px wide
            row.append(bg + (warm - bg) * contrast * cover)
        img.append(row)
    # Clutter (leaves, debris, heat shimmer) is part of the scene, so the blur softens it too;
    # a little sensor grain is added last.
    clutter = 4 + 26 * (1 - quality)
    img = [[v + rng.gauss(0, clutter) for v in row] for row in img]
    for _ in range(round(2.5 * (1 - quality))):  # blur: out of focus, motion, distance
        img = _blur3(img)
    return [int(_clamp(v + rng.gauss(0, 3), 0, 255)) for row in img for v in row]


class TerrainSensor:
    def __init__(self, grid: Grid, table: dict, transit_pod_factor: float, signature_noise: float, dims: int,
                 transit_pfa_factor: float = 1.0, decoy_pod_factor: float = 0.6, inspect_pod_factor: float = 1.8):
        self.grid = grid
        self.table = table
        self.transit_pod_factor = transit_pod_factor
        self.transit_pfa_factor = transit_pfa_factor
        self.signature_noise = signature_noise
        self.dims = dims
        self.decoy_pod_factor = decoy_pod_factor
        self.inspect_pod_factor = inspect_pod_factor
        self.make_thumbs = True  # tests that only count detections switch the images off

    def pod(self, terrain: str, mode: str) -> float:
        pod = self.table[terrain]["pod"]
        if mode == "inspect":  # low, slow, hovering over one spot: a much better look
            return min(0.98, pod * self.inspect_pod_factor)
        return pod * self.transit_pod_factor if mode == "transit" else pod

    def pfa(self, terrain: str, mode: str) -> float:
        """A faster, higher transit pass gives fewer false alarms as well as fewer detections."""
        pfa = self.table[terrain]["pfa"]
        if mode == "inspect":
            return pfa * 0.3
        return pfa * self.transit_pfa_factor if mode == "transit" else pfa

    def sense(self, cell: tuple[int, int], terrain: str, mode: str, objects_in_cell: list,
              rng: random.Random, rng_false: random.Random | None = None) -> list[dict]:
        """Return detections for one pass over `cell`. Each detection carries a
        `truth` field (object id or None) that the server strips before replying.

        `rng_false` drives one-off false alarms separately, so changing the
        false-alarm rate never changes which real victims are detected."""
        if rng_false is None:
            rng_false = rng
        params = self.table[terrain]
        pod = self.pod(terrain, mode)
        sigma = params["sigma"]
        detections = []
        for v in objects_in_cell:
            decoy = v.id.startswith("d")
            if rng.random() < (pod * self.decoy_pod_factor if decoy else pod):
                quality = _clamp(params["pod"] * rng.uniform(0.8, 1.15) + (0.35 if mode == "inspect" else 0.0))
                detections.append(
                    {
                        "x": round(v.x + rng.gauss(0, sigma), 2),
                        "y": round(v.y + rng.gauss(0, sigma), 2),
                        "confidence": round(rng.uniform(0.40, 0.85) if decoy else rng.uniform(0.45, 0.95), 3),
                        "signature": _noisy_unit(v.signature, self.signature_noise, rng),
                        "evidence": round(quality, 3),
                        "thumb": thumbnail("decoy" if decoy else "victim", quality, rng) if self.make_thumbs else None,
                        "truth": v.id,
                    }
                )
        rf = rng_false
        if rf.random() < self.pfa(terrain, mode):
            i, j = cell
            sig = [rf.gauss(0, 1) for _ in range(self.dims)]
            quality = _clamp(params["pod"] * rf.uniform(0.5, 1.0))
            detections.append(
                {
                    "x": round((i + rf.random()) * self.grid.cell_m, 2),
                    "y": round((j + rf.random()) * self.grid.cell_m, 2),
                    "confidence": round(rf.uniform(0.30, 0.80), 3),
                    "signature": _noisy_unit(sig, 0.0, rf),
                    "evidence": round(quality, 3),
                    "thumb": thumbnail("noise", quality, rf) if self.make_thumbs else None,
                    "truth": None,
                }
            )
        return detections
