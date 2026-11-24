"""Replicated log: Raft and primary-backup (architecture doc, section 9).

Both algorithms keep the same thing: an ordered log of commands, identical on
every station up to the commit index. Each committed command is passed to
`apply(index, cmd)` exactly once, in order, on every replica.

They differ in who may lead and when a command counts as committed:

  primary-backup  The primary is fixed until it goes silent; then the next
                  backup takes over with a higher epoch. A command commits once
                  every backup that is still reachable has stored it (or at once
                  if none is). Weakness: a cut link between live stations looks
                  like a dead primary, so two primaries can commit different
                  things. On reconnect the older one steps down, and anything it
                  committed alone is lost from the log (counted) and re-submitted.

  raft            A leader needs votes from a majority, and a command commits
                  once a majority has stored it. At most one leader can commit
                  in any term, and a committed command is never lost. A leader
                  that cannot reach a majority steps down, so the minority side
                  has no leader and its decisions wait (provisional).

Entries that a node has to discard (overwritten by the new leader's log) are
handed to `on_orphans`, so the station can re-submit them as merged commands.

The classes are transport-free: the owner calls tick() regularly and handle()
for every consensus message, and supplies send(dst, msg) and now().
"""
from __future__ import annotations

import json
import random
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

MAX_BATCH = 64


@dataclass
class Entry:
    term: int
    cmd: dict


class Store:
    """Append-only file: terms, votes, log changes, commit index, replicated items.

    Replayed on restart, so a station that crashes comes back with its log."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._f = open(self.path, "a", encoding="utf-8", buffering=1)

    def _write(self, rec: dict) -> None:
        self._f.write(json.dumps(rec, separators=(",", ":")) + "\n")

    def term(self, term: int, voted_for: str | None) -> None:
        self._write({"k": "term", "term": term, "vote": voted_for})

    def append(self, index: int, entry: Entry) -> None:
        self._write({"k": "entry", "i": index, "term": entry.term, "cmd": entry.cmd})

    def truncate(self, index: int) -> None:
        self._write({"k": "trunc", "i": index})

    def commit(self, index: int) -> None:
        self._write({"k": "commit", "i": index})

    def item(self, kind: str, data: dict) -> None:
        self._write({"k": "item", "kind": kind, "data": data})

    @staticmethod
    def load(path: str | Path) -> dict:
        state = {"term": 0, "vote": None, "entries": [], "commit": 0, "items": []}
        p = Path(path)
        if not p.exists():
            return state
        with open(p, encoding="utf-8") as f:
            for line in f:
                try:
                    r = json.loads(line)
                except json.JSONDecodeError:
                    continue  # last line cut off by a crash
                k = r["k"]
                if k == "term":
                    state["term"], state["vote"] = r["term"], r["vote"]
                elif k == "entry":
                    del state["entries"][r["i"] - 1:]
                    state["entries"].append(Entry(r["term"], r["cmd"]))
                elif k == "trunc":
                    del state["entries"][r["i"] - 1:]
                elif k == "commit":
                    state["commit"] = r["i"]
                elif k == "item":
                    state["items"].append((r["kind"], r["data"]))
        state["commit"] = min(state["commit"], len(state["entries"]))
        return state


class Replica:
    kind = "base"

    def __init__(self, me: str, peers: list[str], send: Callable[[str, dict], None], now: Callable[[], float],
                 apply: Callable[[int, dict], None], cfg: dict, store: Store | None = None,
                 loaded: dict | None = None, rng: random.Random | None = None,
                 on_event: Callable[..., None] | None = None, on_orphans: Callable[[list[dict]], None] | None = None,
                 on_reset: Callable[[], None] | None = None):
        self.me = me
        self.peers = sorted(p for p in peers if p != me)
        self.send = send
        self.now = now
        self.apply_cb = apply
        self.cfg = cfg
        self.store = store
        self.rng = rng or random.Random()
        self.on_event = on_event or (lambda ev, **kw: None)
        self.on_orphans = on_orphans or (lambda cmds: None)
        self.on_reset = on_reset or (lambda: None)
        self.term = 0
        self.voted_for: str | None = None
        self.log: list[Entry] = []
        self.commit = 0
        self.applied = 0
        self.role = "follower"
        self.leader: str | None = None
        self.leader_heard = -1e9
        self.next_index: dict[str, int] = {}
        self.match_index: dict[str, int] = {}
        self.last_reply: dict[str, float] = {}
        self.last_send = -1e9
        self.hb = float(cfg.get("heartbeat_real_s", 0.15))
        if loaded:
            self.term, self.voted_for = loaded["term"], loaded["vote"]
            self.log = list(loaded["entries"])
            self.commit = loaded["commit"]

    # -- helpers ------------------------------------------------------------------------

    @property
    def n(self) -> int:
        return len(self.peers) + 1

    def majority(self) -> int:
        return self.n // 2 + 1

    def last_index(self) -> int:
        return len(self.log)

    def term_at(self, i: int) -> int:
        return self.log[i - 1].term if 1 <= i <= len(self.log) else 0

    def is_leader(self) -> bool:
        return self.role == "leader"

    def leader_alive(self, timeout: float) -> bool:
        """A follower that heard its leader recently."""
        return self.role == "follower" and self.leader is not None and self.now() - self.leader_heard <= timeout

    def _set_term(self, term: int, vote: str | None = None) -> None:
        self.term, self.voted_for = term, vote
        if self.store:
            self.store.term(term, vote)

    def _set_role(self, role: str, leader: str | None) -> None:
        changed = role != self.role or leader != self.leader
        self.role, self.leader = role, leader
        if changed:
            self.on_event("role", role=role, term=self.term, leader=leader)

    def _become_follower(self, term: int, leader: str | None) -> None:
        if term > self.term:
            self._set_term(term, None)
        self._set_role("follower", leader)

    def _become_leader(self) -> None:
        self._set_role("leader", self.me)
        now = self.now()
        self.next_index = {p: self.last_index() + 1 for p in self.peers}
        self.match_index = {p: 0 for p in self.peers}
        self.last_reply = {p: now for p in self.peers}
        # A no-op in the new term lets the leader commit what earlier leaders left.
        self._append_local(Entry(self.term, {"op": "noop", "cid": f"{self.me}:noop:{self.term}"}))
        self._replicate_all()
        self._advance_commit()

    def _append_local(self, e: Entry) -> None:
        self.log.append(e)
        if self.store:
            self.store.append(len(self.log), e)

    def _truncate(self, index: int) -> None:
        removed = self.log[index - 1:]
        if not removed:
            return
        self.log = self.log[:index - 1]
        if self.store:
            self.store.truncate(index)
        lost = max(0, self.commit - (index - 1))
        if lost:
            # Only possible in primary-backup: entries this node had committed alone.
            self.on_event("committed_lost", count=lost)
            self.commit = index - 1
        if self.applied > index - 1:
            # State built from discarded entries must be rebuilt from the surviving log.
            self.applied = 0
            self.on_reset()
            self._apply_ready()
        self.on_orphans([e.cmd for e in removed if e.cmd.get("op") != "noop"])

    def _set_commit(self, c: int) -> None:
        if c <= self.commit:
            return
        self.commit = c
        if self.store:
            self.store.commit(c)
        self._apply_ready()

    def _apply_ready(self) -> None:
        while self.applied < self.commit:
            self.applied += 1
            self.apply_cb(self.applied, self.log[self.applied - 1].cmd)

    def replay(self) -> None:
        """Apply what was committed before a restart."""
        self._apply_ready()

    # -- the leader side ---------------------------------------------------------------

    def submit(self, cmd: dict) -> int | None:
        if not self.is_leader():
            return None
        self._append_local(Entry(self.term, cmd))
        self._replicate_all()
        self._advance_commit()
        return self.last_index()

    def _replicate(self, peer: str) -> None:
        prev = self.next_index[peer] - 1
        entries = self.log[prev:prev + MAX_BATCH]
        self.send(peer, {"type": "append", "term": self.term, "leader": self.me, "prev_index": prev,
                         "prev_term": self.term_at(prev), "entries": [[e.term, e.cmd] for e in entries],
                         "commit": self.commit})

    def _replicate_all(self) -> None:
        for p in self.peers:
            self._replicate(p)
        self.last_send = self.now()

    def _advance_commit(self) -> None:
        raise NotImplementedError

    # -- message handling ------------------------------------------------------------------

    def handle(self, src: str, m: dict) -> None:
        t = m.get("type")
        if t == "append":
            self._on_append(src, m)
        elif t == "append_reply":
            self._on_append_reply(src, m)
        else:
            self._on_other(src, m)

    def _yield_to(self, other: str) -> bool:
        """Two leaders in the same term (only in primary-backup): the lower id keeps leading."""
        return other < self.me

    def _on_append(self, src: str, m: dict) -> None:
        if m["term"] < self.term:
            self.send(src, {"type": "append_reply", "term": self.term, "success": False, "match": 0})
            return
        if m["term"] == self.term and self.is_leader() and not self._yield_to(src):
            self.send(src, {"type": "append_reply", "term": self.term, "success": False, "match": 0, "yield": True})
            return
        self._become_follower(m["term"], src)
        self.leader_heard = self.now()
        self._heard_leader()
        prev = m["prev_index"]
        if prev > self.last_index() or self.term_at(prev) != m["prev_term"]:
            self.send(src, {"type": "append_reply", "term": self.term, "success": False,
                            "match": 0, "hint": min(prev - 1, self.last_index())})
            return
        idx = prev
        for term, cmd in m["entries"]:
            idx += 1
            if idx <= self.last_index():
                if self.term_at(idx) == term:
                    continue
                self._truncate(idx)
            self._append_local(Entry(term, cmd))
        last_new = prev + len(m["entries"])
        if m["commit"] > self.commit:
            self._set_commit(min(m["commit"], last_new))
        self.send(src, {"type": "append_reply", "term": self.term, "success": True, "match": last_new})

    def _on_append_reply(self, src: str, m: dict) -> None:
        if m["term"] > self.term:
            self._become_follower(m["term"], None)
            return
        if not self.is_leader() or m["term"] != self.term or src not in self.next_index:
            return
        if m.get("yield"):
            self._become_follower(self.term, src)
            return
        self.last_reply[src] = self.now()
        if m["success"]:
            self.match_index[src] = max(self.match_index[src], m["match"])
            self.next_index[src] = self.match_index[src] + 1
            self._advance_commit()
            if self.next_index[src] <= self.last_index():
                self._replicate(src)
        else:
            hint = m.get("hint", self.next_index[src] - 2)
            self.next_index[src] = max(1, min(self.next_index[src] - 1, hint + 1))
            self._replicate(src)

    def _on_other(self, src: str, m: dict) -> None:
        pass

    def _heard_leader(self) -> None:
        pass

    def tick(self) -> None:
        raise NotImplementedError

    def describe(self) -> dict:
        return {"kind": self.kind, "role": self.role, "term": self.term, "leader": self.leader,
                "commit": self.commit, "last_index": self.last_index()}


class Raft(Replica):
    kind = "raft"

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        lo, hi = self.cfg.get("election_timeout_real_s", [1.0, 2.0])
        self.et_lo, self.et_hi = float(lo), float(hi)
        self.votes: set[str] = set()
        self._reset_deadline()
        if not self.peers:
            self.deadline = self.now()  # a single station elects itself at once

    def _reset_deadline(self) -> None:
        self.deadline = self.now() + self.rng.uniform(self.et_lo, self.et_hi)

    def _heard_leader(self) -> None:
        self._reset_deadline()

    def _up_to_date(self, last_term: int, last_index: int) -> bool:
        mine = (self.term_at(self.last_index()), self.last_index())
        return (last_term, last_index) >= mine

    def tick(self) -> None:
        now = self.now()
        if self.is_leader():
            if now - self.last_send >= self.hb:
                self._replicate_all()
            # Leader that cannot reach a majority steps down: no leader on the minority side.
            reachable = 1 + sum(1 for p in self.peers if now - self.last_reply.get(p, -1e9) <= self.et_hi)
            if reachable < self.majority():
                self.on_event("lost_quorum", term=self.term)
                self._set_role("follower", None)
                self._reset_deadline()
        elif now >= self.deadline:
            self._start_election()

    def _start_election(self) -> None:
        self._set_term(self.term + 1, self.me)
        self._set_role("candidate", None)
        self.votes = {self.me}
        self._reset_deadline()
        if len(self.votes) >= self.majority():
            self._become_leader()
            return
        for p in self.peers:
            self.send(p, {"type": "vote_request", "term": self.term, "candidate": self.me,
                          "last_index": self.last_index(), "last_term": self.term_at(self.last_index())})

    def _on_other(self, src: str, m: dict) -> None:
        t = m.get("type")
        if t == "vote_request":
            if m["term"] > self.term:
                self._become_follower(m["term"], None)
            grant = (m["term"] == self.term and self.voted_for in (None, m["candidate"])
                     and self._up_to_date(m["last_term"], m["last_index"]))
            if grant:
                self._set_term(self.term, m["candidate"])
                self._reset_deadline()
            self.send(src, {"type": "vote_reply", "term": self.term, "granted": grant})
        elif t == "vote_reply":
            if m["term"] > self.term:
                self._become_follower(m["term"], None)
                return
            if self.role == "candidate" and m["term"] == self.term and m["granted"]:
                self.votes.add(src)
                if len(self.votes) >= self.majority():
                    self._become_leader()

    def _advance_commit(self) -> None:
        for n in range(self.last_index(), self.commit, -1):
            if self.term_at(n) != self.term:
                break  # only entries from the current term are committed by counting
            if 1 + sum(1 for p in self.peers if self.match_index.get(p, 0) >= n) >= self.majority():
                self._set_commit(n)
                break


class PrimaryBackup(Replica):
    kind = "primary_backup"

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.takeover = float(self.cfg.get("pb_takeover_real_s", 1.5))
        self.leader_heard = self.now()
        self._start_as_primary = False
        if self.term == 0:
            # Fixed initial primary: the lowest id, epoch 1. It takes the role on its
            # first tick (the owner may not be ready to send or log during construction).
            self._set_term(1, None)
            primary = min([self.me] + self.peers)
            self._start_as_primary = primary == self.me
            self.leader = primary if primary != self.me else None

    def _reachable(self, p: str) -> bool:
        return self.now() - self.last_reply.get(p, -1e9) <= self.takeover

    def tick(self) -> None:
        now = self.now()
        if self._start_as_primary:
            self._start_as_primary = False
            if self.role != "leader" and self.term == 1:
                self._become_leader()
        if self.is_leader():
            if now - self.last_send >= self.hb:
                self._replicate_all()
            self._advance_commit()  # a backup that went silent no longer holds commits up
            return
        # Backups take over in a fixed order, so they do not all jump at once.
        order = [p for p in sorted([self.me] + self.peers) if p != self.leader]
        rank = order.index(self.me) if self.me in order else 0
        if now - self.leader_heard > self.takeover * (1 + rank):
            self._set_term(self.term + 1, None)
            self.on_event("takeover", term=self.term, previous=self.leader)
            self._become_leader()

    def _advance_commit(self) -> None:
        live = [p for p in self.peers if self._reachable(p)]
        for n in range(self.last_index(), self.commit, -1):
            if all(self.match_index.get(p, 0) >= n for p in live):
                self._set_commit(n)
                break


def make_replica(kind: str, *args, **kwargs) -> Replica:
    if kind == "raft":
        return Raft(*args, **kwargs)
    if kind == "primary_backup":
        return PrimaryBackup(*args, **kwargs)
    raise ValueError(f"unknown consensus {kind!r}")
