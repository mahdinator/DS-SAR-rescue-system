"""Step 7: chain of command, checked when commands are applied (log order)."""
from sar.groundstation.command import CommandState

CFG = {"roster": [{"id": "ali", "role": "commander"}, {"id": "sara", "role": "commander"},
                  {"id": "omar", "role": "analyst"}, {"id": "nour", "role": "observer"}],
       "initial": "ali", "succession": ["ali", "sara"]}


def cmd(by, action, **args):
    return {"by": by, "action": action, "args": args}


def run(state, *cmds):
    return [state.apply(i, c) for i, c in enumerate(cmds, start=1)]


def test_only_the_commander_sets_priority_zones():
    s = CommandState(CFG)
    (ok1, _), (ok2, reason) = run(s, cmd("ali", "zone", x=0, y=0, radius_m=50, weight=3),
                                  cmd("omar", "zone", x=0, y=0, radius_m=50, weight=3))
    assert ok1 and not ok2 and "not in command" in reason
    assert s.zone_weight(10, 10) == 3 and s.zone_weight(500, 500) == 1


def test_handover_needs_acceptance():
    s = CommandState(CFG)
    run(s, cmd("ali", "transfer", to="sara"))
    assert s.commander == "ali"  # still in command until sara accepts
    assert not run(s, cmd("omar", "accept"))[0][0]  # the offer is not for omar
    run(s, cmd("sara", "accept"))
    assert s.commander == "sara" and not s.acting


def test_command_submitted_before_a_handover_is_refused_if_applied_after_it():
    """ali submits a zone just before sara's acceptance commits. Every replica applies
    the log in the same order, so every replica refuses ali's zone the same way."""
    results = []
    for _ in range(3):  # three replicas
        s = CommandState(CFG)
        results.append(run(s, cmd("ali", "transfer", to="sara"), cmd("sara", "accept"),
                           cmd("ali", "zone", x=0, y=0, radius_m=50, weight=3)))
        assert s.zones == {}
    assert results[0] == results[1] == results[2]
    assert results[0][2][0] is False


def test_succession_makes_an_acting_commander():
    s = CommandState(CFG)
    run(s, cmd("system", "succession", to="sara", absent="ali"), cmd("sara", "accept"))
    assert s.commander == "sara" and s.acting
    assert not run(s, cmd("system", "succession", to="omar"))[0][0]  # analysts cannot hold command


def test_alerts_are_acknowledged_once_and_only_by_the_commander():
    s = CommandState(CFG)
    s.raise_alert("lost:drone-3:4", "lost_aircraft", drone="drone-3")
    (a, _), (b, _), (c, reason) = run(s, cmd("omar", "ack", alert="lost:drone-3:4"),
                                      cmd("ali", "ack", alert="lost:drone-3:4"),
                                      cmd("ali", "ack", alert="lost:drone-3:4"))
    assert (a, b, c) == (False, True, False) and "already" in reason


def test_reviewing_needs_the_analyst_or_commander_role():
    s = CommandState(CFG)
    assert s.may_review("omar") and s.may_review("ali") and not s.may_review("nour")
    assert CommandState({}).may_review("anyone")  # no roster: no checks


def test_every_action_is_in_the_audit_trail_including_refusals():
    s = CommandState(CFG)
    run(s, cmd("nour", "zone", x=0, y=0, radius_m=1, weight=2), cmd("ali", "transfer", to="sara"))
    assert [(a["by"], a["action"], a["ok"]) for a in s.audit] == [("nour", "zone", False), ("ali", "transfer", True)]
