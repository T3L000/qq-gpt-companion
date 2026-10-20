"""Optional one-group Web2API reply worker.

Never starts unless explicitly enabled in ignored local config. A request is
marked attempted *before* contacting a web provider, so failures/timeouts never
silently retry a possibly completed browser submission.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import time
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from . import core

LOG = logging.getLogger("qq_web2api")
MODEL = "chatgpt/default"


def _daemon_config_path() -> Path:
    override = os.getenv("WEB2API_CONFIG_PATH")
    if override:
        return Path(override)
    local = os.getenv("LOCALAPPDATA")
    return (Path(local) / "Glidea" / "Web2API" / "config.json") if local else (Path.home() / ".web2api" / "config.json")


def _provider_config() -> tuple[str, str]:
    path = _daemon_config_path()
    data = json.loads(path.read_text(encoding="utf-8"))
    port = data.get("port")
    key = data.get("api_key")
    if type(port) is not int or port < 1024 or port > 65535 or not isinstance(key, str) or not key.startswith("wb2_"):
        raise ValueError("Invalid local Web2API daemon configuration")
    return f"http://127.0.0.1:{port}", key


def _get_json(url: str, key: str | None = None, timeout: int = 5) -> dict:
    headers = {"Authorization": f"Bearer {key}"} if key is not None else {}
    with urlopen(Request(url, headers=headers), timeout=timeout) as response:
        obj = json.load(response)
    if not isinstance(obj, dict):
        raise ValueError("Unexpected Web2API response")
    return obj


def _stream_reply(base: str, key: str, prompt: str, timeout: int = 90) -> tuple[str, int | None, int]:
    """Parse upstream Responses SSE, measuring first *text* token and completion.

    A partial stream is always uncertain: it is never submitted to the QQ queue.
    """
    req = Request(
        f"{base}/v1/responses",
        data=json.dumps({"model": MODEL, "input": prompt, "stream": True}, ensure_ascii=False).encode("utf-8"),
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
        method="POST",
    )
    started = time.monotonic()
    first_ms: int | None = None
    pieces: list[str] = []
    chars = 0
    completed = False
    with urlopen(req, timeout=timeout) as response:
        if "text/event-stream" not in response.headers.get("Content-Type", ""):
            raise ValueError("Expected streaming text/event-stream")
        for raw in response:
            if time.monotonic() - started > timeout:
                raise TimeoutError("Model stream exceeded total deadline")
            if len(raw) > 262_144:
                raise ValueError("Oversized SSE event")
            if not raw.startswith(b"data:"):
                continue
            payload = raw[5:].strip()
            if payload == b"[DONE]":
                continue
            item = json.loads(payload.decode("utf-8"))
            kind = item.get("type")
            if kind == "error" or kind == "response.failed":
                raise ValueError("Model stream failed")
            if kind == "response.output_text.delta":
                delta = item.get("delta")
                if isinstance(delta, str) and delta:
                    if first_ms is None:
                        first_ms = int((time.monotonic() - started) * 1000)
                    pieces.append(delta)
                    chars += len(delta)
                    if chars > 3_000:
                        raise ValueError("Response exceeds safety limit")
            elif kind == "response.completed":
                completed = True
                break
    if not completed:
        raise ValueError("Incomplete model response stream")
    reply = "".join(pieces).strip()
    if not reply:
        raise ValueError("Model stream contained no text")
    return reply, first_ms, int((time.monotonic() - started) * 1000)


def provider_ready() -> tuple[bool, str]:
    try:
        base, key = _provider_config()
        health = _get_json(f"{base}/healthz")
        if health.get("daemon") != "ready" or not health.get("extension_connected") or health.get("workers_ready", 0) < 1:
            return False, "Extension not connected or worker not ready"
        models = _get_json(f"{base}/v1/models", key)
        ids = {item.get("id") for item in models.get("data", []) if isinstance(item, dict)}
        if MODEL not in ids:
            return False, "ChatGPT worker not available"
        return True, "ready"
    except (FileNotFoundError, ValueError, OSError, URLError, HTTPError, json.JSONDecodeError) as exc:
        return False, type(exc).__name__


def extract_text(result: dict) -> str:
    if result.get("status") not in (None, "completed"):
        raise ValueError("Web2API response not completed")
    chunks = []
    for item in result.get("output", []):
        if not isinstance(item, dict) or item.get("type") not in (None, "message"):
            continue
        for content in item.get("content", []):
            if isinstance(content, dict) and isinstance(content.get("text"), str):
                chunks.append(content["text"])
    reply = "\n".join(chunks).strip()
    if not reply:
        raise ValueError("Web2API returned no text")
    return reply


def _mark_attempt(request_id: str, root: Path) -> bool:
    # Durable once-only marker protects against browser timeouts, restarts
    # and accidental multiple worker instances.
    with core._DB_LOCK, core.open_db(root) as db:
        db.execute("BEGIN IMMEDIATE")
        row = db.execute("SELECT status FROM requests WHERE id=?", (request_id,)).fetchone()
        if not row or row["status"] != "pending":
            return False
        # Preserve group conversation order even when two worker processes exist.
        busy = db.execute(
            "SELECT 1 FROM requests WHERE group_id=(SELECT group_id FROM requests WHERE id=?) "
            "AND id!=? AND status IN ('generating','ready','sending') LIMIT 1",
            (request_id, request_id),
        ).fetchone()
        if busy:
            return False
        cur = db.execute(
            "INSERT OR IGNORE INTO web2api_attempts(request_id,state,updated_at) VALUES(?,?,?)",
            (request_id, "attempted", core.now()),
        )
        if cur.rowcount:
            db.execute("UPDATE requests SET status='generating',updated_at=? WHERE id=?",
                       (core.now(), request_id))
        db.commit()
        return cur.rowcount == 1


def _record_attempt(
    request_id: str, state: str, duration_ms: int, root: Path,
    first_token_ms: int | None = None, error_type: str = "",
) -> None:
    with core._DB_LOCK, core.open_db(root) as db:
        db.execute(
            "UPDATE web2api_attempts SET state=?,duration_ms=?,updated_at=? WHERE request_id=?",
            (state, duration_ms, core.now(), request_id),
        )
        db.execute(
            "INSERT INTO request_timings(request_id,first_token_ms,model_total_ms,error_type,updated_at) "
            "VALUES(?,?,?,?,?) ON CONFLICT(request_id) DO UPDATE SET "
            "first_token_ms=excluded.first_token_ms,model_total_ms=excluded.model_total_ms,"
            "error_type=excluded.error_type,updated_at=excluded.updated_at",
            (request_id, first_token_ms, duration_ms, error_type[:80], core.now()),
        )
        if state == "model_uncertain":
            db.execute(
                "UPDATE requests SET status='model_uncertain',updated_at=? "
                "WHERE id=? AND status='generating'", (core.now(), request_id),
            )
        db.commit()


def process_once(root: Path = core.ROOT) -> str:
    cfg = core.config(root)
    provider = cfg.get("provider", {})
    groups = cfg.get("allowed_groups", [])
    if not (cfg.get("enabled") and provider.get("mode") == "web2api"
            and provider.get("auto_web_control") is True and len(groups) == 1):
        return "disabled"
    ready, reason = provider_ready()
    if not ready:
        return f"not_ready:{reason}"
    base, key = _provider_config()
    for item in core.pending_requests(root):
        if item["status"] != "pending" or item["group_id"] not in groups:
            continue
        if not _mark_attempt(item["id"], root):
            continue
        start = time.monotonic()
        try:
            reply, first_ms, total_ms = _stream_reply(base, key, item["prompt"])
            # The model may have succeeded but local state might have changed
            # meanwhile: never force a second submission in that case.
            core.submit_reply(item["id"], reply, root, accepted_statuses=("generating",))
        except (HTTPError, URLError, TimeoutError, OSError, ValueError, KeyError, json.JSONDecodeError) as exc:
            took = int((time.monotonic() - start) * 1000)
            _record_attempt(item["id"], "model_uncertain", took, root, error_type=type(exc).__name__)
            LOG.warning("Web2API request failed (no automatic retry): %s, %dms", type(exc).__name__, took)
            return "failed_no_retry"
        _record_attempt(item["id"], "ready", total_ms, root, first_token_ms=first_ms)
        LOG.info("Web2API text first=%sms total=%dms (QQ send independently gated)", first_ms, total_ms)
        return "reply_ready"
    return "idle"


def main() -> None:
    parser = argparse.ArgumentParser(description="Opt-in one-group local Web2API worker")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--doctor", action="store_true", help="Read-only provider connection check")
    mode.add_argument("--once", action="store_true", help="Try one pending request")
    mode.add_argument("--serve", action="store_true", help="Poll until stopped")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    if args.doctor:
        ok, reason = provider_ready()
        print(json.dumps({"ready": ok, "reason": reason}, ensure_ascii=False))
        if not ok:
            raise SystemExit(2)
        return
    if args.once:
        print(process_once())
        return
    cfg = core.config()
    if not (cfg.get("provider", {}).get("mode") == "web2api"
            and cfg.get("provider", {}).get("auto_web_control") is True):
        raise SystemExit("Web2API worker not enabled in local config")
    last = ""
    try:
        while True:
            state = process_once()
            if state != last:
                LOG.info("Worker: %s", state)
                last = state
            time.sleep(1)
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
