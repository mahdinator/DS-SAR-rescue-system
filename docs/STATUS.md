# Build status and decisions (handoff notes)

Living notes for whoever continues the build. The full design is the architecture
document: https://claude.ai/artifact/Xc5THQ6wpjuKpPTRhBFirK . This file records what
is built, what was decided while building (some of it differs from or adds to the
document), measured results, and what comes next.

## How to work with it

```powershell
.venv\Scripts\python -m pytest
.venv\Scripts\python -m sar.launcher.run scenarios/demo.yaml
```

`demo.yaml` runs everything slowly (20 real minutes) with the operator UI on every station:
http://127.0.0.1:8080/ (gs-1), 8081 (gs-2), 8082 (gs-3). Type your name as `commander`
(you start in command) or `analyst`. Close the commander's page and within ~10 s the
leader offers command to the bot ic-2 (acting commander).

- Every run serves the **control center** at http://127.0.0.1:8090/ (faults, kill/restart, observer view).
- `--set key.path=value` overrides any scenario value; `--name x` suffixes the run name;
  `--set base_port=5650` lets runs go in parallel (control center then on 8091).
- Results: `runs/<time>_<name>/summary.json`, `events.jsonl`, `logs/`, `state/` (station logs).
- The report (`sar/tools/report.py`) scores everything against ground truth from the world server's `detection` events.

## Done

| Step | Summary | Scenario |
| --- | --- | --- |
| 1 | Minimum loop: world server, drones, station, bot operator, logger, launcher | `step1.yaml`, `step1_high_fa.yaml` |
| 2 | Emulated radio (receiver-side link model), scripted faults, relay, outbox with acks/retries, failure detector, lost-link failsafe | `step2.yaml` |
| 3 | Dedup (derived clusters), review queue with claim leases and compare-and-set, drone-side suppression of known victims, decoys, evidence thumbnails, web review UI | `step3.yaml`, `step3_demo.yaml` |
| — | Control center: live faults, kill/restart, observer dashboard; launcher job object | any |
| 4 | Replicated stations: Raft and primary-backup, forwarding to the leader, anti-entropy for sightings/coverage, provisional verdicts, orphan merge, persistence and recovery | `step4.yaml` |
| 5 | Sortie-sized jobs, leases with fencing tokens and battery deadlines, leftover jobs, revoke, lost-aircraft alert, POA x POD tasking with re-search, boost near confirmed victims, inspect jobs | `step5.yaml` |
| 7 | Chain of command: roster and roles, permissions checked at apply time, handover with acceptance, succession to an acting IC when the IC is silent, priority zones, lost-aircraft alerts and acknowledgement, conflict settling, audit trail; commander bot | `step7.yaml` |
| UI | Dark theme for both pages. Operator UI per station (each station's own view): map with terrain or remaining-probability layer, jobs, drone trails, zones; tabs Review (with closer look), Drones (battery, job progress, lease), Stations (replication, peers, jobs, swap queue), Command (IC, offers with accept/decline, alerts, disputed verdicts, zones, handover, audit trail) | `demo.yaml` |
| 6 | Battery drain in the air, bingo (way home + reserve), swap queue at base (one at a time, first come first served), naive vs staggered launch, leftover work first | `step6.yaml` |

Tests: 81 passing (`tests/`), including a simulated-network consensus suite and a 60-schedule Raft fuzz (run ad hoc).

## Decisions made while building (beyond or different from the document)

**Simulation**
- World randomness depends on (seed, cell, mode, n-th pass), not on which drone flew: assignment timing varies between runs and must not change what is found. Victim and false-alarm rolls use separate streams.
- Default false-alarm rates were halved (avg ~1.1% per pass); `step1_high_fa.yaml` keeps the old ones as a stress test. Transit passes have half the detection *and* half the false-alarm rate.
- **Decoys**: fixed look-alike objects detected on every pass (POD x 0.6), so repeat false alarms exist. Thumbnails: person shape (victim), round lump (decoy), random blob (one-off).
- Evidence crops are simulated 32 x 32 thermal images (person = head, body, limbs in a random pose; decoy = warm lump without limbs; one-off = scattered small spots). Poor views (low terrain POD) lower contrast and blur them. The UI shows them in an ironbow palette with a reference card (`/api/examples`). Tests that only count detections switch image drawing off (`sensor.make_thumbs`).
- Bot operator judges against ground truth with radius = 2.5 x terrain sigma (fixed radius misjudged forest).
- `planner.passes` (grid planner only) queues every sector again at half priority: the step-1 stand-in for re-search.
- Steps 1-4 scenarios now say `battery: {enabled: false}` and `planner.kind: grid` (static strategy), so their measured numbers still reproduce.

**Messaging and radio**
- Envelope carries `hop` (relaying transmitter) and `attempt` (retry number); loss is decided per (transmitter, receiver, msg, ttl, attempt), so retries and relayed copies are independent tries.
- Relay rule is geographic (toward/away from a station), not flooding.
- Message, sighting and sensing counters start at `incarnation x 10^9`, so a restarted node never reuses ids (otherwise its messages are dropped as duplicates).
- Node subclasses must not reuse the attribute name `seen` (it once silently broke delivery; the node now refuses to start).

**Review**
- Operators **pull** work (claim one cluster at a time); the bot works sequentially, so backlog shows as queue wait.
- Near-rejected objects come back as low-priority **recheck**, never dropped.
- Verdicts are stored as a decisions map (sighting -> decision), separate from sightings, so a replicated decision can arrive before its sighting.
- Merged verdicts never refuse: fill undecided, record conflicts, **confirm wins**.

**Replication (step 4)**
- Sightings and coverage: add-only sets, accepted by any station, synced by anti-entropy (AP). Assignments, completions, verdicts: the replicated log (CP). Claims: leader-only soft state (lost on failover).
- Any station accepts operations; a non-leader **forwards** to the leader; with no leader reachable, verdicts become **provisional** and are merged later. Sector operations wait instead.
- Entries a station must discard (orphans) are re-submitted as merged commands (verdicts, completions); assignments are dropped.
- Drones and bots follow the leader with the highest epoch from station heartbeats; drones ignore assignments from an older epoch; queued messages are re-addressed to the new leader (same msg_id).
- Bots can be pinned to a station (`station: gs-2`), like an analyst at that terminal.
- Raft leader steps down when it cannot reach a majority. Primary-backup commits once every *reachable* backup has the entry.
- Station state persists to `runs/<run>/state/<station>.jsonl` and is replayed on restart.

**Planning (step 5)**
- Sortie planner halves the area until each piece fits one battery from base (and `max_cells`). On the 1 km map all pieces end up ~150 cells; the distance effect only shows on larger maps (tested on 1 x 4 km).
- Jobs need not fit whole: a job is offered if the drone can do at least `min_search_s` of it and get home. The drone searches until bingo and gives the rest back. (Whole-job fitting wasted ~40% of each battery.)
- The leader puts everything replicas need into the command (job definition for new jobs, lease deadline), so applying is deterministic; the *choice* uses the leader's own POA map (AP data), which is fine because only the leader chooses.
- Lease = (drone, token, deadline). Token: a counter in the replicated pool, +1 per assignment. Deadline = assignment + battery left + grace: by then the drone is certainly down, so no renewals. The drone also turns back early enough to be home before the deadline (a lease holder stops before expiry).
- Expiry puts the job back from the last progress the stations heard in telemetry. If the drone is suspected at expiry: `lost_aircraft` alert.
- Old tokens are refused (completion, release). A late completion is accepted only while nobody else holds the job. Revoke is sent only on positive evidence (newer lease, job done, expired), never because the leader has not applied an assignment yet, and never for a lease the drone closed itself.
- Strategies: greedy (re-search competes with first-pass work by POA x POD / time), sweep_first, static (old). Inspect jobs are created by the leader for clusters in recheck/conflict and cancelled when decided.
- Victims are placed by POA but not in groups, so the boost near confirmed victims has little to find in this world; that is honest and goes into the trade-offs.

**Battery (step 6)**
- No landing-pad leases (user decision): multirotors land anywhere; the real limit is the crew swapping batteries. Swaps are a FIFO queue at base, one at a time, held by the leader as soft state (drones repeat their request, so a new leader rebuilds it). With no station reachable, the crew swaps anyway.
- Predictive relief dropped: with a fixed fleet it needs an idle standby drone. Instead, work a drone gave back gets `leftover_boost` priority.
- Battery drains whenever the drone is in the air, including hovering while waiting for an answer; a drone with no answer heads home instead of hovering.

**Chain of command (step 7)**
- Command state lives in the replicated log, so every permission is checked when the command is *applied*: a command submitted just before a handover commits is refused on every station alike (unit test; seen in the run as ic-1's zone refused after handing over).
- Handover is two-step (offer, accept); the old IC keeps command until the other accepts.
- Succession: presence messages (every person, twice a second) reach every station; the leader offers command to the next present person in the succession list when the IC is silent for `absence_timeout_real_s`. Accepting makes them acting IC. Absence is soft state; the offer and acceptance are in the log.
- Analysts' verdicts are refused at apply if the person is not an analyst or commander. Empty roster = no checks (steps 1-6).
- Priority zones multiply the value of cells in the zone (POA x POD x weight) in the leader's tasking.
- Conflicting verdicts: the commander bot settles them as confirmed (policy: a possible person is checked by a ground team).
- Command actions need a leader (CP). With no leader reachable they wait: no "acting commander for a cut-off team" (dropped to keep it simple; a trade-off to mention).
- With no roster, anyone may take command actions (the UI works in old scenarios).
- UI people send presence every 2 s; the station re-broadcasts it, so the leader can judge the IC's absence wherever the IC's terminal is.

## Measured so far (single runs, seed 42; step 8 will add seeds)

- Step 2 (faults): without acks 15/47 sightings never arrived and the station's coverage view was 67%; with acks 2/42 lost, worst delay 13 mission minutes.
- Step 3: with drone suppression off, no dedup put the same victim in front of operators 11 times; full dedup 2. Zero wrong merges, zero wrong suppressions.
- Step 4 (same fault script): Raft (3) had 10 s with two leaders (isolated leader stepping down), 0 committed entries lost, replicas consistent. Primary-backup (2) had **653 s with two leaders** and **7 committed entries overwritten** (re-submitted and merged). Commit latency ~31 ms both.
- Step 5 (4 drones, 3 stations, drone-3 killed mid-search at 6.7 min, drone-2 isolated 7 min): greedy POA x POD tasking found 16/25 victims, half of them by 58 min; the old fixed grid (`--set planner.kind=grid --set planner.strategy=static`) found 13, half by 73 min, but covered more ground (97% vs 90%): re-search of likely areas versus covering everything once. The dead drone's lease expired at its battery deadline, 65 unsearched cells went back to the pool, `lost_aircraft` raised; no other lease expired, 0 revokes, replicas consistent.
- Step 7: the scripted story ran as planned: zone at 10 min (the next three jobs went into or next to it), handover offered at 25 min and accepted at 26, ic-1's later zone refused at apply, ic-2 killed at 40 min, command offered to ic-1 and accepted as acting IC at 43. Lost-aircraft alert acknowledged 38 s after it was raised. 0 errors, replicas consistent.
- Step 6 (5 drones, 4-min swaps): naive launch: swap queue peaked at 4, longest wait 15 min, 5.1 min with no drone searching. Staggered: longest wait 2 min, queue peak 1, never without a searching drone. Victims found similar on one seed (17 vs 18).

**Experiments (step 8)**
- `python -m sar.tools.experiments` runs 6 experiments x variants x seeds (10 seeds = 150 runs, about 35 min with --parallel 8) at clock x50, into `results/experiments/` (runs.csv, summary.md, charts). Findings and trade-offs: [docs/RESULTS.md](RESULTS.md).
- Mission clock is pausable and its speed adjustable from the control center (timeline published on the control channel; network protocols keep real time). Drone movement uses half-cell sub-steps so results do not depend on clock speed.

## Next

1. Walk the user through the architecture and main decisions; run the demo together.
2. Update the architecture document (artifact) with what changed while building; then the slides.

Not yet done from the design: Cristian clock sync (single machine so far), multi-machine/docker deployment (sockets bind 127.0.0.1), phi-accrual detector.

After the build: walk the user through the architecture and main decisions, run a demo, update the architecture document, then slides.
