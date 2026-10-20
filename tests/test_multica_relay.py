"""Offline QQ -> fixed Agent -> QQ-queue tests; never contact QQ/Multica."""
import datetime as dt
import json
import tempfile
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from http.server import ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from urllib.request import Request, urlopen
from urllib.error import HTTPError
import threading
import re

from companion import core, agent_queue, multica_bridge, server


class MulticaRelayTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        (self.root / "data").mkdir()
        self.cfg = core.default_config()
        self.cfg["enabled"] = True
        self.cfg["allowed_groups"] = ["synthetic-group"]
        self.cfg["outbound_groups"] = []
        self.cfg["engagement"]["tail_seconds"] = 0
        self.cfg["engagement"]["max_collection_seconds"] = 8
        self.cfg["multica"] = {"enabled": True,"test_group":"synthetic-group",
                               "autopilot_id":"synthetic-fixed-autopilot","event_max_age_seconds":120}
        self.save()
        core.init(self.root)

    def tearDown(self):
        self.tmp.cleanup()

    def save(self):
        (self.root / "config.json").write_text(json.dumps(self.cfg),encoding="utf-8")

    def ingest(self, number=1, sender="user1", directed=True, text="gpt 合成测试消息"):
        return core.ingest_message({"group_id":"synthetic-group", "message_id":str(number),
            "user_id":sender,"user_name":"tester","origin":"synthetic-origin",
            "text":text,"directed":directed},self.root)

    def wake_and_claim(self, run_id="run.synthetic.1"):
        job = agent_queue.reserve_wake(self.root)
        self.assertIsNotNone(job)
        claimed = agent_queue.claim_for_agent(run_id,self.root)
        self.assertIsNotNone(claimed)
        self.assertEqual(claimed["request_id"],job["request_id"])
        return claimed

    def test_directed_and_ambient_decisions_preserved(self):
        with patch.object(core,"in_quiet_hours",return_value=False):
            self.cfg["engagement"]["ambient_probability"] = 1.0
            self.save()
            self.assertTrue(self.ingest(directed=False)["queued"])
        self.assertTrue(self.ingest(2,sender="user2",directed=True)["queued"])

    def test_followup_must_obey_cooldown_and_hourly_cap(self):
        self.cfg["engagement"].update({"ambient_probability":0.0,"followup_probability":1.0,
                                       "ambient_cooldown_seconds":180,"ambient_max_per_hour":4})
        self.save()
        with core.open_db(self.root) as db:
            db.execute("INSERT INTO attention VALUES(?,?,?)",("synthetic-group","user1",core.now()))
        msg={"id":"synthetic-follows", "text":"这个内容值得继续聊聊", "user_id":"user1",
             "group_id":"synthetic-group","directed":False}
        with core.open_db(self.root) as db, patch.object(core,"in_quiet_hours",return_value=False):
            self.assertTrue(core._should_respond(db,msg,self.cfg))
            # Non-directed queued request within cooldown MUST block 42% followup.
            db.execute("INSERT INTO messages VALUES(?,?,?,?,?,?,?,?,?)",
                       ("oldmsg","synthetic-group","user2","t","text","origin",0,core.now(),core.now()))
            db.execute("INSERT INTO requests(id,source_message_id,group_id,origin,prompt,status,created_at,updated_at) "
                       "VALUES(?,?,?,?,?,?,?,?)",("oldreq","oldmsg","synthetic-group","origin","p","pending",core.now(),core.now()))
            self.assertFalse(core._should_respond(db,msg,self.cfg))
            db.execute("UPDATE messages SET received_at=? WHERE id='oldmsg'",
                       ((dt.datetime.now(core.UTC)-dt.timedelta(minutes=4)).isoformat(timespec="seconds"),))
            self.cfg["engagement"]["ambient_max_per_hour"] = 1
            self.assertFalse(core._should_respond(db,msg,self.cfg))
            self.cfg["engagement"]["ambient_max_per_hour"] = 4
            self.assertTrue(core._should_respond(db,msg,self.cfg))
            with patch.object(core,"in_quiet_hours",return_value=True):
                self.assertFalse(core._should_respond(db,msg,self.cfg))
            msg["directed"] = True
            self.assertTrue(core._should_respond(db,msg,self.cfg))

    def test_same_speaker_burst_merges_before_second_draw(self):
        first = self.ingest(1)
        second = self.ingest(2, text="同一个人的追加内容，不带gpt")
        self.assertEqual(second["reason"],"merged")
        self.assertEqual(first["request_id"], second["request_id"])
        with core.open_db(self.root) as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM requests").fetchone()[0],1)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM agent_jobs").fetchone()[0],1)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM messages").fetchone()[0],2)

    def test_intervening_speaker_breaks_burst_even_under_eight_seconds(self):
        first=self.ingest(1,sender="user1")
        self.cfg["engagement"]["ambient_probability"]=0
        self.save()
        self.ingest(2,sender="user2",directed=False,text="路过插一句别人的话")
        third=self.ingest(3,sender="user1",directed=True,text="gpt 这是同一用户的新一轮")
        self.assertTrue(third["queued"])
        self.assertNotEqual(first["request_id"],third["request_id"])
        with core.open_db(self.root) as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM requests").fetchone()[0],2)

    def test_prompt_order_is_stable_when_timestamps_match(self):
        self.ingest(1,sender="user1",text="gpt 第一条")
        self.ingest(2,sender="user1",text="第二条补充")
        self.ingest(3,sender="user1",text="第三条补充")
        with core.open_db(self.root) as db:
            history=core.build_prompt(db,"synthetic-group",self.cfg)
        self.assertLess(history.index("gpt 第一条"),history.index("第二条补充"))
        self.assertLess(history.index("第二条补充"),history.index("第三条补充"))

    def test_claim_idempotency_reply_and_outbound_gate(self):
        self.ingest()
        job=self.wake_and_claim()
        same=agent_queue.claim_for_agent(job["run_id"],self.root)
        self.assertEqual(same["event_id"],job["event_id"])
        result=agent_queue.finalize(job["run_id"],job["request_id"],job["event_id"],"reply","合成回答",self.root)
        self.assertEqual(result["state"],"done")
        again=agent_queue.finalize(job["run_id"],job["request_id"],job["event_id"],"reply","合成回答",self.root)
        self.assertTrue(again["duplicate"])
        with self.assertRaises(ValueError):
            agent_queue.finalize(job["run_id"],job["request_id"],job["event_id"],"silence",root=self.root)
        self.assertIsNone(core.claim_outbound(self.root))
        with core.open_db(self.root) as db:
            self.assertEqual(db.execute("SELECT status FROM requests").fetchone()[0],"ready")
            self.assertEqual(db.execute("SELECT outcome FROM agent_run_history").fetchone()[0],"done")

    def test_attention_updates_only_after_real_qq_sent_ack(self):
        self.ingest()
        job=self.wake_and_claim()
        agent_queue.finalize(job["run_id"],job["request_id"],job["event_id"],
                             "reply","synthetic prepared reply",root=self.root)
        with core.open_db(self.root) as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM attention").fetchone()[0],0)
        self.cfg["outbound_groups"]=["synthetic-group"]
        self.save()
        item=core.claim_outbound(self.root)
        self.assertEqual(item["id"],job["request_id"])
        with core.open_db(self.root) as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM attention").fetchone()[0],0)
        core.ack_outbound(item["id"],"sent",root=self.root)
        with core.open_db(self.root) as db:
            self.assertEqual(db.execute("SELECT member_id FROM attention").fetchone()[0],"user1")
            timing=db.execute("SELECT qq_total_ms FROM request_timings").fetchone()[0]
            self.assertGreaterEqual(timing,0)
            self.assertLess(timing,5000)

    def test_new_utc_timestamps_have_millisecond_precision(self):
        self.assertRegex(core.now(),r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{3}\+00:00$")
        self.ingest()
        with core.open_db(self.root) as db:
            stamp=db.execute("SELECT received_at FROM messages").fetchone()[0]
            self.assertRegex(stamp,r"\.\d{3}\+00:00$")

    def test_unknown_qq_ack_does_not_create_false_followup_attention(self):
        self.ingest()
        core.submit_reply(core.pending_requests(self.root)[0]["id"],"manual prepared reply",self.root)
        self.cfg["outbound_groups"]=["synthetic-group"]
        self.save()
        item=core.claim_outbound(self.root)
        core.ack_outbound(item["id"],"uncertain",root=self.root)
        with core.open_db(self.root) as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM attention").fetchone()[0],0)

    def test_wait_requires_next_speaker_message_new_event(self):
        self.ingest()
        job=self.wake_and_claim()
        agent_queue.finalize(job["run_id"],job["request_id"],job["event_id"],"wait",root=self.root)
        x=self.ingest(2, text="我再补充一点细节")
        self.assertEqual(x["reason"],"merged")
        next_job=agent_queue.reserve_wake(self.root)
        self.assertIsNotNone(next_job)
        self.assertNotEqual(next_job["event_id"],job["event_id"])
        with self.assertRaises(ValueError):
            agent_queue.finalize(job["run_id"],job["request_id"],job["event_id"],"reply","late",self.root)
        self.assertEqual(agent_queue.claim_for_agent("run.synthetic.2",self.root)["event_id"],next_job["event_id"])

    def test_silence_and_unknown_do_not_send(self):
        self.ingest()
        job=self.wake_and_claim()
        agent_queue.finalize(job["run_id"],job["request_id"],job["event_id"],"silence",root=self.root)
        self.assertIsNone(core.claim_outbound(self.root))
        self.ingest(2,sender="user2")
        job2=self.wake_and_claim("run.synthetic.2")
        agent_queue.finalize(job2["run_id"],job2["request_id"],job2["event_id"],"unknown",root=self.root)
        self.assertIsNone(core.claim_outbound(self.root))
        with self.assertRaises(ValueError):
            agent_queue.finalize(job2["run_id"],job2["request_id"],job2["event_id"],"reply","late",self.root)

    def test_global_busy_queue_and_concurrent_claim(self):
        self.ingest()
        self.ingest(2,sender="user2")
        with ThreadPoolExecutor(max_workers=6) as executor:
            results=list(executor.map(lambda _: agent_queue.reserve_wake(self.root),range(9)))
        self.assertEqual(sum(x is not None for x in results),1)
        with ThreadPoolExecutor(max_workers=6) as executor:
            answers=list(executor.map(lambda i: agent_queue.claim_for_agent("run."+str(i),self.root),range(8)))
        self.assertEqual(sum(x is not None for x in answers),1)
        taken=next(x for x in answers if x)
        agent_queue.finalize(taken["run_id"],taken["request_id"],taken["event_id"],"silence",root=self.root)
        self.assertIsNotNone(agent_queue.reserve_wake(self.root))

    def test_unsent_qq_reply_blocks_next_model_job(self):
        self.ingest()
        self.ingest(2,sender="user2")
        first = self.wake_and_claim()
        agent_queue.finalize(first["run_id"],first["request_id"],first["event_id"],
                             "reply","synthetic reply",root=self.root)
        # Even though the next model is idle, the group has an older QQ reply
        # waiting in the original AstrBot queue.
        self.assertIsNone(agent_queue.reserve_wake(self.root))
        self.cfg["outbound_groups"] = ["synthetic-group"]
        self.save()
        item = core.claim_outbound(self.root)
        self.assertEqual(item["id"],first["request_id"])
        self.assertIsNone(agent_queue.reserve_wake(self.root))
        core.ack_outbound(item["id"],"sent",root=self.root)
        self.assertIsNotNone(agent_queue.reserve_wake(self.root))

    def test_queue_backlog_survives_agent_timeout_window(self):
        self.ingest()
        self.ingest(2,sender="user2")
        first = agent_queue.reserve_wake(self.root)
        with core.open_db(self.root) as db:
            older = (dt.datetime.now(core.UTC)-dt.timedelta(seconds=160)).isoformat(timespec="seconds")
            db.execute("UPDATE agent_jobs SET created_at=? WHERE state='queued'",(older,))
            db.execute("UPDATE requests SET created_at=? WHERE id!=?",(older,first["request_id"]))
        # The active first job may be slow; other queued messages should not
        # be silently silenced at the shorter 120s run timeout.
        states=agent_queue.queue_status(self.root)["counts"]
        self.assertEqual(states.get("queued"),1)
        with core.open_db(self.root) as db:
            self.assertEqual(db.execute("SELECT status FROM requests WHERE id!=?",(first["request_id"],)).fetchone()[0],"pending")

    def test_status_poll_recovers_stale_waking_without_retrigger(self):
        self.ingest()
        agent_queue.reserve_wake(self.root)
        with core.open_db(self.root) as db:
            db.execute("UPDATE agent_jobs SET wake_ms=?",(agent_queue.milliseconds()-180000,))
        counts=agent_queue.queue_status(self.root)["counts"]
        self.assertEqual(counts.get("unknown"),1)
        self.assertIsNone(agent_queue.reserve_wake(self.root))
        with core.open_db(self.root) as db:
            self.assertEqual(db.execute("SELECT status FROM requests").fetchone()[0],"model_uncertain")

    def test_wake_timeout_becomes_unknown_and_never_rewakes(self):
        self.ingest()
        job=agent_queue.reserve_wake(self.root)
        with core.open_db(self.root) as db:
            db.execute("UPDATE agent_jobs SET wake_ms=?",(agent_queue.milliseconds()-180000,))
        self.assertIsNone(agent_queue.reserve_wake(self.root))
        with core.open_db(self.root) as db:
            self.assertEqual(db.execute("SELECT state FROM agent_jobs").fetchone()[0],"unknown")
            self.assertEqual(db.execute("SELECT status FROM requests").fetchone()[0],"model_uncertain")
        self.assertIsNone(agent_queue.claim_for_agent("run.late",self.root))

    def test_legacy_pending_not_enqueued_after_enable(self):
        self.cfg["multica"]["enabled"] = False
        self.save()
        self.ingest()
        self.cfg["multica"]["enabled"] = True
        self.save()
        self.assertIsNone(agent_queue.reserve_wake(self.root))

    def test_cli_trigger_is_bounded_and_does_not_create_issue(self):
        self.ingest()
        out=[]
        def fake_runner(args,**kwargs):
            out.append((args,kwargs));return SimpleNamespace(returncode=0)
        with patch.object(multica_bridge,"readiness",return_value=(True,"ready")):
            self.assertEqual(multica_bridge.dispatch_once(self.root,fake_runner),
                             "trigger_accepted_waiting_for_agent")
        self.assertEqual(out[0][0][:3],["multica","autopilot","trigger"])
        self.assertLessEqual(out[0][1]["timeout"],20)
        self.assertEqual(agent_queue.queue_status(self.root)["counts"].get("waking"),1)

    def test_failed_trigger_unknown_not_restarted(self):
        self.ingest()
        with patch.object(multica_bridge,"readiness",return_value=(True,"ready")):
            result=multica_bridge.dispatch_once(self.root,lambda *a,**kw:SimpleNamespace(returncode=1))
        self.assertEqual(result,"UNKNOWN_no_retry")
        self.assertIsNone(agent_queue.reserve_wake(self.root))
        with core.open_db(self.root) as db:
            self.assertEqual(db.execute("SELECT state FROM agent_jobs").fetchone()[0],"unknown")


    def test_http_wake_claim_and_result_real_loopback(self):
        """Verify real HTTP tool routing on a synthetic temporary database."""
        self.ingest()
        reserve,claim,finalize = agent_queue.reserve_wake,agent_queue.claim_for_agent,agent_queue.finalize
        get_status=agent_queue.queue_status
        with patch.object(server.core,"token",return_value="synthetic-token"), \
             patch.object(server.agent_queue,"reserve_wake",side_effect=lambda:reserve(self.root)), \
             patch.object(server.agent_queue,"claim_for_agent",side_effect=lambda run:claim(run,self.root)), \
             patch.object(server.agent_queue,"finalize",side_effect=lambda *args:finalize(*args,root=self.root)), \
             patch.object(server.agent_queue,"queue_status",side_effect=lambda:get_status(self.root)):
            http=ThreadingHTTPServer(("127.0.0.1",0),server.Handler)
            thread=threading.Thread(target=http.serve_forever,daemon=True)
            thread.start()
            def post(route,data,authenticated=True):
                headers={"Content-Type":"application/json"}
                if authenticated:headers["X-Companion-Token"]="synthetic-token"
                req=Request(f"http://127.0.0.1:{http.server_port}"+route,
                    data=json.dumps(data).encode(),headers=headers,method="POST")
                with urlopen(req,timeout=3) as result:
                    return json.load(result)
            try:
                with self.assertRaises(HTTPError) as denied:
                    post("/api/agent/wake",{},authenticated=False)
                self.assertEqual(denied.exception.code,401)
                w=post("/api/agent/wake",{})["item"]
                self.assertIsNotNone(w)
                self.assertIsNone(post("/api/agent/wake",{})["item"])
                c=post("/api/agent/claim",{"run_id":"synthetic.HTTP"})["item"]
                self.assertEqual(w["event_id"],c["event_id"])
                d={"run_id":"synthetic.HTTP","request_id":c["request_id"],
                   "event_id":c["event_id"],"decision":"reply","reply":"synthetic via HTTP"}
                self.assertEqual(post("/api/agent/result",d)["state"],"done")
                self.assertTrue(post("/api/agent/result",d)["duplicate"])
                with self.assertRaises(HTTPError) as rejected:
                    post("/api/agent/result",dict(d,event_id="stale"))
                self.assertEqual(rejected.exception.code,400)
                with core.open_db(self.root) as db:
                    self.assertEqual(db.execute("SELECT status FROM requests").fetchone()[0],"ready")
                self.assertIsNone(core.claim_outbound(self.root))
            finally:
                http.shutdown();http.server_close();thread.join(timeout=2)

    def test_disabled_or_wrong_group_no_dispatch(self):
        self.ingest()
        self.cfg["multica"]["enabled"]=False
        self.save()
        self.assertFalse(agent_queue.permitted(self.root))
        self.assertIsNone(agent_queue.reserve_wake(self.root))
        self.cfg["multica"]["enabled"]=True
        self.cfg["allowed_groups"]=["synthetic-group","target-group"]
        self.save()
        self.assertFalse(agent_queue.permitted(self.root))
        self.assertIsNone(agent_queue.reserve_wake(self.root))


if __name__ == "__main__":
    unittest.main()
