"""Durable QQ-side handoff for one fixed Multica Agent (no per-message issue).

Only requests CREATED with the integration enabled get an agent_jobs record.
Old pending QQ messages are never swept into this dispatcher. The external
Agent must claim with its real run_id and return a matching event_id.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import os
import re
import time
import uuid
from pathlib import Path
from typing import Any

from . import core


def milliseconds() -> int:
    return time.time_ns() // 1_000_000


def permitted(root: Path = core.ROOT) -> bool:
    cfg = core.config(root)
    m = cfg.get("multica", {})
    group = str(m.get("test_group", ""))
    # Hard boundary for this phase: one group only, never arbitrary target groups.
    if root.resolve() == core.ROOT.resolve() and group != os.getenv("QQ_COMPANION_TEST_GROUP", ""):
        # Production phase-1 allowlist: never dispatch the two target groups.
        return False
    return bool(cfg.get("enabled") and m.get("enabled") and group
                and [str(x) for x in cfg.get("allowed_groups", [])] == [group])


def _expire(db, root: Path) -> None:
    multica = core.config(root).get("multica", {})
    max_age = max(10, int(multica.get("event_max_age_seconds", 120)))
    # A busy agent must not silently expire the entire waiting queue after
    # only one run timeout. Old group context still has a bounded freshness.
    queued_age = max(max_age, int(multica.get("queue_max_age_seconds", 900)))
    current = milliseconds()
    # Not receiving an acknowledgement within the bounded window leaves an
    # UNKNOWN; never resubmit an external Agent run with unknown side effects.
    rows = db.execute(
        "SELECT request_id,event_id,state,wake_ms,claim_ms FROM agent_jobs "
        "WHERE state IN ('waking','running')"
    ).fetchall()
    for row in rows:
        start = row["claim_ms"] or row["wake_ms"]
        if start is not None and current - start > max_age * 1000:
            _end_unknown(db, row["request_id"], row["event_id"], "timeout_without_result")
    cutoff = (dt.datetime.now(core.UTC) - dt.timedelta(seconds=queued_age)).isoformat(timespec="seconds")
    stale = db.execute(
        "SELECT request_id,event_id FROM agent_jobs WHERE state IN ('queued','waiting') AND created_at<?",
        (cutoff,),
    ).fetchall()
    for row in stale:
        db.execute("UPDATE agent_jobs SET state='expired',updated_at=? WHERE request_id=?",
                   (core.now(), row["request_id"]))
        db.execute("UPDATE requests SET status='silenced',updated_at=? WHERE id=? AND status='pending'",
                   (core.now(), row["request_id"]))
        db.execute(
            "INSERT OR REPLACE INTO agent_run_history(event_id,request_id,outcome,updated_at) VALUES(?,?,?,?)",
            (row["event_id"], row["request_id"], "expired", core.now()),
        )


def _end_unknown(db, request_id: str, event_id: str, why: str) -> None:
    db.execute(
        "UPDATE agent_jobs SET state='unknown',decision='unknown',details=?,result_ms=?,updated_at=? "
        "WHERE request_id=? AND event_id=? AND state IN ('waking','running')",
        (why[:180], milliseconds(), core.now(), request_id, event_id),
    )
    db.execute("UPDATE requests SET status='model_uncertain',updated_at=? WHERE id=? AND status='pending'",
               (core.now(), request_id))
    db.execute("UPDATE agent_run_history SET outcome='unknown',updated_at=? WHERE event_id=?",
               (core.now(), event_id))


def reserve_wake(root: Path = core.ROOT) -> dict[str, Any] | None:
    """Atomically mark ONE new job waking; call the Multica trigger only once."""
    if not permitted(root):
        return None
    # This is also responsible for refreshing the same-speaker tail prompt.
    with core._DB_LOCK:
        # Keep same-process ingestion and the tail-quiet refresh serialized.
        ready = {x["id"] for x in core.pending_requests(root, limit=250)
                 if x["status"] == "pending"}
        return _reserve_ready(root, ready)


def _reserve_ready(root: Path, ready: set[str]) -> dict[str, Any] | None:
    with core._DB_LOCK, core.open_db(root) as db:
        db.execute("BEGIN IMMEDIATE")
        _expire(db, root)
        if db.execute("SELECT 1 FROM agent_jobs WHERE state IN ('waking','running') LIMIT 1").fetchone():
            return None
        # Model execution must not overtake an earlier unsent QQ response.
        if db.execute("SELECT 1 FROM requests WHERE status IN ('ready','sending','generating') "
                      "AND group_id=? LIMIT 1", (str(core.config(root)["multica"]["test_group"]),)).fetchone():
            return None
        if not ready:
            return None
        placeholders = ",".join("?" for _ in ready)
        row = db.execute(
            "SELECT j.request_id,j.event_id,j.group_id,r.prompt FROM agent_jobs j "
            "JOIN requests r ON r.id=j.request_id "
            "WHERE j.state='queued' AND r.status='pending' AND j.request_id IN ("+placeholders+") "
            "ORDER BY j.created_at LIMIT 1", tuple(sorted(ready)),
        ).fetchone()
        if not row:
            return None
        db.execute("UPDATE agent_jobs SET state='waking',wake_ms=?,updated_at=? "
                   "WHERE request_id=? AND state='queued'", (milliseconds(),core.now(),row["request_id"]))
        db.execute(
            "INSERT INTO agent_run_history(event_id,request_id,outcome,updated_at) VALUES(?,?,?,?)",
            (row["event_id"],row["request_id"],"waking",core.now()),
        )
        return dict(row)


def claim_for_agent(run_id: str, root: Path = core.ROOT) -> dict[str, Any] | None:
    """Fixed Agent's authorized local tool: bind actual run_id exactly once."""
    if not permitted(root):
        return None
    if not re.fullmatch(r"[A-Za-z0-9_.:\-]{1,128}",run_id or ""):
        raise ValueError("invalid run_id")
    with core._DB_LOCK, core.open_db(root) as db:
        db.execute("BEGIN IMMEDIATE")
        _expire(db, root)
        row = db.execute("SELECT * FROM agent_jobs WHERE run_id=?",(run_id,)).fetchone()
        if row:
            if row["state"] != "running":
                return None
        else:
            row = db.execute("SELECT * FROM agent_jobs WHERE state='waking' "
                             "ORDER BY created_at LIMIT 1").fetchone()
            if not row:
                return None
            db.execute("UPDATE agent_jobs SET state='running',run_id=?,claim_ms=?,updated_at=? "
                       "WHERE request_id=? AND state='waking'",
                       (run_id,milliseconds(),core.now(),row["request_id"]))
            db.execute("UPDATE agent_run_history SET run_id=?,outcome='running',updated_at=? "
                       "WHERE event_id=?",(run_id,core.now(),row["event_id"]))
        request = db.execute(
            "SELECT prompt,status FROM requests WHERE id=?",(row["request_id"],)
        ).fetchone()
        if not request or request["status"] != "pending":
            return None
        return {"request_id":row["request_id"],"event_id":row["event_id"],
                "run_id":run_id,"group_id":row["group_id"],"prompt":request["prompt"]}


def finalize(run_id: str, request_id: str, event_id: str, decision: str,
             reply: str = "", root: Path = core.ROOT) -> dict[str, Any]:
    """Planner: reply | silence | wait | unknown. UNKNOWN never sends."""
    if not permitted(root):
        raise ValueError("agent relay disabled")
    if decision not in ("reply","silence","wait","unknown"):
        raise ValueError("invalid planner decision")
    if not re.fullmatch(r"[A-Za-z0-9_.:\-]{1,128}",run_id or ""):
        raise ValueError("invalid run_id")
    reply = reply.strip()
    if decision == "reply" and not (1 <= len(reply) <= 3000):
        raise ValueError("invalid reply length")
    if decision != "reply" and reply:
        raise ValueError("non-reply must not contain text")
    digest = hashlib.sha256((decision+"\0"+reply).encode("utf-8")).hexdigest()
    with core._DB_LOCK, core.open_db(root) as db:
        db.execute("BEGIN IMMEDIATE")
        job = db.execute("SELECT * FROM agent_jobs WHERE request_id=? AND event_id=? AND run_id=?",
                         (request_id,event_id,run_id)).fetchone()
        if not job:
            raise ValueError("unbound or stale request/event/run")
        if job["state"] in ("done","silenced","waiting","unknown"):
            if job["details"] == digest and job["decision"] == decision:
                return {"ok":True,"duplicate":True,"state":job["state"]}
            raise ValueError("conflicting or stale Agent result")
        if job["state"] != "running":
            raise ValueError("Agent run not active")
        req = db.execute("SELECT status,group_id FROM requests WHERE id=?",(request_id,)).fetchone()
        if not req or req["status"] != "pending" or req["group_id"] != job["group_id"]:
            raise ValueError("request changed while Agent was working")
        if decision == "reply":
            limit = int(core.config(root)["engagement"]["max_reply_characters"])
            db.execute("UPDATE requests SET status='ready',reply_text=?,updated_at=? WHERE id=?",
                       (reply[:limit],core.now(),request_id))
            state = "done"
        elif decision == "silence":
            db.execute("UPDATE requests SET status='silenced',updated_at=? WHERE id=?",
                       (core.now(),request_id))
            state = "silenced"
        elif decision == "wait":
            state = "waiting"
        else:
            db.execute("UPDATE requests SET status='model_uncertain',updated_at=? WHERE id=?",
                       (core.now(),request_id))
            state = "unknown"
        db.execute("UPDATE agent_jobs SET state=?,decision=?,details=?,result_ms=?,updated_at=? "
                   "WHERE request_id=?",(state,decision,digest,milliseconds(),core.now(),request_id))
        db.execute("UPDATE agent_run_history SET outcome=?,updated_at=? WHERE event_id=?",
                   (state,core.now(),event_id))
        return {"ok":True,"duplicate":False,"state":state}


def mark_trigger_unknown(event_id: str, why: str, root: Path = core.ROOT) -> None:
    with core._DB_LOCK,core.open_db(root) as db:
        db.execute("BEGIN IMMEDIATE")
        row = db.execute("SELECT request_id FROM agent_jobs WHERE event_id=? AND state='waking'",
                         (event_id,)).fetchone()
        if row:
            _end_unknown(db,row["request_id"],event_id,why)


def queue_status(root: Path = core.ROOT) -> dict[str, Any]:
    # Read-only UI polling is enough to reconcile stale in-flight runs after a
    # dispatcher crash. This only changes state; it never triggers a retry.
    with core._DB_LOCK, core.open_db(root) as db:
        db.execute("BEGIN IMMEDIATE")
        _expire(db, root)
        counts = {r["state"]:r["n"] for r in db.execute(
            "SELECT state,COUNT(*) n FROM agent_jobs GROUP BY state")}
        last = db.execute("SELECT request_id,event_id,state,run_id,wake_ms,claim_ms,result_ms "
                          "FROM agent_jobs ORDER BY updated_at DESC LIMIT 1").fetchone()
    durations = None
    if last:
        durations = {"wake_to_claim_ms":last["claim_ms"]-last["wake_ms"]
                     if last["claim_ms"] is not None and last["wake_ms"] is not None else None,
                     "claim_to_result_ms":last["result_ms"]-last["claim_ms"]
                     if last["result_ms"] is not None and last["claim_ms"] is not None else None}
    return {"counts":counts,"last_timing_ms":durations}
