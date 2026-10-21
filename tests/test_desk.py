import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from servicedesk import SLA_HOURS, Desk, DeskError, Forbidden, NotFound  # noqa: E402

H = 3600.0


class Clock:
    def __init__(self):
        self.t = 1_000_000.0

    def __call__(self):
        return self.t


def desk():
    c = Clock()
    d = Desk(clock=c)
    d.add_user("ann", "requester")
    d.add_user("bob", "requester")
    d.add_user("sam", "agent")
    d.add_user("kim", "agent")
    return d, c


def test_ticket_gets_a_deadline_from_its_priority_and_a_history_entry():
    d, c = desk()
    t = d.create("ann", "Printer jams", "Third floor", "high")
    row = d.get("sam", t)
    assert row["status"] == "new" and row["due"] == c.t + 8 * H and row["requester"] == "ann"
    assert [(h["field"], h["old"], h["new"]) for h in row["history"]] == [("status", None, "new")]
    assert set(SLA_HOURS) == {"critical", "high", "medium", "low"}


def test_validation_of_input_and_users():
    d, _ = desk()
    with pytest.raises(DeskError):
        d.create("ann", "  ")
    with pytest.raises(DeskError):
        d.create("ann", "x", priority="urgent")
    with pytest.raises(DeskError):
        d.add_user("ann", "agent")                       # duplicate name
    with pytest.raises(DeskError):
        d.add_user("zed", "admin")
    with pytest.raises(NotFound):
        d.create("ghost", "x")


def test_requesters_see_only_their_tickets_agents_see_all():
    d, _ = desk()
    a, b = d.create("ann", "A"), d.create("bob", "B")
    assert [t["title"] for t in d.list("ann")] == ["A"]
    assert {t["title"] for t in d.list("sam")} == {"A", "B"}
    with pytest.raises(Forbidden):
        d.get("ann", b)
    assert d.get("bob", b)["title"] == "B"
    with pytest.raises(NotFound):
        d.get("sam", 999)


def test_only_agents_change_status_and_only_along_allowed_transitions():
    d, _ = desk()
    t = d.create("ann", "VPN down")
    with pytest.raises(Forbidden):
        d.set_status("ann", t, "open")
    with pytest.raises(DeskError):
        d.set_status("sam", t, "resolved")               # new -> resolved is not allowed
    d.set_status("sam", t, "open")
    d.set_status("sam", t, "pending")
    d.set_status("sam", t, "resolved")
    d.set_status("sam", t, "closed")
    with pytest.raises(DeskError):
        d.set_status("sam", t, "open")                   # closed is final
    hist = [(h["old"], h["new"], h["actor"]) for h in d.get("sam", t)["history"]]
    assert hist == [(None, "new", "ann"), ("new", "open", "sam"), ("open", "pending", "sam"), ("pending", "resolved", "sam"), ("resolved", "closed", "sam")]


def test_assignment_rules_and_history():
    d, _ = desk()
    t = d.create("ann", "Laptop slow")
    with pytest.raises(Forbidden):
        d.assign("ann", t, "sam")
    with pytest.raises(Forbidden):
        d.assign("sam", t, "bob")                        # a requester cannot be an assignee
    d.assign("sam", t, "kim")
    row = d.get("kim", t)
    assert row["assignee"] == "kim" and row["history"][-1]["field"] == "assignee"


def test_notifications_go_to_the_right_people_and_can_be_marked_sent():
    d, _ = desk()
    t = d.create("ann", "Mail down", priority="critical")
    d.assign("sam", t, "kim")
    d.set_status("kim", t, "open")
    subjects = [(o["to_user"], o["subject"]) for o in d.outbox()]
    assert subjects == [("agents", "New critical ticket #1: Mail down"), ("kim", "Ticket #1 assigned to you"), ("ann", "Ticket #1 is now open")]
    d.mark_sent(1)
    assert len(d.outbox()) == 2 and len(d.outbox(only_unsent=False)) == 3


def test_sla_report_breached_at_risk_and_met():
    d, c = desk()
    late = d.create("ann", "never answered", priority="critical")        # due in 4 h
    risky = d.create("ann", "almost late", priority="high")              # due in 8 h
    fine = d.create("ann", "plenty of time", priority="low")             # due in 72 h
    quick = d.create("ann", "fixed in time", priority="medium")          # due in 24 h
    slow = d.create("ann", "fixed too late", priority="critical")
    for tid in (quick, slow):
        d.set_status("sam", tid, "open")
    c.t += 1 * H
    d.set_status("sam", quick, "resolved")
    c.t += 4 * H                                                         # now 5 h after creation
    d.set_status("sam", slow, "resolved")
    c.t += 2.5 * H                                                       # 7.5 h: 'almost late' has 30 min of 8 h left
    rep = d.sla_report()
    assert rep["breached"] == [late, slow] and rep["at_risk"] == [risky] and rep["met"] == 1
    assert fine not in rep["breached"] + rep["at_risk"]


def test_reopening_a_resolved_ticket_clears_its_resolution_time():
    d, c = desk()
    t = d.create("ann", "flaky", priority="low")
    d.set_status("sam", t, "open")
    d.set_status("sam", t, "resolved")
    d.set_status("sam", t, "open")
    assert d.get("sam", t)["resolved"] is None and d.sla_report()["met"] == 0
