"""Offline tests of the official ChatGPT-plan protocol, never using an account."""
import base64
from concurrent.futures import ThreadPoolExecutor
import datetime as dt
import json
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

from companion import core, siwc_auth, siwc_worker


class ChatGPTPlanWorkerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        (self.root / "data").mkdir()
        self.config = core.default_config()
        self.config["enabled"] = True
        self.config["allowed_groups"] = ["synthetic-group"]
        self.config["outbound_groups"] = []
        self.config["engagement"]["tail_seconds"] = 0
        self.config["engagement"]["max_collection_seconds"] = 0
        self.config["provider"].update({
            "mode": "siwc", "siwc_enabled": True,
            "model": "synthetic-authorized-model",
            "siwc_activated_at": core.now(),
        })
        (self.root / "config.json").write_text(json.dumps(self.config), encoding="utf-8")
        core.init(self.root)

    def tearDown(self):
        self.temp.cleanup()

    def ingest(self, seq=1):
        return core.ingest_message({
            "group_id": "synthetic-group", "message_id": str(seq),
            "user_id": "synthetic-sender", "user_name": "tester",
            "text": "gpt synthetic prompt", "origin": "synthetic:qq",
            "directed": True,
        }, self.root)

    def test_disabled_gate_performs_no_auth_and_no_model_call(self):
        self.config["provider"]["siwc_enabled"] = False
        core.save_config(self.config, self.root)
        self.ingest()
        with patch.object(siwc_auth, "current_access_token",
                          side_effect=AssertionError("Must not read credentials")):
            self.assertEqual(siwc_worker.process_once(self.root), "disabled")

    def test_no_old_qq_request_automatically_enters_subscription(self):
        self.ingest()
        with core.open_db(self.root) as db:
            db.execute("UPDATE requests SET created_at=?",
                       ((dt.datetime.now(core.UTC)-dt.timedelta(minutes=10)).isoformat(),))
        with patch.object(siwc_auth,"current_access_token",
                          side_effect=AssertionError("Must not use account quota")):
            self.assertEqual(siwc_worker.process_once(self.root), "idle")
        with core.open_db(self.root) as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM siwc_attempts").fetchone()[0],0)

    def test_approved_response_enters_ready_queue_only_once(self):
        self.ingest()
        with patch.object(siwc_auth, "current_access_token", return_value="synthetic-token"), \
             patch.object(siwc_worker, "_stream_response",
                          return_value=("synthetic reply",34,92)) as stream:
            self.assertEqual(siwc_worker.process_once(self.root), "reply_ready")
            self.assertEqual(siwc_worker.process_once(self.root), "idle")
            stream.assert_called_once()
        with core.open_db(self.root) as db:
            row=db.execute("SELECT status,reply_text FROM requests").fetchone()
            self.assertEqual(tuple(row),("ready","synthetic reply"))
            metric=db.execute("SELECT first_token_ms,model_total_ms FROM request_timings").fetchone()
            self.assertEqual(tuple(metric),(34,92))
        self.assertIsNone(core.claim_outbound(self.root))

    def test_stream_error_marks_uncertain_without_replay(self):
        self.ingest()
        with patch.object(siwc_auth,"current_access_token",return_value="synthetic-token"), \
             patch.object(siwc_worker,"_stream_response",
                          side_effect=TimeoutError("synthetic timeout")) as stream:
            self.assertEqual(siwc_worker.process_once(self.root),"unknown_no_retry")
            self.assertEqual(siwc_worker.process_once(self.root),"idle")
            stream.assert_called_once()
        with core.open_db(self.root) as db:
            self.assertEqual(db.execute("SELECT status FROM requests").fetchone()[0],"model_uncertain")
            self.assertEqual(db.execute("SELECT state FROM siwc_attempts").fetchone()[0],"model_uncertain")
            self.assertEqual(db.execute("SELECT error_type FROM request_timings").fetchone()[0],"TimeoutError")
        self.assertIsNone(core.claim_outbound(self.root))

    def test_credentials_unavailable_do_not_mark_request_as_attempted(self):
        self.ingest()
        with patch.object(siwc_auth,"current_access_token",
                          side_effect=FileNotFoundError("no consent")):
            self.assertEqual(siwc_worker.process_once(self.root),"auth_not_ready")
        with core.open_db(self.root) as db:
            self.assertEqual(db.execute("SELECT status FROM requests").fetchone()[0],"pending")
            self.assertEqual(db.execute("SELECT COUNT(*) FROM siwc_attempts").fetchone()[0],0)

    def test_sse_has_only_supported_official_fields_and_latency(self):
        payloads = []
        class Fixture(BaseHTTPRequestHandler):
            def log_message(self,*args):pass
            def do_POST(self):
                payloads.append(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
                self.send_response(200)
                self.send_header("Content-Type","text/event-stream")
                self.end_headers()
                time.sleep(.030)
                for event in ({"type":"response.output_text.delta","delta":"synthetic "},
                              {"type":"response.output_text.delta","delta":"completed"}):
                    self.wfile.write(("data: "+json.dumps(event)+"\n\n").encode("utf-8"))
                    self.wfile.flush()
                time.sleep(.014)
                self.wfile.write(b'data: {"type":"response.completed"}\n\n')
                self.wfile.flush()
        http=ThreadingHTTPServer(("127.0.0.1",0),Fixture)
        thread=threading.Thread(target=http.serve_forever,daemon=True);thread.start()
        try:
            with patch.object(siwc_auth,"RESOURCE",f"http://127.0.0.1:{http.server_port}/v1"):
                text, first, total=siwc_worker._stream_response("synthetic prompt","fake-token","test-model",3)
            self.assertEqual(text,"synthetic completed")
            self.assertGreaterEqual(first,15)
            self.assertGreaterEqual(total,first)
            self.assertEqual(set(payloads[0]),{"model","input","store","stream"})
            self.assertIs(payloads[0]["store"],False)
            self.assertIs(payloads[0]["stream"],True)
            self.assertEqual(payloads[0]["input"],[{"role":"user","content":"synthetic prompt"}])
        finally:
            http.shutdown();http.server_close();thread.join(timeout=2)

    def test_incomplete_sse_does_not_yield_partial_reply(self):
        class Fixture(BaseHTTPRequestHandler):
            def log_message(self,*_):pass
            def do_POST(self):
                self.rfile.read(int(self.headers["Content-Length"]))
                self.send_response(200)
                self.send_header("Content-Type","text/event-stream")
                self.end_headers()
                self.wfile.write(b'data: {"type":"response.output_text.delta","delta":"partial"}\n\n')
        http=ThreadingHTTPServer(("127.0.0.1",0),Fixture)
        thread=threading.Thread(target=http.serve_forever,daemon=True);thread.start()
        try:
            with patch.object(siwc_auth,"RESOURCE",f"http://127.0.0.1:{http.server_port}/v1"):
                with self.assertRaises(ValueError):
                    siwc_worker._stream_response("synthetic prompt","fake-token","test-model",3)
        finally:
            http.shutdown();http.server_close();thread.join(timeout=2)

    def test_activation_requires_recent_smoke_and_never_enables_qq_send(self):
        self.config["provider"]["siwc_enabled"]=False
        self.config["provider"]["mode"]="manual_web"
        core.save_config(self.config,self.root)
        with self.assertRaises(FileNotFoundError):
            siwc_worker.activate("synthetic-authorized-model",self.root)
        siwc_auth._save_private(self.root/"data"/"siwc"/"model_test.json",
                                {"model":"synthetic-authorized-model","timestamp":int(time.time())})
        with patch.object(siwc_auth,"status",
                          return_value={"plan_permission":True,"token_current":True}):
            result=siwc_worker.activate("synthetic-authorized-model",self.root)
        self.assertTrue(result["new_requests_only"])
        config=core.config(self.root)
        self.assertEqual(config["provider"]["mode"],"siwc")
        self.assertFalse(config["provider"]["auto_web_control"])
        self.assertEqual(config["outbound_groups"],[])
        self.assertEqual(siwc_worker.deactivate(self.root)["mode"],"manual_web")

    def test_grant_requires_explicit_plan_scope(self):
        token={"access_token":"synthetic","refresh_token":"synthetic-refresh",
               "expires_in":3600,"scope":"openid profile email"}
        with self.assertRaises(PermissionError):
            siwc_auth._normalize_grant(token,"oaiapp_synthetic",
                                       "urn:uuid:synthetic",{"sub":"fake"})

    def test_concurrent_refresh_serializes_rotating_tokens(self):
        now=int(time.time())
        old={
            "issuer":siwc_auth.ISSUER, "client_id":"oaiapp_synthetic",
            "ext_agent_host_id":"urn:uuid:synthetic","subject":"subject",
            "scopes":["openid","resource.invoke",siwc_auth.REQUIRED_SCOPE],
            "access_token":"expired-access", "refresh_token":"old-refresh",
            "id_token":"synthetic-id", "expires_at":now-1,
        }
        siwc_auth._save_private(siwc_auth.credentials_path(self.root),old)
        response={
            "scope":"openid resource.invoke chatgpt.tokens.use.direct",
            "access_token":"new-access", "refresh_token":"new-refresh",
            "expires_in":3600,
        }
        with patch.object(siwc_auth,"_token_exchange",return_value=response) as exchange:
            with ThreadPoolExecutor(max_workers=6) as pool:
                tokens=list(pool.map(lambda _:siwc_auth.current_access_token(self.root),range(8)))
        self.assertEqual(tokens,["new-access"]*8)
        exchange.assert_called_once()
        stored=siwc_auth._read_json(siwc_auth.credentials_path(self.root))
        self.assertEqual(stored["refresh_token"],"new-refresh")
        self.assertEqual(stored["id_token"],"synthetic-id")

    def test_activation_does_not_allow_second_group(self):
        self.config["allowed_groups"]=["synthetic-group","another-group"]
        core.save_config(self.config,self.root)
        siwc_auth._save_private(siwc_auth._directory(self.root)/"model_test.json",{
            "model":"synthetic-authorized-model","timestamp":int(time.time())})
        with patch.object(siwc_auth,"status",return_value={"plan_permission":True}):
            with self.assertRaises(ValueError):
                siwc_worker.activate("synthetic-authorized-model",self.root)

    def test_validate_signed_openid_id_token_and_reject_nonce_change(self):
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import padding, rsa
        key=rsa.generate_private_key(public_exponent=65537,key_size=2048)
        numbers=key.public_key().public_numbers()
        def enc(obj):
            return base64.urlsafe_b64encode(json.dumps(obj).encode()).rstrip(b"=").decode()
        def num(i):
            return base64.urlsafe_b64encode(i.to_bytes((i.bit_length()+7)//8,"big")).rstrip(b"=").decode()
        jwks={"keys":[{"kid":"synthetic","kty":"RSA","use":"sig",
                       "n":num(numbers.n),"e":num(numbers.e)}]}
        header={"alg":"RS256","kid":"synthetic"}
        claims={"iss":siwc_auth.ISSUER,"aud":"oaiapp_synthetic","nonce":"nonce-good",
                "iat":int(time.time()),"exp":int(time.time()+3600),"sub":"synthetic-subject"}
        signed=enc(header)+"."+enc(claims)
        signature=key.sign(signed.encode(),padding.PKCS1v15(),hashes.SHA256())
        jwt=signed+"."+base64.urlsafe_b64encode(signature).rstrip(b"=").decode()
        self.assertEqual(siwc_auth.verify_id_token(jwt,"oaiapp_synthetic","nonce-good",
                                                   jwks)["sub"],"synthetic-subject")
        with self.assertRaises(ValueError):
            siwc_auth.verify_id_token(jwt,"oaiapp_synthetic","nonce-wrong",jwks)


if __name__=="__main__":
    unittest.main()
