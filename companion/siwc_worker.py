"""Official SIWC ChatGPT-plan Responses adapter, opt-in only.

Uses only app-authorized OAuth to https://api.openai.com/v1/responses.
Never extracts browser output, reads Codex CLI auth, or directly sends QQ.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import logging
import time
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from . import core, siwc_auth

LOG = logging.getLogger("qq_siwc")
MAX_REPLY_TEXT = 3000
LIVE_TEST_GROUP = os.getenv("QQ_COMPANION_TEST_GROUP", "")


def config_valid(root: Path = core.ROOT) -> tuple[bool, str]:
    cfg = core.config(root)
    provider = cfg.get("provider", {})
    groups = [str(g) for g in cfg.get("allowed_groups", [])]
    if not cfg.get("enabled") or provider.get("mode") != "siwc" or provider.get("siwc_enabled") is not True:
        return False, "disabled"
    if len(groups) != 1 or (root.resolve() == core.ROOT.resolve() and groups[0] != LIVE_TEST_GROUP):
        return False, "test_group_only"
    if not provider.get("model") or not isinstance(provider.get("model"), str):
        return False, "model_not_selected"
    if not isinstance(provider.get("siwc_activated_at"), str) or not provider["siwc_activated_at"]:
        return False, "activation_time_missing"
    return True, "configured"


def doctor(root: Path = core.ROOT) -> dict:
    enabled, reason = config_valid(root)
    authentication = siwc_auth.status(root)
    return {"ready": bool(enabled and authentication.get("plan_permission") and
                           authentication.get("token_current")),
            "reason": reason if not enabled else (
                "ready" if authentication.get("plan_permission") and authentication.get("token_current")
                else "need_authorization_or_refresh"),
            "plan_auth": authentication}


def model_catalog(root: Path = core.ROOT) -> list[dict]:
    """Read-only model listing: actual OAuth entitlements, not a guessed catalog."""
    token = siwc_auth.current_access_token(root)
    req = Request(siwc_auth.RESOURCE + "/models", headers={"Authorization": "Bearer " + token})
    with urlopen(req, timeout=12) as response:
        data = json.load(response)
    models = data.get("models", []) if isinstance(data, dict) else []
    return [{"slug":m["slug"], "display_name":m.get("display_name", m["slug"])}
            for m in models if isinstance(m, dict) and m.get("visibility") == "list"
            and isinstance(m.get("slug"), str)]


def test_model(model: str, root: Path = core.ROOT) -> dict:
    """Opt-in one-turn live test; consumes eligible subscription usage."""
    choices = {item["slug"] for item in model_catalog(root)}
    if model not in choices:
        raise ValueError("Selected model is not in this account's current model list")
    token = siwc_auth.current_access_token(root)
    _text, first_ms, total_ms = _stream_response(
        "Reply briefly with QQ_SIWC_OK.", token, model, timeout=60
    )
    # No raw reply or account tokens are logged/persisted.
    receipt = {"model": model, "first_token_ms": first_ms,
               "model_total_ms": total_ms, "timestamp": int(time.time())}
    siwc_auth._save_private(siwc_auth._directory(root) / "model_test.json", receipt)
    return {"completed": True, "model": model, "first_token_ms": first_ms,
            "model_total_ms": total_ms}


def activate(model: str, root: Path = core.ROOT) -> dict:
    """Explicit local activation affects NEW requests only, never QQ sending."""
    receipt = siwc_auth._read_json(siwc_auth._directory(root) / "model_test.json")
    if receipt.get("model") != model or time.time() - receipt.get("timestamp", 0) > 3600:
        raise ValueError("A successful recent test is required before activation")
    if not siwc_auth.status(root).get("plan_permission"):
        raise PermissionError("ChatGPT plan use was not authorized")
    config = core.config(root)
    if len(config.get("allowed_groups", [])) != 1 or (
        root.resolve() == core.ROOT.resolve() and
        str(config["allowed_groups"][0]) != LIVE_TEST_GROUP
    ):
        raise ValueError("Only the approved test group can enable this model")
    provider = config.setdefault("provider", {})
    provider.update({
        "mode": "siwc", "siwc_enabled": True, "model": model,
        "siwc_activated_at": core.now(), "auto_web_control": False,
    })
    core.save_config(config, root)
    return {"mode": "siwc", "model": model,
            "new_requests_only": True, "qq_outbound_unchanged": True}


def deactivate(root: Path = core.ROOT) -> dict:
    cfg = core.config(root)
    cfg["provider"]["mode"] = "manual_web"
    cfg["provider"]["siwc_enabled"] = False
    core.save_config(cfg, root)
    return {"mode": "manual_web", "qq_outbound_unchanged": True}


def _stream_response(prompt: str, token: str, model: str, timeout: int = 90):
    # SIWC preview requires store:false, stream:true, array input and no
    # previous_response_id/unsupported sampling fields.
    body = {"model": model, "input": [{"role": "user", "content": prompt}],
            "store": False, "stream": True}
    req = Request(
        siwc_auth.RESOURCE + "/responses",
        data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
        headers={"Authorization": "Bearer " + token, "Content-Type": "application/json"},
        method="POST",
    )
    start = time.monotonic()
    first_ms = None
    parts: list[str] = []
    total_chars = 0
    completed = False
    with urlopen(req, timeout=timeout) as response:
        if "text/event-stream" not in response.headers.get("Content-Type", ""):
            raise ValueError("Expected Responses SSE stream")
        for line in response:
            if time.monotonic() - start > timeout:
                raise TimeoutError("Responses stream deadline exceeded")
            if len(line) > 262_144:
                raise ValueError("Oversized Responses SSE frame")
            if not line.startswith(b"data:"):
                continue
            payload = line[5:].strip()
            if not payload or payload == b"[DONE]":
                continue
            event = json.loads(payload.decode("utf-8"))
            if not isinstance(event, dict):
                raise ValueError("Invalid SSE event")
            kind = event.get("type")
            if kind in ("error", "response.failed", "response.incomplete"):
                # Do not retry: the upstream may already have consumed usage.
                raise ValueError("Responses did not complete: " + str(kind))
            if kind == "response.output_text.delta":
                delta = event.get("delta")
                if isinstance(delta, str) and delta:
                    first_ms = first_ms if first_ms is not None else int((time.monotonic()-start)*1000)
                    total_chars += len(delta)
                    if total_chars > MAX_REPLY_TEXT:
                        raise ValueError("Model output exceeds local reply limit")
                    parts.append(delta)
            if kind == "response.completed":
                completed = True
                break
    if not completed or not parts:
        raise ValueError("Model stream incomplete; no QQ reply queued")
    return "".join(parts).strip(), first_ms, int((time.monotonic() - start) * 1000)


def _claim(request_id: str, root: Path) -> bool:
    with core._DB_LOCK, core.open_db(root) as db:
        db.execute("BEGIN IMMEDIATE")
        row = db.execute("SELECT status,group_id FROM requests WHERE id=?", (request_id,)).fetchone()
        if not row or row["status"] != "pending":
            return False
        group = core.config(root)["allowed_groups"][0]
        if row["group_id"] != str(group):
            return False
        previous = db.execute(
            "SELECT 1 FROM requests WHERE group_id=? AND id<>? "
            "AND status IN ('ready','sending','generating') LIMIT 1",
            (row["group_id"], request_id),
        ).fetchone()
        if previous:
            return False
        cursor = db.execute(
            "INSERT OR IGNORE INTO siwc_attempts(request_id,state,updated_at) "
            "VALUES(?,'attempted',?)", (request_id, core.now()),
        )
        if cursor.rowcount != 1:
            return False
        db.execute("UPDATE requests SET status='generating',updated_at=? "
                   "WHERE id=? AND status='pending'", (core.now(),request_id))
        return True


def _record(request_id: str, root: Path, state: str, first_ms: int | None,
            total_ms: int, error_type: str = ""):
    with core._DB_LOCK, core.open_db(root) as db:
        db.execute("UPDATE siwc_attempts SET state=?,first_token_ms=?,duration_ms=?,"
                   "error_type=?,updated_at=? WHERE request_id=?",
                   (state, first_ms, total_ms, error_type[:80], core.now(), request_id))
        db.execute(
            "INSERT INTO request_timings(request_id,first_token_ms,model_total_ms,error_type,updated_at) "
            "VALUES(?,?,?,?,?) ON CONFLICT(request_id) DO UPDATE SET "
            "first_token_ms=excluded.first_token_ms,model_total_ms=excluded.model_total_ms,"
            "error_type=excluded.error_type,updated_at=excluded.updated_at",
            (request_id, first_ms,total_ms,error_type[:80],core.now()),
        )
        if state == "model_uncertain":
            db.execute("UPDATE requests SET status='model_uncertain',updated_at=? "
                       "WHERE id=? AND status='generating'",(core.now(),request_id))


def process_once(root: Path = core.ROOT) -> str:
    valid, reason = config_valid(root)
    if not valid:
        return reason
    # No network calls or quota used before a newly admitted job is found.
    activation = dt.datetime.fromisoformat(core.config(root)["provider"]["siwc_activated_at"])
    pending = [row for row in core.pending_requests(root, limit=100)
               if row["status"] == "pending" and
               dt.datetime.fromisoformat(row["created_at"]) >= activation]
    if not pending:
        return "idle"
    try:
        token = siwc_auth.current_access_token(root)
    except (FileNotFoundError, PermissionError, ValueError, HTTPError, URLError):
        return "auth_not_ready"
    model = core.config(root)["provider"]["model"]
    for row in pending:
        if not _claim(row["id"], root):
            continue
        start = time.monotonic()
        try:
            reply, first_ms, total_ms = _stream_response(row["prompt"], token, model)
            if not reply:
                raise ValueError("Empty model reply")
            core.submit_reply(row["id"], reply, root, accepted_statuses=("generating",))
        except (ValueError, TimeoutError, HTTPError, URLError, OSError, KeyError, json.JSONDecodeError) as exc:
            elapsed = int((time.monotonic() - start) * 1000)
            _record(row["id"],root,"model_uncertain",None,elapsed,type(exc).__name__)
            LOG.warning("SIWC request uncertain, not requeued: %s, %dms",type(exc).__name__, elapsed)
            return "unknown_no_retry"
        _record(row["id"], root, "ready",first_ms,total_ms)
        LOG.info("SIWC reply ready: first text %sms; total %sms", first_ms,total_ms)
        return "reply_ready"
    return "busy"


def main():
    parser = argparse.ArgumentParser(description="Authorized ChatGPT-plan QQ text worker")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--doctor",action="store_true")
    mode.add_argument("--models",action="store_true")
    mode.add_argument("--once",action="store_true")
    mode.add_argument("--serve",action="store_true")
    mode.add_argument("--test-model",metavar="MODEL")
    mode.add_argument("--activate-model",metavar="MODEL")
    mode.add_argument("--deactivate",action="store_true")
    args = parser.parse_args()
    core.ensure_initialized()
    logging.basicConfig(level=logging.INFO)
    if args.doctor:
        print(json.dumps(doctor(),ensure_ascii=False))
    elif args.models:
        print(json.dumps(model_catalog(),ensure_ascii=False))
    elif args.test_model:
        print(json.dumps(test_model(args.test_model),ensure_ascii=False))
    elif args.activate_model:
        print(json.dumps(activate(args.activate_model),ensure_ascii=False))
    elif args.deactivate:
        print(json.dumps(deactivate(),ensure_ascii=False))
    elif args.once:
        print(process_once())
    else:
        ok, reason = config_valid()
        if not ok:
            raise SystemExit("SIWC QQ worker disabled: " + reason)
        try:
            while True:
                process_once()
                time.sleep(1)
        except KeyboardInterrupt:
            pass


if __name__ == "__main__":
    main()
