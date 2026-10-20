"""Offline tests: no ChatGPT calls, no QQ connections."""
import json
import datetime as dt
import threading
from concurrent.futures import ThreadPoolExecutor
import time
import tempfile
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

from companion import core, web2api_worker as worker


class Web2APIWorkerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        (self.root / "data").mkdir()
        self.config = core.default_config()
        self.config["enabled"] = True
        self.config["allowed_groups"] = ["synthetic-group"]
        self.config["outbound_groups"] = ["synthetic-group"]
        self.config["provider"]["mode"] = "web2api"
        self.config["provider"]["auto_web_control"] = True
        self._write_config()
        self.item = {"id": "synthetic-request-1", "status": "pending", "group_id": "synthetic-group", "prompt": "synthetic test"}
        core.init(self.root)
        self._seed_request()

    def tearDown(self):
        self.tmp.cleanup()

    def _write_config(self):
        (self.root / "config.json").write_text(json.dumps(self.config), encoding="utf-8")

    def _seed_request(self):
        with core.open_db(self.root) as db:
            db.execute(
                "INSERT INTO messages(id,group_id,user_id,user_name,text,origin,directed,ts,received_at) "
                "VALUES(?,?,?,?,?,?,?,?,?)",
                ("msg1","synthetic-group","user1","tester","synthetic test","origin",1,core.now(),core.now()),
            )
            db.execute(
                "INSERT INTO requests(id,source_message_id,group_id,origin,prompt,status,created_at,updated_at) "
                "VALUES(?,?,?,?,?,?,?,?)",
                (self.item["id"],"msg1","synthetic-group","origin","synthetic test","pending",core.now(),core.now()),
            )

    def test_disabled_mode_cannot_access_provider(self):
        self.config["provider"]["auto_web_control"] = False
        self._write_config()
        with patch.object(worker, "provider_ready", side_effect=AssertionError("should not contact Web2API")):
            self.assertEqual(worker.process_once(self.root), "disabled")

    def test_parse_responses_text(self):
        result = {"status": "completed", "output": [
            {"type": "message", "content": [{"type": "output_text", "text": "测试回复"}]}
        ]}
        self.assertEqual(worker.extract_text(result), "测试回复")
        with self.assertRaises(ValueError):
            worker.extract_text({"status": "in_progress", "output": []})

    def test_one_request_claimed_once_after_success(self):
        with patch.object(worker, "provider_ready", return_value=(True, "ready")), \
             patch.object(worker, "_provider_config", return_value=("http://127.0.0.1:3210", "wb2_fake")), \
             patch.object(core, "pending_requests", return_value=[self.item]), \
             patch.object(worker, "_stream_reply", return_value=("synthetic-response", 25, 80)) as post:
            self.assertEqual(worker.process_once(self.root), "reply_ready")
            self.assertEqual(worker.process_once(self.root), "idle")
            post.assert_called_once()
        with core.open_db(self.root) as db:
            row = db.execute("SELECT status,reply_text FROM requests WHERE id=?", (self.item["id"],)).fetchone()
            self.assertEqual((row["status"],row["reply_text"]),("ready","synthetic-response"))
            row = db.execute("SELECT first_token_ms,model_total_ms FROM request_timings").fetchone()
            self.assertEqual(tuple(row),(25,80))

    def test_timeout_not_auto_retried(self):
        with patch.object(worker, "provider_ready", return_value=(True, "ready")), \
             patch.object(worker, "_provider_config", return_value=("http://127.0.0.1:3210", "wb2_fake")), \
             patch.object(core, "pending_requests", return_value=[self.item]), \
             patch.object(worker, "_stream_reply", side_effect=TimeoutError("synthetic timeout")) as post:
            self.assertEqual(worker.process_once(self.root), "failed_no_retry")
            self.assertEqual(worker.process_once(self.root), "idle")
            post.assert_called_once()
        with core.open_db(self.root) as db:
            self.assertEqual(db.execute("SELECT status FROM requests").fetchone()[0], "model_uncertain")
            self.assertEqual(db.execute("SELECT error_type FROM request_timings").fetchone()[0], "TimeoutError")
        self.assertIsNone(core.claim_outbound(self.root))

    def test_concurrent_workers_claim_exactly_once(self):
        with ThreadPoolExecutor(max_workers=8) as pool:
            outcomes = list(pool.map(lambda _: worker._mark_attempt(self.item["id"], self.root), range(12)))
        self.assertEqual(sum(outcomes), 1)
        with core.open_db(self.root) as db:
            self.assertEqual(db.execute("SELECT status FROM requests").fetchone()[0], "generating")
            self.assertEqual(db.execute("SELECT COUNT(*) FROM web2api_attempts").fetchone()[0], 1)

    def test_disconnected_wait_then_resume_new_request(self):
        with patch.object(worker, "provider_ready", side_effect=[(False, "offline"), (True, "ready")]), \
             patch.object(worker, "_provider_config", return_value=("http://127.0.0.1:3210", "wb2_fake")), \
             patch.object(core, "pending_requests", return_value=[self.item]), \
             patch.object(worker, "_stream_reply", return_value=("recovered", 19, 40)) as stream:
            self.assertEqual(worker.process_once(self.root), "not_ready:offline")
            with core.open_db(self.root) as db:
                self.assertEqual(db.execute("SELECT status FROM requests").fetchone()[0], "pending")
            self.assertEqual(worker.process_once(self.root), "reply_ready")
            stream.assert_called_once()

    def test_second_group_request_waits_for_first_ready(self):
        with core.open_db(self.root) as db:
            db.execute("INSERT INTO messages VALUES(?,?,?,?,?,?,?,?,?)",
                       ("msg2","synthetic-group","user2","tester","hello","origin2",1,core.now(),core.now()))
            db.execute("INSERT INTO requests(id,source_message_id,group_id,origin,prompt,status,created_at,updated_at) "
                       "VALUES(?,?,?,?,?,?,?,?)",
                       ("synthetic-request-2","msg2","synthetic-group","origin2","hello","pending",core.now(),core.now()))
        self.assertTrue(worker._mark_attempt(self.item["id"],self.root))
        self.assertFalse(worker._mark_attempt("synthetic-request-2",self.root))

    def test_ingest_replay_and_unapproved_group(self):
        msg = {
            "group_id": "synthetic-group", "message_id": "new-message",
            "user_id": "synthetic-user", "origin": "synthetic-origin",
            "text": "gpt synthetic", "directed": True,
        }
        self.assertTrue(core.ingest_message(msg, self.root)["queued"])
        self.assertEqual(core.ingest_message(msg, self.root)["reason"], "duplicate")
        msg.update({"group_id": "unapproved", "message_id": "new-unapproved"})
        self.assertEqual(core.ingest_message(msg, self.root)["reason"], "group_not_allowed")
        with core.open_db(self.root) as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM messages").fetchone()[0], 2)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM requests").fetchone()[0], 2)

    def test_inbound_permission_does_not_enable_qq_outbound(self):
        with core.open_db(self.root) as db:
            db.execute("UPDATE requests SET status='ready'")
        self.config["outbound_groups"] = []
        self._write_config()
        self.assertIsNone(core.claim_outbound(self.root))

    def test_manual_recovery_model_uncertain_is_explicit(self):
        with core.open_db(self.root) as db:
            db.execute("UPDATE requests SET status='model_uncertain'")
        core.submit_reply(self.item["id"], "manual fallback", self.root)
        with core.open_db(self.root) as db:
            row = db.execute("SELECT status,reply_text FROM requests").fetchone()
            self.assertEqual(tuple(row), ("ready", "manual fallback"))

    def test_stale_model_and_send_are_not_retried(self):
        stale=(dt.datetime.now(dt.timezone.utc)-dt.timedelta(minutes=10)).isoformat(timespec="seconds")
        with core.open_db(self.root) as db:
            db.execute("UPDATE requests SET status='generating',updated_at=?", (stale,))
            db.execute("INSERT INTO web2api_attempts VALUES(?,?,?,?)",(self.item["id"],"attempted",None,stale))
        core.init(self.root)
        with core.open_db(self.root) as db:
            self.assertEqual(db.execute("SELECT status FROM requests").fetchone()[0],"model_uncertain")
            self.assertEqual(db.execute("SELECT state FROM web2api_attempts").fetchone()[0],"model_uncertain")
            db.execute("UPDATE requests SET status='sending',updated_at=?", (stale,))
            db.execute("INSERT INTO deliveries(request_id,outcome,updated_at) VALUES(?,?,?)",(self.item["id"],"unknown",stale))
        core.init(self.root)
        with core.open_db(self.root) as db:
            self.assertEqual(db.execute("SELECT status FROM requests").fetchone()[0],"uncertain")
            self.assertEqual(db.execute("SELECT outcome FROM deliveries").fetchone()[0],"uncertain")

    def test_outbound_allowlist_is_enforced(self):
        with core.open_db(self.root) as db:
            db.execute("UPDATE requests SET group_id='unapproved',status='ready'")
        self.assertIsNone(core.claim_outbound(self.root))
        with core.open_db(self.root) as db:
            db.execute("UPDATE requests SET group_id='synthetic-group'")
        self.assertEqual(core.claim_outbound(self.root)["id"],self.item["id"])
        self.assertIsNone(core.claim_outbound(self.root))
        core.ack_outbound(self.item["id"],"sent",root=self.root)
        with core.open_db(self.root) as db:
            self.assertGreaterEqual(db.execute("SELECT qq_total_ms FROM request_timings").fetchone()[0],0)

    def test_local_sse_first_token_and_total_latency(self):
        class Server(BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass
            def do_POST(self):
                length=int(self.headers["Content-Length"])
                payload=json.loads(self.rfile.read(length))
                assert payload["stream"] is True and payload["model"]==worker.MODEL
                self.send_response(200)
                self.send_header("Content-Type","text/event-stream")
                self.end_headers()
                time.sleep(0.045)
                for event in (
                    {"type":"response.output_text.delta","delta":"合成"},
                    {"type":"response.output_text.delta","delta":"回复"},
                ):
                    self.wfile.write(("data: "+json.dumps(event,ensure_ascii=False)+"\n\n").encode())
                    self.wfile.flush()
                    time.sleep(0.025)
                self.wfile.write(b'data: {"type":"response.completed"}\n\n')
                self.wfile.flush()
        http=ThreadingHTTPServer(("127.0.0.1",0),Server)
        thread=threading.Thread(target=http.serve_forever,daemon=True)
        thread.start()
        try:
            text,first,total=worker._stream_reply(f"http://127.0.0.1:{http.server_port}","wb2_synthetic","hi",timeout=4)
            self.assertEqual(text,"合成回复")
            self.assertGreaterEqual(first,30)
            self.assertGreaterEqual(total,first)
        finally:
            http.shutdown()
            http.server_close()
            thread.join(timeout=2)

    def test_broken_stream_never_yields_partial_reply(self):
        class Server(BaseHTTPRequestHandler):
            def log_message(self,*_):
                pass
            def do_POST(self):
                self.rfile.read(int(self.headers["Content-Length"]))
                self.send_response(200)
                self.send_header("Content-Type","text/event-stream")
                self.end_headers()
                self.wfile.write(b'data: {"type":"response.output_text.delta","delta":"partial"}\n\n')
        http=ThreadingHTTPServer(("127.0.0.1",0),Server)
        thread=threading.Thread(target=http.serve_forever,daemon=True);thread.start()
        try:
            with self.assertRaises(ValueError):
                worker._stream_reply(f"http://127.0.0.1:{http.server_port}","wb2_synthetic","hi",timeout=3)
        finally:
            http.shutdown();http.server_close();thread.join(timeout=2)


if __name__ == "__main__":
    unittest.main()
