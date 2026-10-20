"""Offline tests for QQ Luna assignment; never change real Multica."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from companion import core, luna_setup


class LunaAssignmentTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.config = core.default_config()
        self.config["allowed_groups"] = [luna_setup.TEST_GROUP]
        self.config["enabled"] = True
        self.config["provider"]["mode"] = "manual_web"
        (self.root / "config.json").write_text(json.dumps(self.config), encoding="utf-8")

    def tearDown(self):
        self.temp.cleanup()

    def test_unscoped_refuses_before_any_operation(self):
        with patch.dict("os.environ", {"MULTICA_TOKEN": ""}), \
             patch.object(luna_setup, "_invoke", side_effect=AssertionError("must not call Multica")):
            with self.assertRaises(PermissionError):
                luna_setup.assign(self.root)
        self.assertEqual(core.config(self.root), self.config)

    def test_new_luna_and_run_only_created_once_and_disabled(self):
        recorded = []
        def fake(arguments):
            recorded.append(arguments)
            if arguments[:2] == ["agent", "list"]:
                return {"agents": []}
            if arguments[:2] == ["agent", "create"]:
                return {"agent": {"id": "synthetic-luna-id"}}
            if arguments[:2] == ["autopilot", "list"]:
                return {"autopilots": []}
            if arguments[:2] == ["autopilot", "create"]:
                return {"autopilot": {"id": "synthetic-autopilot-id"}}
            raise AssertionError(arguments)
        with patch.dict("os.environ", {"MULTICA_TOKEN": "mat_synthetic"}), \
             patch.object(luna_setup, "_invoke", side_effect=fake):
            result = luna_setup.assign(self.root)
        self.assertTrue(result["staged_only"])
        self.assertEqual(result["agent_id"], "synthetic-luna-id")
        self.assertEqual(result["autopilot_id"], "synthetic-autopilot-id")
        agent_args = next(a for a in recorded if a[:2] == ["agent", "create"])
        self.assertIn("gpt-6-luna", agent_args)
        self.assertIn("private", agent_args)
        autopilot_args = next(a for a in recorded if a[:2] == ["autopilot", "create"])
        self.assertIn("run_only", autopilot_args)
        self.assertNotIn("create_issue", autopilot_args)
        cfg = core.config(self.root)
        self.assertFalse(cfg["multica"]["enabled"])
        self.assertEqual(cfg["multica"]["autopilot_id"], "synthetic-autopilot-id")
        self.assertEqual(cfg["provider"]["mode"], "manual_web")
        self.assertEqual(cfg["outbound_groups"], [])

    def test_reuses_exact_owned_objects_without_mutation(self):
        called = []
        def fake(args):
            called.append(args)
            if args[:2] == ["agent", "list"]:
                return {"agents": [{"id": "existing-luna", "name": luna_setup.NAME,
                                    "runtime_id": luna_setup.RUNTIME_ID, "model": luna_setup.MODEL}]}
            if args[:2] == ["autopilot", "list"]:
                return {"autopilots": [{"id": "existing-auto", "title": luna_setup.TITLE}]}
            if args[:2] == ["autopilot", "get"]:
                return {"autopilot": {"id": "existing-auto", "mode": "run_only",
                                      "agent_id": "existing-luna"}}
            raise AssertionError("Must not create or mutate existing entities")
        with patch.dict("os.environ", {"MULTICA_TOKEN": "mat_synthetic"}), \
             patch.object(luna_setup, "_invoke", side_effect=fake):
            result = luna_setup.assign(self.root)
        self.assertEqual(result["autopilot_id"], "existing-auto")
        self.assertNotIn("create", [a[1] for a in called])

    def test_rejects_wrong_model_without_reassigning_existing_luna(self):
        def fake(args):
            if args[:2] == ["agent", "list"]:
                return {"agents": [
                    {"id": "existing-pick", "name": "执行-Codex-Luna",
                     "runtime_id": luna_setup.RUNTIME_ID, "model": luna_setup.MODEL},
                    {"id": "wrong-model", "name": luna_setup.NAME,
                     "runtime_id": luna_setup.RUNTIME_ID, "model": "different"}]}
            raise AssertionError("Must not mutate a mismatched agent")
        with patch.dict("os.environ", {"MULTICA_TOKEN": "mat_synthetic"}), \
             patch.object(luna_setup, "_invoke", side_effect=fake):
            with self.assertRaises(RuntimeError):
                luna_setup.assign(self.root)
        self.assertEqual(core.config(self.root), self.config)


if __name__ == "__main__":
    unittest.main()
