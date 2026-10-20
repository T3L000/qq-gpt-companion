"""Local loopback bridge; no third-party dependencies and no website scraping."""
from __future__ import annotations

import argparse
import hmac
import json
import logging
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

from . import core, agent_queue, tuning

LOG = logging.getLogger("companion")


class Handler(BaseHTTPRequestHandler):
    server_version = "QQGPTCompanion/0.1"
    def log_message(self, fmt, *args):
        LOG.info("%s - %s", self.client_address[0], fmt % args)

    def _json(self, code: int, body: object) -> None:
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _authorized(self) -> bool:
        supplied = self.headers.get("X-Companion-Token", "")
        if not hmac.compare_digest(supplied, core.token()):
            self._json(401, {"error": "invalid local token"})
            return False
        return True

    def _parse_body(self) -> dict:
        length = int(self.headers.get("Content-Length", "0"))
        if length <= 0 or length > 400_000:
            raise ValueError("invalid request size")
        body = json.loads(self.rfile.read(length))
        if not isinstance(body, dict):
            raise ValueError("JSON object required")
        return body

    def do_GET(self):
        path = urlsplit(self.path).path
        if path in ("/", "/dashboard"):
            html = (core.ROOT / "dashboard" / "index.html").read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(html)))
            self.end_headers()
            self.wfile.write(html)
            return
        if not self._authorized():
            return
        try:
            if path == "/api/status":
                self._json(200, core.get_status())
            elif path == "/api/requests":
                self._json(200, {"requests": core.pending_requests()})
            elif path == "/api/agent/status":
                self._json(200, agent_queue.queue_status())
            elif path == "/api/tuning":
                self._json(200, tuning.snapshot())
            else:
                self._json(404, {"error": "not_found"})
        except Exception as exc:
            LOG.exception("GET failed")
            self._json(500, {"error": str(exc)})

    def do_POST(self):
        if not self._authorized():
            return
        try:
            path = urlsplit(self.path).path
            body = self._parse_body()
            if path == "/api/events":
                result = core.ingest_message(body)
            elif path == "/api/reply":
                result = core.submit_reply(str(body.get("request_id", "")), str(body.get("reply", "")))
            elif path == "/api/silence":
                result = core.silence_reply(str(body.get("request_id", "")))
            elif path == "/api/outbound/claim":
                result = {"item": core.claim_outbound()}
            elif path == "/api/outbound/ack":
                core.ack_outbound(str(body.get("request_id", "")), str(body.get("outcome", "")), str(body.get("details", "")))
                result = {"ok": True}
            elif path == "/api/agent/wake":
                result = {"item": agent_queue.reserve_wake()}
            elif path == "/api/agent/claim":
                result = {"item": agent_queue.claim_for_agent(str(body.get("run_id", "")))}
            elif path == "/api/agent/result":
                result = agent_queue.finalize(
                    str(body.get("run_id", "")), str(body.get("request_id", "")),
                    str(body.get("event_id", "")), str(body.get("decision", "")),
                    str(body.get("reply", "")),
                )
            elif path == "/api/agent/trigger-unknown":
                agent_queue.mark_trigger_unknown(str(body.get("event_id", "")),"wake_outcome_unknown")
                result = {"ok": True}
            elif path == "/api/tuning":
                result = tuning.update(body.get("values"), body.get("expected_version"))
            else:
                self._json(404, {"error": "not_found"})
                return
            self._json(200, result)
        except (ValueError, KeyError, json.JSONDecodeError) as exc:
            self._json(400, {"error": str(exc)})
        except Exception as exc:
            LOG.exception("POST failed")
            self._json(500, {"error": str(exc)})


def main():
    parser = argparse.ArgumentParser(description="Run local GPT QQ companion bridge")
    parser.add_argument("--port", type=int, default=18761)
    args = parser.parse_args()
    core.init()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    LOG.info("Ready: http://127.0.0.1:%d/ (local only, provider=manual_web)", args.port)
    try:
        server.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
