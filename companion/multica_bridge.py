"""Bounded event waker for an EXISTING fixed Multica autopilot.

No issue creation, agent registration, daemon manipulation, or web scraping.
The v0.5.0 CLI supports `autopilot trigger <id>` but has no per-run payload;
the fixed Agent retrieves its QQ task via agent_queue.claim_for_agent().
"""
from __future__ import annotations

import argparse
import json
import os
import socket
import subprocess
import time
from pathlib import Path
from urllib.parse import urlsplit

from . import core, agent_queue


def readiness(root: Path = core.ROOT) -> tuple[bool, str]:
    if not agent_queue.permitted(root):
        return False, "qq_agent_disabled"
    m = core.config(root).get("multica", {})
    if not m.get("autopilot_id"):
        return False, "no_fixed_autopilot_id"
    if not os.getenv("MULTICA_TOKEN", "").startswith("mat_"):
        return False, "no_task_scoped_multica_token"
    uri = urlsplit(os.getenv("MULTICA_SERVER_URL", "http://127.0.0.1:3001"))
    if uri.scheme != "http" or uri.hostname not in ("localhost", "127.0.0.1"):
        return False, "unexpected_multica_server_address"
    try:
        with socket.create_connection((uri.hostname, uri.port or 80), timeout=1):
            pass
    except OSError:
        return False, "multica_server_offline"
    return True, "ready_for_trigger_only"


def dispatch_once(root: Path = core.ROOT, runner=None) -> str:
    ok, reason = readiness(root)
    if not ok:
        return reason
    job = agent_queue.reserve_wake(root)
    if job is None:
        return "idle"
    autopilot_id = core.config(root)["multica"]["autopilot_id"]
    if runner is None:
        runner = subprocess.run
    try:
        result = runner(
            ["multica", "autopilot", "trigger", autopilot_id, "--output", "json"],
            check=False, capture_output=True, text=True, timeout=20,
        )
    except (OSError, subprocess.TimeoutExpired):
        agent_queue.mark_trigger_unknown(job["event_id"], "trigger_exception", root)
        return "UNKNOWN_no_retry"
    if result.returncode != 0:
        agent_queue.mark_trigger_unknown(job["event_id"], "trigger_nonzero", root)
        return "UNKNOWN_no_retry"
    # A successful CLI trigger is NOT a completed Agent or QQ send. The fixed
    # Agent still must claim with its actual Multica run_id and callback.
    return "trigger_accepted_waiting_for_agent"


def main() -> None:
    parser = argparse.ArgumentParser(description="QQ-side Multica event waker")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--doctor", action="store_true")
    group.add_argument("--once", action="store_true")
    group.add_argument("--serve", action="store_true")
    args = parser.parse_args()
    core.ensure_initialized()
    if args.doctor:
        ok, reason = readiness()
        print(json.dumps({"ready": ok, "reason": reason}, ensure_ascii=False))
    elif args.once:
        print(dispatch_once())
    else:
        ok, reason = readiness()
        if not ok:
            raise SystemExit("Dispatcher not started: " + reason)
        try:
            while True:
                dispatch_once()
                time.sleep(1)
        except KeyboardInterrupt:
            pass


if __name__ == "__main__":
    main()
