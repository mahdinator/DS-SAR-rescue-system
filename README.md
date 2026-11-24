# Distributed multi-UAV search and rescue

A distributed system in Python that coordinates a fleet of search-and-rescue drones,
with a human confirming every victim sighting. Every drone, ground station and service
is a separate process; only the environment (terrain, victims, camera, radio) is simulated.

Architecture document: https://claude.ai/artifact/Xc5THQ6wpjuKpPTRhBFirK

## Setup

```powershell
python -m venv .venv
.venv\Scripts\python -m pip install -r requirements.txt
```

## Run a scenario

```powershell
.venv\Scripts\python -m sar.launcher.run scenarios/step1.yaml
```

Each run writes to `runs/<timestamp>_<name>/`:

- `events.jsonl`: every event from every node (the logging service's output)
- `logs/<node>.log`: each process's console output
- `summary.json`: the metrics printed at the end, measured against ground truth

Re-print a summary: `.venv\Scripts\python -m sar.tools.report runs/<run directory>`

Override any scenario value for an experiment, and run several at once on different ports:

```powershell
.venv\Scripts\python -m sar.launcher.run scenarios/step3.yaml --name dedup_none --set dedup.kind=none --set base_port=5650
```

## Control center (faults and the observer view)

Every run serves a control center at http://127.0.0.1:8090/ (the launcher prints the address;
parallel runs on other `base_port`s get 8091, 8092, ...). It shows the whole run as only an outside
observer can see it: true drone positions, every radio link's delivery rate, real victims and decoys
next to what the station believes, an event feed and live metrics. From it you can:

- black out a node, cut one link, or jam an area of the map (click to place), for a set number of mission minutes
- change the radio loss everywhere
- kill and restart any drone, station or operator process

Live faults reach the nodes over a control channel and go into the same rulebook as scripted faults.
Child processes never outlive the launcher, even if it is killed. Use `--no-control` to skip the dashboard.

## Review sightings in the browser

```powershell
.venv\Scripts\python -m sar.launcher.run scenarios/step3_demo.yaml
```

Open http://127.0.0.1:8080/ , type your name, then **Claim next (N)**, look at the evidence images,
and **Confirm (C)** or **Reject (R)**. A bot analyst works the same queue; nobody is offered a
cluster someone else holds.

## Tests

```powershell
.venv\Scripts\python -m pytest
```

## Layout

| Package | Role |
| --- | --- |
| `sar/common` | Messages and envelope, config, clocks, bus, node base class, interfaces |
| `sar/world` | World server: seeded terrain, victims, simulated sensor, ground truth |
| `sar/groundstation` | Sortie planner, leased job pool, tasking (POA x POD), battery swap queue, sightings and verdicts |
| `sar/drone` | Flight, lawnmower search, sensing, battery and bingo, reporting |
| `sar/transport` | Emulated radio: link model (range, loss, latency) and fault script |
| `sar/messaging` | Duplicate suppression, outbox with acks and retries, relay rule |
| `sar/membership` | Failure detector |
| `sar/consensus` | Replicated log: Raft and primary-backup, persistence |
| `sar/operator` | Bot analyst and commander bot (stand in for people) |
| `sar/ui` | Web review UI served by a ground station |
| `sar/control` | Control center: process control, live faults, observer dashboard (served by the launcher) |
| `sar/logger` | Event collector |
| `sar/launcher` | Starts and stops every process of a scenario |
| `sar/tools` | Run report |

## Build status

Detailed status, decisions made while building, and measured results: [docs/STATUS.md](docs/STATUS.md). Short feature list: [docs/IMPLEMENTED.md](docs/IMPLEMENTED.md). Experiment findings: [docs/RESULTS.md](docs/RESULTS.md).

1. Minimum loop: world, drones, one ground station, bot operator, static sectors, logging. **Done.**
2. Radio emulator, relay, failure detector, outbox. **Done.**
3. Deduplication and the web review queue. **Done.**
   Control center for faults and observation. **Done.**
4. Ground-station replication (primary-backup and Raft). **Done.**
5. Sortie planner, leases with fencing tokens, POA x POD tasking with re-search. **Done.**
6. Battery, bingo, swap queue at base, staggered launch. **Done.**
7. Chain of command: roles, handover with acceptance, succession, priority zones, alerts, audit. **Done.**
   Operator UI redesign (per-station ops screen). **Done.**
8. Experiment runner, tables and charts (`python -m sar.tools.experiments`; findings in [docs/RESULTS.md](docs/RESULTS.md)). **Done.**
