"""Event collector: a PULL socket that writes every event as one JSON line."""
from __future__ import annotations

import json
from pathlib import Path

import msgpack
import zmq

from ..common.config import Runtime
from ..common.node import node_args


def main() -> None:
    args = node_args()
    rt = Runtime.load(args.runtime)
    ctx = zmq.Context()
    sock = ctx.socket(zmq.PULL)
    sock.setsockopt(zmq.RCVHWM, 100000)
    sock.bind(rt.logger_addr)
    out = Path(rt.run_dir) / "events.jsonl"
    with open(out, "a", encoding="utf-8", buffering=1) as f:  # line-buffered: survives a hard kill
        try:
            while True:
                rec = msgpack.unpackb(sock.recv(), raw=False)
                f.write(json.dumps(rec, default=str) + "\n")
        except KeyboardInterrupt:
            pass


if __name__ == "__main__":
    main()
