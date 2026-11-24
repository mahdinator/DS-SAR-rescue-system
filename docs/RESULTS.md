# Experiment results and what they mean

Seven experiments, each a design decision tested as an A/B comparison: same scenario,
one setting changed, ten seeds (42-51; each seed is a different map, different victim
positions and different random rolls). Every number is measured against ground truth
from the event log. Full tables, per-run data and charts:
[results/experiments/README.md](../results/experiments/README.md).

Reproduce (180 runs at clock x50; 8-12 at a time on a 16-core laptop showed no timing
effects, about 30 minutes; finished runs are reused, so adding seeds only runs the new ones):

```powershell
.venv\Scripts\python -m sar.tools.experiments --seeds 10 --parallel 8
.venv\Scripts\python -m sar.tools.experiments tasking --seeds 10 --parallel 8
```

Numbers are the mean over the 10 seeds. Where the spread between seeds is larger than
the difference between variants, it says so. Running 8 missions at once did not disturb
the timing: in every 3-station scenario without station faults, each run had exactly
one election and no time without a leader.

---

## 1. Acknowledgements and retries (messaging)

| | no acks | acks + retries |
|---|---|---|
| Sightings never delivered (of ~49) | **14.4** | **0.6** |
| Station's coverage view | 68% | 91% |
| Delivery delay, 95th percentile | 3.9 s | 528 s |
| Victims confirmed | 10.6 | 13.6 |

**Finding.** Without acknowledgements, over a quarter of all sightings and a third of
the coverage reports are lost when links fail (isolation, jamming, a dead drone). With
acks, retries and store-and-forward, almost everything arrives, and 3 more victims per
mission get confirmed.

**Trade-off.** The 95th-percentile delay *rises* from seconds to about nine minutes.
That is not acks making things slower: without acks the messages sent during an outage
are simply lost, so only the fast ones are counted. With acks they wait in the outbox
and arrive when the link comes back. A late sighting beats a lost one: the person is
still there.

## 2. Worse radio (radio_loss)

| base packet loss | 2% | 15% | 30% |
|---|---|---|---|
| Retransmissions | 235 | 302 | 402 |
| Packets relayed | 1111 | 875 | 693 |
| Sightings never delivered | 0.7 | 2.1 | 1.7 |
| Delivery delay, median | 2.1 s | 7.8 s | 13.7 s |
| Victims confirmed | 14.1 | 12.8 | 11.6 |

**Finding.** Retries absorb most of the loss: retransmissions rise by 70%, the median
delay grows from 2 to 14 s, and only about 2 of ~49 sightings are lost even at 30%.
Relaying falls because fewer relayed copies get through. Slightly fewer victims are
confirmed, because sightings reach the operators later.

**Limit.** The 95th-percentile delay (not shown) varies more between seeds than between
loss levels: this scenario's scripted outages (a 5-minute isolation, a jam) dominate
the worst cases, not the background loss.

## 3. Deduplication (dedup)

Drone-side suppression of known victims was switched off, so this compares the
station's clustering alone.

| | none | position | position + signature |
|---|---|---|---|
| Operator reviews | 129 | **105** | 116 |
| Same victim reviewed again | 14.5 | 1.1 | 1.2 |
| Two people merged into one | 0 | **2 cases** in 10 seeds | **0** |

**Finding.** Clustering removes almost all repeat reviews of the same victim (14.5 to
about 1) and cuts operator work by 10-20%.

**Trade-off.** Position alone saves the most work but twice merged two different people
into one object: one of them would never have been reviewed separately. Adding the
appearance signature prevented that in every seed, at the cost of about 11 more reviews.
For SAR the safer choice wins: an extra review costs half a minute, a hidden person can
cost a life. Repeat reviews of the same decoy stay (~9) by design: something seen again
near a rejected object comes back for a recheck instead of being silently dropped.

## 4. Replication: Raft vs primary-backup (replication)

Same fault script: a station cut off for 6 minutes, a station crashing and restarting,
the link between two stations breaking.

| | Raft (3 stations) | primary-backup (2) |
|---|---|---|
| Time with two leaders | 12 s | **580 s** |
| Time without a leader | 24 s | 29 s |
| Committed decisions overwritten | **0** | **17** |
| Replicas disagreeing at the end | 0 | 0 |
| Commit latency | 30 ms | 30 ms |

**Finding.** Primary-backup has a split brain of nearly 10 minutes in every seed: when
the two stations cannot hear each other, each acts as primary, and 17 decisions that had
been reported as committed are later overwritten. Our merge rules re-apply them, so the
replicas agree in the end and nothing is silently lost, but that is repair after the
damage. Raft never loses a committed decision: a station can only commit with a
majority (2 of 3), so a cut-off station cannot. Its brief "two leaders" is an old leader
that has not yet noticed it lost its majority; it can no longer commit anything.

**Trade-off (CAP in practice).** Raft pays with about 24 s per mission without any
leader, during elections: it chooses consistency over availability. Primary-backup is
not more available in this script (about 29 s while the restarted station takes over)
and is inconsistent under partition. Same commit latency: the safety costs nothing in
normal operation, only a third station.

## 5. Where to search next (tasking)

| | greedy (POA x POD) | sweep first | fixed grid |
|---|---|---|---|
| Victims detected (of 25) | 16.9 | 16.4 | 16.1 |
| Minutes to detect half | 54.7 | 53.5 | 58.3 |
| Coverage | 92% | 99% | 97% |

Seed by seed, greedy found more victims than the grid in 7 of 10 seeds (fewer in 3);
sweep first in 4 of 10 (same in 3, fewer in 3).

**Finding (weak on this map).** On the 1 km x 1 km map the order of search hardly
matters: about one more person on average, and not on every seed. With three seeds it
had looked like +2; ten seeds showed most of that was the particular maps. The reason is
the map size: four drones cover nearly the whole area within the mission whatever order
they fly, so every strategy ends up searching almost everywhere.

### 5b. A map too big to cover (tasking_large)

Same drones and mission length on 1.5 km x 1.5 km (2.25 times the area, victims and
decoys scaled to keep the same density: 56 victims). Now only about half the area can
be searched in time, as in a real disaster.

| | greedy (POA x POD) | sweep first | fixed grid |
|---|---|---|---|
| Victims detected (of 56) | 19.3 | **20.5** | 15.8 |
| Victims confirmed | 16.4 | **17.1** | 12.8 |
| Coverage | 46% | 48% | 43% |

Seed by seed, both probability-driven strategies found more people than the grid in
9 of 10 seeds (fewer in 1). Sweep first found as many as or more than greedy in every seed.

**Finding (strong).** When the area is bigger than the flight time, searching the
likely, easy-to-search places first finds **22-30% more people** (+3.5 to +4.7), and the
gap grows the longer the mission runs (curve). This is the situation real teams face,
and the main argument for POA x POD tasking.

**Trade-off.** Greedy re-searches rubble before finishing unlikely areas; on both maps
sweep first did at least as well and leaves no area unsearched, so it is our
recommendation. Both depend on the estimate of where people are being right: a person
in an "unlikely" field is found late either way, which is why the commander can paint
priority zones from what the ground teams know.

## 6. Battery swaps: naive vs staggered launch (launch)

| | naive | staggered |
|---|---|---|
| Longest swap wait | **762 s** (13 min) | 235 s (4 min) |
| Most drones queued at once | 3.6 | 1.3 |
| Minutes with no drone searching | **6.0** | **0** |
| Victims detected | 19.2 | 19.2 |

**Finding.** Drones launched together run low together and queue at the single swap
point: one waits 13 minutes, and for 6 minutes nobody searches at all. This held on all
ten seeds. Spreading take-offs by one swap time removes the queue and the gaps.

**Trade-off.** The same number of victims is found by the end: staggering starts later
(fewer people in the first 20 minutes) and catches up. Its value is continuity: there is
always a drone in the air, which matters for keeping eyes on an area, not for the total.

---

## How these map to the problems we set out to solve

| Problem | Mechanism | Evidence |
|---|---|---|
| Radio link failure | acks + retries + store-and-forward, relay, failsafe | Exp. 1 and 2: strong |
| Duplicate sightings | clustering by position + signature, suppression of known victims | Exp. 3: strong |
| Ground station removal | Raft replication, forwarding, provisional verdicts | Exp. 4: strong |
| Area distribution | sortie-sized jobs, leases, POA x POD tasking | Exp. 5b: strong when the area exceeds flight time |
| Battery | bingo, swap queue, staggered launch, leftover work | Exp. 6: strong |
