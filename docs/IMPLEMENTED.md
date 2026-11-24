# What is implemented

A distributed multi-drone search-and-rescue system in Python, modelled on the AUSPEX
paper (Döschl, Sommer, Kiam, 2025): drones search a disaster area, report possible
victims, and people at ground stations confirm or reject each sighting. The world is
simulated; every drone, ground station and operator is a separate process talking
over the network. Measured results: [RESULTS.md](RESULTS.md). Build notes and
decisions: [STATUS.md](STATUS.md).

## Simulation

- Seeded world: terrain regions (rubble, damaged buildings, roads, forest, open field,
  water), victims placed by terrain likelihood, decoys (debris that looks like a person).
- Simulated camera: detection and false-alarm chances per terrain, position error,
  appearance signature, and a 32 x 32 thermal-style image for each sighting.
- Randomness depends on the map cell and pass, not on which drone flew it, so runs are
  comparable.

## Network and messaging

- Emulated radio: range, packet loss, latency, jamming zones, cut links, blackouts;
  the ground network between stations and operators is separate.
- Relaying through other drones toward the stations.
- Acknowledgements, retries with back-off, and store-and-forward: drones keep messages
  until a station confirms them.
- Duplicate suppression by message id; restart-safe ids.
- Failure detector (timeouts) and a link-loss failsafe: keep searching, fly toward base
  when work runs out.

## Ground stations (replicated)

- Raft or primary-backup (selectable): one replicated log for assignments, completions,
  verdicts and command decisions; any station accepts requests and forwards them to the
  leader.
- Sightings and coverage spread between stations by anti-entropy (available during
  partitions).
- Provisional verdicts when no leader is reachable, merged later; discarded entries
  re-submitted.
- State saved to disk and recovered after a restart; consistency checkpoints.

## Sightings and review

- Deduplication: sightings grouped by position and appearance (none / position /
  position + signature selectable).
- Review queue with claim leases (two operators never review the same object) and
  compare-and-set verdicts; doubtful objects come back for a recheck.
- Drones stop reporting victims already confirmed.
- Bot analyst for experiments; people review in the web UI.

## Planning and battery

- Sortie-sized search jobs (each fits one battery), leased to drones with fencing tokens
  and battery-based deadlines; jobs of lost drones come back automatically, with a
  lost-aircraft alert.
- Tasking by probability of finding someone (POA x POD) with re-search; strategies
  greedy, sweep first and fixed grid; boost near confirmed victims; "closer look" jobs.
- Battery drain, return when the battery reaches the way home plus a reserve, unfinished
  work handed back first; one battery-swap queue at base; naive or staggered launch.

## Chain of command

- Roles (commander, analyst, observer) checked when a decision is applied, so every
  station decides the same way.
- Handover of command with acceptance; succession to an acting commander when the
  commander goes silent.
- Priority zones, lost-aircraft acknowledgement, settling disputed verdicts, audit trail.

## Interfaces and tools

- Launcher: starts every process of a scenario (YAML), scripted faults, `--set` overrides.
- Control center (web): live map of truth vs. what stations believe, inject faults,
  kill and restart nodes, pause the mission or change its speed, event feed, live metrics.
- Operator UI per station (web): map, review with thermal images, drones, stations,
  command; each station shows its own view.
- Run report scored against ground truth; experiment runner (variants x seeds, in
  parallel) producing tables and charts.
- 81 automated tests, including a simulated-network consensus test suite.
