"""IT service desk: tickets with priorities and SLA deadlines, role-based access (requester / agent), a full change history,
an outbox of notifications and an SLA report. SQLite, standard library only."""
import json
import sqlite3
import time
from typing import Callable, Dict, List, Optional

SLA_HOURS = {"critical": 4, "high": 8, "medium": 24, "low": 72}
TRANSITIONS = {"new": {"open", "closed"}, "open": {"pending", "resolved"}, "pending": {"open", "resolved"}, "resolved": {"closed", "open"}, "closed": set()}
ROLES = ("requester", "agent")


class DeskError(Exception):
    pass


class Forbidden(DeskError):
    pass


class NotFound(DeskError):
    pass


SCHEMA = """
CREATE TABLE IF NOT EXISTS users(id INTEGER PRIMARY KEY, name TEXT UNIQUE NOT NULL, role TEXT NOT NULL CHECK(role IN ('requester','agent')));
CREATE TABLE IF NOT EXISTS tickets(id INTEGER PRIMARY KEY, title TEXT NOT NULL, description TEXT NOT NULL DEFAULT '', priority TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'new', requester_id INTEGER NOT NULL REFERENCES users(id), assignee_id INTEGER REFERENCES users(id),
  created REAL NOT NULL, due REAL NOT NULL, resolved REAL);
CREATE TABLE IF NOT EXISTS history(id INTEGER PRIMARY KEY, ticket_id INTEGER NOT NULL, at REAL NOT NULL, actor TEXT NOT NULL, field TEXT NOT NULL, old TEXT, new TEXT);
CREATE TABLE IF NOT EXISTS outbox(id INTEGER PRIMARY KEY, at REAL NOT NULL, to_user TEXT NOT NULL, subject TEXT NOT NULL, sent INTEGER NOT NULL DEFAULT 0);
"""


class Desk:
    def __init__(self, path: str = ":memory:", clock: Callable[[], float] = time.time):
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)
        self.clock = clock

    # ---- users
    def add_user(self, name: str, role: str) -> int:
        if role not in ROLES:
            raise DeskError("role must be one of %s" % ", ".join(ROLES))
        try:
            cur = self.db.execute("INSERT INTO users(name, role) VALUES(?,?)", (name, role))
        except sqlite3.IntegrityError:
            raise DeskError("user %r already exists" % name)
        self.db.commit()
        return cur.lastrowid

    def user(self, name: str) -> sqlite3.Row:
        row = self.db.execute("SELECT * FROM users WHERE name=?", (name,)).fetchone()
        if not row:
            raise NotFound("no user %r" % name)
        return row

    # ---- tickets
    def create(self, actor: str, title: str, description: str = "", priority: str = "medium") -> int:
        u = self.user(actor)
        if not title.strip():
            raise DeskError("a ticket needs a title")
        if priority not in SLA_HOURS:
            raise DeskError("priority must be one of %s" % ", ".join(SLA_HOURS))
        now = self.clock()
        cur = self.db.execute("INSERT INTO tickets(title, description, priority, requester_id, created, due) VALUES(?,?,?,?,?,?)",
                              (title.strip(), description, priority, u["id"], now, now + SLA_HOURS[priority] * 3600))
        self._log(cur.lastrowid, actor, "status", None, "new")
        self._notify("agents", "New %s ticket #%d: %s" % (priority, cur.lastrowid, title.strip()))
        self.db.commit()
        return cur.lastrowid

    def _ticket(self, tid: int) -> sqlite3.Row:
        row = self.db.execute("SELECT t.*, r.name AS requester, a.name AS assignee FROM tickets t JOIN users r ON r.id=t.requester_id "
                              "LEFT JOIN users a ON a.id=t.assignee_id WHERE t.id=?", (tid,)).fetchone()
        if not row:
            raise NotFound("no ticket #%s" % tid)
        return row

    def get(self, actor: str, tid: int) -> Dict[str, object]:
        u, t = self.user(actor), self._ticket(tid)
        if u["role"] == "requester" and t["requester_id"] != u["id"]:
            raise Forbidden("requesters can only see their own tickets")       # same answer as for an existing ticket, no leak
        out = dict(t)
        out["history"] = [dict(h) for h in self.db.execute("SELECT at, actor, field, old, new FROM history WHERE ticket_id=? ORDER BY id", (tid,))]
        return out

    def list(self, actor: str, status: Optional[str] = None) -> List[Dict[str, object]]:
        u = self.user(actor)
        q, args = "SELECT t.id, t.title, t.priority, t.status, t.due, r.name AS requester FROM tickets t JOIN users r ON r.id=t.requester_id WHERE 1=1", []
        if u["role"] == "requester":
            q, args = q + " AND t.requester_id=?", args + [u["id"]]
        if status:
            q, args = q + " AND t.status=?", args + [status]
        return [dict(r) for r in self.db.execute(q + " ORDER BY t.due, t.id", args)]

    def set_status(self, actor: str, tid: int, status: str) -> None:
        u, t = self.user(actor), self._ticket(tid)
        if u["role"] != "agent":
            raise Forbidden("only agents can change a ticket's status")
        if status not in TRANSITIONS.get(t["status"], set()):
            raise DeskError("cannot move a ticket from %s to %s" % (t["status"], status))
        now = self.clock()
        self.db.execute("UPDATE tickets SET status=?, resolved=? WHERE id=?", (status, now if status == "resolved" else (None if status == "open" else t["resolved"]), tid))
        self._log(tid, actor, "status", t["status"], status)
        self._notify(t["requester"], "Ticket #%d is now %s" % (tid, status))
        self.db.commit()

    def assign(self, actor: str, tid: int, assignee: str) -> None:
        u, t, a = self.user(actor), self._ticket(tid), self.user(assignee)
        if u["role"] != "agent" or a["role"] != "agent":
            raise Forbidden("only agents can assign tickets, and only to agents")
        self.db.execute("UPDATE tickets SET assignee_id=? WHERE id=?", (a["id"], tid))
        self._log(tid, actor, "assignee", t["assignee"], assignee)
        self._notify(assignee, "Ticket #%d assigned to you" % tid)
        self.db.commit()

    # ---- SLA
    def sla_report(self) -> Dict[str, object]:
        """Breached = still not resolved after its deadline, or resolved later than the deadline. At risk = under 25% of its time left."""
        now, breached, at_risk, met = self.clock(), [], [], 0
        for t in self.db.execute("SELECT id, title, priority, status, created, due, resolved FROM tickets WHERE status<>'closed' OR resolved IS NOT NULL"):
            if t["resolved"] is not None:
                if t["resolved"] > t["due"]:
                    breached.append(t["id"])
                else:
                    met += 1
            elif now > t["due"]:
                breached.append(t["id"])
            elif (t["due"] - now) < 0.25 * (t["due"] - t["created"]):
                at_risk.append(t["id"])
        return {"breached": sorted(breached), "at_risk": sorted(at_risk), "met": met}

    # ---- notifications and history
    def _notify(self, to: str, subject: str) -> None:
        self.db.execute("INSERT INTO outbox(at, to_user, subject) VALUES(?,?,?)", (self.clock(), to, subject))

    def outbox(self, only_unsent: bool = True) -> List[Dict[str, object]]:
        q = "SELECT id, to_user, subject FROM outbox" + (" WHERE sent=0" if only_unsent else "") + " ORDER BY id"
        return [dict(r) for r in self.db.execute(q)]

    def mark_sent(self, outbox_id: int) -> None:
        self.db.execute("UPDATE outbox SET sent=1 WHERE id=?", (outbox_id,))
        self.db.commit()

    def _log(self, tid: int, actor: str, field: str, old: Optional[str], new: Optional[str]) -> None:
        self.db.execute("INSERT INTO history(ticket_id, at, actor, field, old, new) VALUES(?,?,?,?,?,?)", (tid, self.clock(), actor, field, old, new))
