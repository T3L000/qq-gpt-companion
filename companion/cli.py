"""Local administration CLI. It never sends messages to a model by itself."""
from __future__ import annotations

import argparse
import json
import sqlite3
from pathlib import Path

from . import core


def show(obj):
    print(json.dumps(obj, ensure_ascii=False, indent=2))


def main():
    parser = argparse.ArgumentParser(prog="python -m companion.cli")
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("init", "status", "local-token", "requests", "enable", "disable"):
        sub.add_parser(name)
    p = sub.add_parser("allow-group")
    p.add_argument("group_id")
    p = sub.add_parser("remove-group")
    p.add_argument("group_id")
    p = sub.add_parser("bind-session")
    p.add_argument("group_id")
    p.add_argument("url")
    p = sub.add_parser("reply")
    p.add_argument("request_id")
    p.add_argument("text")
    handoff = sub.add_parser("handoff")
    handoff.add_argument("action", choices=("prepare", "activate", "abort"))
    handoff.add_argument("id", nargs="?")
    handoff.add_argument("url", nargs="?")
    handoff.add_argument("--group", default="")
    memory = sub.add_parser("memory")
    memory.add_argument("action", choices=("opt-in", "opt-out", "add", "list", "delete"))
    memory.add_argument("--group", required=True)
    memory.add_argument("--member", default="")
    memory.add_argument("--content", default="")
    memory.add_argument("--source", default="")
    memory.add_argument("--id", default="")
    args = parser.parse_args()
    core.init()
    if args.command == "init":
        print(f"Initialized: {core.ROOT}")
    elif args.command == "status":
        show(core.get_status())
    elif args.command == "local-token":
        print(core.token())
        print("Keep this secret; only configure it inside AstrBot or your private browser dashboard.")
    elif args.command == "requests":
        show(core.pending_requests())
    elif args.command == "bind-session":
        show(core.bind_initial_session(args.group_id, args.url))
    elif args.command == "reply":
        show(core.submit_reply(args.request_id, args.text))
    elif args.command in ("enable", "disable", "allow-group", "remove-group"):
        cfg = core.config()
        if args.command in ("enable", "disable"):
            cfg["enabled"] = args.command == "enable"
        else:
            group = str(args.group_id)
            groups = set(map(str, cfg.get("allowed_groups", [])))
            if args.command == "allow-group":
                groups.add(group)
            else:
                groups.discard(group)
            cfg["allowed_groups"] = sorted(groups)
        core.save_config(cfg)
        show({"enabled": cfg["enabled"], "allowed_groups": cfg["allowed_groups"]})
    elif args.command == "handoff":
        if args.action == "prepare":
            if not args.group:
                parser.error("handoff prepare requires --group <群号>")
            show(core.prepare_handoff(args.group))
        elif args.action == "activate":
            if not args.id or not args.url:
                parser.error("handoff activate requires ID and ChatGPT URL")
            show(core.activate_handoff(args.id, args.url))
        else:
            if not args.id:
                parser.error("handoff abort requires ID")
            core.abort_handoff(args.id)
            show({"ok": True, "status": "aborted"})
    elif args.command == "memory":
        if args.action in ("opt-in", "opt-out"):
            if not args.member:
                parser.error("--member required")
            core.set_member_consent(args.group, args.member, args.action == "opt-in")
            show({"ok": True, "member_id": args.member, "enabled": args.action == "opt-in"})
        elif args.action == "add":
            show({"memory_id": core.add_memory(args.group, args.member, args.content, args.source)})
        elif args.action == "list":
            with core.open_db() as db:
                show([dict(x) for x in db.execute(
                    "SELECT id,group_id,member_id,content,source,verified,created_at FROM memories WHERE group_id=? "
                    "ORDER BY created_at DESC LIMIT 100", (args.group,)
                )])
        elif args.action == "delete":
            if not args.id:
                parser.error("--id required")
            with core.open_db() as db:
                db.execute("DELETE FROM memories WHERE id=? AND group_id=?", (args.id, args.group))
                db.commit()
            show({"ok": True})


if __name__ == "__main__":
    main()
