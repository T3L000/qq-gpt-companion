"""Local callable tools for the fixed Multica Agent; no second QQ sender."""
import argparse
import json
import sys

from . import core, agent_queue


def main() -> None:
    parser = argparse.ArgumentParser(description="Claim and finish one QQ Agent job")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("status")
    claim = sub.add_parser("claim")
    claim.add_argument("--run-id", required=True)
    result = sub.add_parser("result")
    result.add_argument("--run-id", required=True)
    result.add_argument("--request-id", required=True)
    result.add_argument("--event-id", required=True)
    result.add_argument("--decision", required=True, choices=("reply","wait","silence","unknown"))
    result.add_argument("--reply", default="")
    result.add_argument("--reply-stdin", action="store_true", help="read reply from standard input, not process arguments")
    args = parser.parse_args()
    core.ensure_initialized()
    if args.command == "status":
        outcome = agent_queue.queue_status()
    elif args.command == "claim":
        outcome = {"item": agent_queue.claim_for_agent(args.run_id)}
    else:
        if args.reply_stdin and args.reply:
            parser.error("use either --reply or --reply-stdin")
        reply = sys.stdin.read(3001) if args.reply_stdin else args.reply
        outcome = agent_queue.finalize(
            args.run_id,args.request_id,args.event_id,args.decision,reply,
        )
    print(json.dumps(outcome, ensure_ascii=False))


if __name__ == "__main__":
    main()
