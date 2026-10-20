"""Synthetic numeric balance sheet tests; never interact with real QQ."""
import json
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from companion import core, server, tuning


class QQNumberBalanceTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.root=Path(self.tmp.name)
        (self.root/"data").mkdir()
        self.cfg=core.default_config()
        self.cfg["enabled"]=True
        self.cfg["allowed_groups"]=["synthetic"]
        self.cfg["outbound_groups"]=[]
        self.save()
        core.init(self.root)

    def tearDown(self):
        self.tmp.cleanup()

    def save(self):
        (self.root/"config.json").write_text(json.dumps(self.cfg),encoding="utf-8")

    def test_sheet_has_all_live_controls_and_three_presets(self):
        state=tuning.snapshot(self.root)
        self.assertEqual(len(state["items"]),16)
        self.assertEqual(set(state["presets"]),{"潜水","日常","活跃"})
        self.assertIn("ambient_probability",{i["id"] for i in state["items"]})
        self.assertIn("codex_cli_daily_limit",{i["id"] for i in state["items"]})
        self.assertNotIn("enabled",{i["id"] for i in state["items"]})
        self.assertNotIn("allowed_groups",{i["id"] for i in state["items"]})

    def test_changes_persist_but_never_enable_model_or_qq_send(self):
        before=tuning.snapshot(self.root)
        result=tuning.update({"ambient_probability":0.10,
                              "ambient_cooldown_seconds":300,
                              "codex_cli_daily_limit":8},before["version"],self.root)
        self.assertNotEqual(result["version"],before["version"])
        cfg=core.config(self.root)
        self.assertAlmostEqual(cfg["engagement"]["ambient_probability"],0.10)
        self.assertEqual(cfg["engagement"]["ambient_cooldown_seconds"],300)
        self.assertEqual(cfg["provider"]["codex_cli_daily_limit"],8)
        self.assertEqual(cfg["provider"]["mode"],"manual_web")
        self.assertEqual(cfg["outbound_groups"],[])
        self.assertNotIn("local_bridge_token",json.dumps(result))
        self.assertNotIn("allowed_groups",json.dumps(result))

    def test_stale_version_refuses_overwrite(self):
        old=tuning.snapshot(self.root)
        tuning.update({"ambient_probability":0.07},old["version"],self.root)
        with self.assertRaises(ValueError):
            tuning.update({"ambient_probability":0.1},old["version"],self.root)
        self.assertAlmostEqual(core.config(self.root)["engagement"]["ambient_probability"],0.07)

    def test_type_limits_steps_and_guardrails(self):
        violations=[
            {"enabled":1}, {"outbound_groups":1}, {"ambient_probability":True},
            {"ambient_probability":-0.1}, {"ambient_probability":0.9},
            {"ambient_probability":0.035}, {"ambient_max_per_hour":1.5},
            {"ambient_cooldown_seconds":-1}, {"codex_cli_daily_limit":60},
        ]
        for changes in violations:
            with self.subTest(changes=changes),self.assertRaises(ValueError):
                tuning.update(changes,tuning.snapshot(self.root)["version"],self.root)
        with self.assertRaises(ValueError):
            tuning.update({"tail_seconds":8,"max_collection_seconds":2},
                          tuning.snapshot(self.root)["version"],self.root)
        with self.assertRaises(ValueError):
            tuning.update({"codex_cli_hourly_limit":12,"codex_cli_daily_limit":4},
                          tuning.snapshot(self.root)["version"],self.root)
        self.assertEqual(core.config(self.root)["outbound_groups"],[])

    def test_original_config_without_new_codex_keys_can_open_sheet(self):
        c=core.config(self.root)
        for name in list(c["provider"]):
            if name.startswith("codex_cli_"):
                del c["provider"][name]
        core.save_config(c,self.root)
        result=tuning.snapshot(self.root)
        keys={p["id"]:p["value"] for p in result["items"]}
        self.assertGreater(keys["codex_cli_daily_limit"],0)
        tuning.update({"ambient_probability":0.11},result["version"],self.root)
        self.assertEqual(core.config(self.root)["provider"]["mode"],"manual_web")
        refreshed=tuning.snapshot(self.root)
        tuning.update({"codex_cli_hourly_limit":3},refreshed["version"],self.root)
        self.assertEqual(core.config(self.root)["provider"]["codex_cli_hourly_limit"],3)

    def test_presets_all_within_safety_bounds(self):
        for label,preset in tuning.PRESETS.items():
            with self.subTest(label=label):
                with tempfile.TemporaryDirectory() as d:
                    root=Path(d);(root/"data").mkdir()
                    (root/"config.json").write_text(json.dumps(core.default_config()),encoding="utf-8")
                    tuning.update(preset,tuning.snapshot(root)["version"],root)

    def test_protected_real_http_numeric_roundtrip_and_401(self):
        original_snapshot=tuning.snapshot
        original_update=tuning.update
        with patch.object(server.core,"token",return_value="synthetic-token"),\
             patch.object(server.tuning,"snapshot",side_effect=lambda root=self.root:original_snapshot(root)),\
             patch.object(server.tuning,"update",side_effect=lambda v,expected:original_update(v,expected,self.root)):
            http=ThreadingHTTPServer(("127.0.0.1",0),server.Handler)
            thread=threading.Thread(target=http.serve_forever,daemon=True);thread.start()
            base=f"http://127.0.0.1:{http.server_port}"
            def call(path,payload=None,token=True):
                headers={}
                if token: headers["X-Companion-Token"]="synthetic-token"
                if payload is not None:headers["Content-Type"]="application/json"
                req=Request(base+path,headers=headers,
                            data=json.dumps(payload).encode("utf-8") if payload is not None else None)
                with urlopen(req,timeout=3) as response:return json.load(response)
            try:
                with self.assertRaises(HTTPError) as err:call("/api/tuning",token=False)
                self.assertEqual(err.exception.code,401)
                values=call("/api/tuning")
                receipt=call("/api/tuning",{"expected_version":values["version"],
                                           "values":{"followup_probability":0.61}})
                self.assertAlmostEqual(next(x for x in receipt["items"] if x["id"]=="followup_probability")["value"],0.61)
                with self.assertRaises(HTTPError) as err:
                    call("/api/tuning",{"expected_version":values["version"],
                                        "values":{"outbound_groups":1}})
                self.assertEqual(err.exception.code,400)
            finally:
                http.shutdown();http.server_close();thread.join(timeout=2)


if __name__=="__main__":
    unittest.main()
