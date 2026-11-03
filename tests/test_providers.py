import json
import os
from pathlib import Path
import sys
import subprocess
import time
import tempfile
import unittest

from crucible.providers import ProviderError, ProviderUncertain, invoke, recover


def opencode(text="OK", reason="stop"):
    return "\n".join(json.dumps(r) for r in [
        {"type": "step_start", "sessionID": "session", "part": {"messageID": "message"}},
        {"type": "text", "sessionID": "session", "part": {"messageID": "message", "text": text}},
        {"type": "step_finish", "sessionID": "session", "part": {
            "messageID": "message", "reason": reason, "tokens": {"input": 10, "output": 1}, "cost": 0}}])


def claude(**updates):
    result = {"type": "result", "subtype": "success", "is_error": False,
              "result": "OK", "session_id": "session", "stop_reason": "end_turn",
              "terminal_reason": "completed", "modelUsage": {"observed-model": {"inputTokens": 10}}}
    result.update(updates)
    return json.dumps(result)


class ProviderTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.script = self.root / "mock_cli.py"
        self.script.write_text('''import json, pathlib, sys
with pathlib.Path("starts.log").open("a") as count: count.write("start\\n")
packet = sys.stdin.read()
pathlib.Path("invocation.json").write_text(json.dumps({"argv":sys.argv[3:],"prompt":packet,"cwd":str(pathlib.Path.cwd())}))
sys.stdout.write(pathlib.Path(sys.argv[1]).read_text())
sys.stderr.write("mock stderr")
sys.exit(int(sys.argv[2]))
''')

    def call(self, output, backend="opencode", code=0, timeout=5):
        fixture = self.root / "fixture.json"
        fixture.write_text(output)
        spec = {"backend": backend, "model": "requested-model", "effort": "high",
                "command": [sys.executable, str(self.script), str(fixture), str(code)]}
        return invoke(spec, "Evidence packet", self.root / "call", timeout)

    def test_opencode_success_and_restrictions(self):
        result = self.call(opencode())
        self.assertEqual(result["text"], "OK")
        self.assertIsNone(result["observed_model"])
        self.assertEqual(result["requested_model"], "requested-model")
        call = self.root / "call"
        actual = json.loads((call / "workspace/invocation.json").read_text())
        self.assertEqual(actual["prompt"], "Evidence packet")
        self.assertIn("--standalone", actual["argv"])
        config = json.loads((call / "workspace/opencode.json").read_text())
        self.assertEqual(config["agents"]["crucible-reviewer"]["permissions"][0]["effect"], "deny")
        self.assertEqual({p["action"] for p in config["policies"]}, {"tool.use", "mcp.use", "skill.use", "plugin.use"})
        for name in ("prompt.txt", "stdout.jsonl", "stderr.log", "request.json", "response.json"):
            self.assertTrue((call / name).exists(), name)
        self.assertEqual(json.loads((call / "request.json").read_text())["exit_code"], 0)

    def test_claude_success_and_restrictions(self):
        result = self.call(claude(), "claude")
        self.assertEqual(result["observed_model"], "observed-model")
        args = json.loads((self.root / "call/workspace/invocation.json").read_text())["argv"]
        self.assertIn("--safe-mode", args)
        self.assertNotIn("--bare", args)
        self.assertEqual(args[args.index("--tools") + 1], "")
        self.assertIn("--strict-mcp-config", args)
        self.assertEqual(args[args.index("--effort") + 1], "high")

    def test_reject_partial_opencode(self):
        with self.assertRaisesRegex(ProviderError, "terminal"):
            self.call(opencode().rsplit("\n", 1)[0])

    def test_reject_length_limit(self):
        with self.assertRaises(ProviderError):
            self.call(opencode(reason="length"))

    def test_reject_error_record(self):
        with self.assertRaisesRegex(ProviderError, "error"):
            self.call(json.dumps({"type": "error", "sessionID": "session", "error": {"message": "failed"}}))

    def test_reject_tool(self):
        raw = json.dumps({"type": "tool_use", "sessionID": "session"}) + "\n" + opencode()
        with self.assertRaisesRegex(ProviderError, "tool"):
            self.call(raw)

    def test_reject_empty_text(self):
        with self.assertRaisesRegex(ProviderError, "empty"):
            self.call(opencode(text=" "))

    def test_claude_success_subtype_can_be_error(self):
        with self.assertRaisesRegex(ProviderError, "failure"):
            self.call(claude(is_error=True, terminal_reason="api_error", api_error="usage_limit_reached"), "claude")

    def test_claude_reject_truncation(self):
        with self.assertRaisesRegex(ProviderError, "incomplete"):
            self.call(claude(stop_reason="max_tokens"), "claude")

    def test_nonzero_preserves_raw(self):
        with self.assertRaisesRegex(ProviderError, "status 7"):
            self.call(opencode(), code=7)
        self.assertEqual((self.root / "call/stdout.jsonl").read_text(), opencode())
        self.assertEqual(json.loads((self.root / "call/error.json").read_text())["exit_code"], 7)
        self.assertFalse((self.root / "call/response.json").exists())

    def test_bad_json_preserves_failure(self):
        with self.assertRaises(ProviderError):
            self.call("not JSON")
        self.assertTrue((self.root / "call/error.json").exists())

    def test_reuse_does_not_destroy_evidence(self):
        self.call(opencode())
        self.assertEqual(self.call("bad")["text"], "OK")
        self.assertEqual((self.root / "call/stdout.jsonl").read_text(), opencode())

    def test_timeout_kills_descendant(self):
        self.script.write_text('''import pathlib, subprocess, sys, time
child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
pathlib.Path("child.pid").write_text(str(child.pid))
print("started", flush=True)
time.sleep(60)
''')
        with self.assertRaises(ProviderError):
            self.call("", timeout=0.5)
        pid = int((self.root / "call/workspace/child.pid").read_text())
        status = Path(f"/proc/{pid}/stat")
        if status.exists():
            self.assertEqual(status.read_text().split()[2], "Z")
        self.assertIn("started", (self.root / "call/stdout.jsonl").read_text())
        self.assertEqual(json.loads((self.root / "call/error.json").read_text())["type"], "TimeoutExpired")


    def launch_caller(self, timeout=0.8, delay=60):
        fixture = self.root / "fixture.json"
        fixture.write_text(opencode())
        self.script.write_text(self.script.read_text().replace(
            "packet = sys.stdin.read()", f"import time; time.sleep({delay})\npacket = sys.stdin.read()"))
        spec = {"backend": "opencode", "model": "requested-model",
                "command": [sys.executable, str(self.script), str(fixture), "0"]}
        caller = subprocess.Popen([sys.executable, "-c",
            "import json,sys; from pathlib import Path; from crucible.providers import invoke; "
            "invoke(json.loads(sys.argv[1]), 'Evidence packet', Path(sys.argv[2]), float(sys.argv[3]))",
            json.dumps(spec), str(self.root / "call"), str(timeout)],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.addCleanup(lambda: caller.poll() is None and caller.kill())
        deadline = time.monotonic() + 5
        while not (self.root / "call/workspace/starts.log").exists():
            if caller.poll() is not None or time.monotonic() >= deadline:
                self.fail("caller did not start the provider")
            time.sleep(0.01)
        return caller, spec

    def test_actor_timeout_survives_caller_sigkill(self):
        caller, spec = self.launch_caller()
        caller.kill()
        caller.wait(timeout=2)
        call = self.root / "call"
        # recover waits for the known actor; no controller remains to enforce timeout.
        with self.assertRaisesRegex(ProviderError, "TimeoutExpired"):
            recover(call)
        receipt = json.loads((call / "terminal.json").read_text())
        self.assertEqual(receipt["status"], "error")
        self.assertEqual(receipt["token"], json.loads((call / "invocation.json").read_text())["token"])
        with self.assertRaisesRegex(ProviderError, "TimeoutExpired"):
            invoke(spec, "Evidence packet", call, 0.8)
        self.assertEqual((call / "workspace/starts.log").read_text().count("start"), 1)
        child = json.loads((call / "provider-child.json").read_text())["identity"]
        status = Path(f"/proc/{child['pid']}/stat")
        if status.exists():
            self.assertEqual(status.read_text().split()[2], "Z")

    def test_completed_actor_recovers_without_new_call(self):
        caller, spec = self.launch_caller(timeout=3, delay=0.4)
        caller.kill()
        caller.wait(timeout=2)
        call = self.root / "call"
        self.assertEqual(recover(call)["text"], "OK")
        self.assertEqual(invoke(spec, "Evidence packet", call, 3)["text"], "OK")
        self.assertEqual((call / "workspace/starts.log").read_text().count("start"), 1)

    def test_unknown_invocation_refuses_recovery_and_retry(self):
        self.call(opencode())
        call = self.root / "call"
        (call / "terminal.json").unlink()
        with self.assertRaises(ProviderUncertain):
            recover(call)
        with self.assertRaises(ProviderUncertain):
            self.call(opencode())
        self.assertEqual((call / "workspace/starts.log").read_text().count("start"), 1)

    def test_partial_or_mismatched_receipt_is_uncertain(self):
        self.call(opencode())
        call = self.root / "call"
        valid = (call / "terminal.json").read_text()
        for broken in ["{", "{}", valid.replace('"status": "success"', '"status": "unknown"')]:
            (call / "terminal.json").write_text(broken)
            with self.assertRaises(ProviderUncertain):
                recover(call)
        receipt = json.loads(valid)
        receipt["token"] = "different-invocation"
        (call / "terminal.json").write_text(json.dumps(receipt))
        with self.assertRaises(ProviderUncertain):
            recover(call)
        (call / "terminal.json").write_text(valid)
        (call / "response.json").write_text('{"text":"tampered"}')
        with self.assertRaises(ProviderUncertain):
            recover(call)

    def test_intent_without_runner_identity_is_uncertain(self):
        self.call(opencode())
        call = self.root / "call"
        (call / "runner-started.json").unlink()
        with self.assertRaises(ProviderUncertain):
            recover(call)
        with self.assertRaises(ProviderUncertain):
            self.call(opencode())

    def test_different_prompt_never_reuses_or_restarts_call(self):
        self.call(opencode())
        invocation = json.loads((self.root / "call/invocation.json").read_text())
        with self.assertRaises(ProviderUncertain):
            invoke(invocation["spec"], "Other evidence", self.root / "call", 5)
        self.assertEqual((self.root / "call/workspace/starts.log").read_text().count("start"), 1)


if __name__ == "__main__":
    unittest.main()
