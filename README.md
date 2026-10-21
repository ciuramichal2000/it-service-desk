# it-service-desk

A reference implementation of an IT service desk, written to show how I design this kind of system. It is **new sample code, not the code of any client project**.

- Tickets with **priorities and SLA deadlines** (critical 4 h, high 8 h, medium 24 h, low 72 h) and a full **change history** (who changed what, when).
- **Roles:** requesters see only their own tickets; agents see everything, change status along allowed transitions (`new → open ⇄ pending → resolved → closed`) and assign tickets to other agents. A forbidden action is refused, not ignored.
- A **notification outbox** (new ticket, assignment, status change) that a mail sender can drain; marking as sent is idempotent.
- An **SLA report**: breached, at risk (under 25% of the time left) and met.

```python
desk = Desk("desk.db")
desk.add_user("ann", "requester"); desk.add_user("sam", "agent")
t = desk.create("ann", "VPN down", priority="high")
desk.set_status("sam", t, "open")
print(desk.sla_report())
```

SQLite and the Python standard library only; the clock is injectable, so SLA behaviour is tested without waiting. `python -m pytest` runs the tests. Built with AI coding assistance.
