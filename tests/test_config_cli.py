import contextlib
import io
import json
import math
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from crucible.cli import init, main
from crucible.config import ConfigError, load, provenance, render_command, validate_decision


class ConfigCliTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="crucible-cli-test-")
        self.project = Path(self.temp.name)
        init(self.project)
        self.config_path = self.project / ".crucible/config.json"

    def tearDown(self):
        self.temp.cleanup()

    def edit(self, transform):
        config = json.loads(self.config_path.read_text())
        transform(config)
        self.config_path.write_text(json.dumps(config))

    def test_no_overwrite(self):
        before = self.config_path.read_bytes()
        with self.assertRaises(ConfigError):
            init(self.project)
        self.assertEqual(before, self.config_path.read_bytes())
        other = self.project / "existing"
        other.mkdir()
        (other / "objective.py").write_text("user code")
        with self.assertRaises(ConfigError):
            init(other)
        self.assertEqual((other / "objective.py").read_text(), "user code")
        self.assertFalse((other / ".crucible").exists())

    def test_parameter_bounds_and_finite(self):
        config = load(self.project)
        for x in (11, float("nan"), float("inf"), True, "2"):
            decision = {**config["initial"], "parameters": {"x": x, "seed": 7}}
            with self.subTest(x=x), self.assertRaises(ConfigError):
                validate_decision(config, decision)
        with self.assertRaises(ConfigError):
            validate_decision(config, {**config["initial"], "parameters": {"x": 1, "seed": 7, "shell": "rm"}})

    def test_argv_shell_text_stays_one_argument(self):
        config = load(self.project)
        spec = config["experiments"]["objective"]
        spec["parameters"] = {"label": {"type": "string"}}
        spec["command"] = ["program", "{label}"]
        value = "$(touch escaped); --unsafe"
        decision = {"action": "run", "experiment": "objective", "parameters": {"label": value}, "reason": "test"}
        validate_decision(config, decision)
        self.assertEqual(render_command(config, decision, self.project), ["program", value])

    def test_forbidden_format_and_paths(self):
        for command in (["echo", "{x.__class__}"], ["echo", "{unknown}"], ["echo", "{x!r}"], ["echo", "{x:1000}"]):
            self.edit(lambda c: c["experiments"]["objective"].update(command=command))
            with self.subTest(command=command), self.assertRaises(ConfigError):
                load(self.project)
        self.edit(lambda c: c["experiments"]["objective"].update(command=["echo"], inputs=["../secret"]))
        with self.assertRaises(ConfigError):
            load(self.project)

    def test_escaping_symlink_rejected(self):
        (self.project / "escape").symlink_to("/etc/passwd")
        self.edit(lambda c: c["experiments"]["objective"].update(inputs=["escape"]))
        with self.assertRaises(ConfigError):
            load(self.project)

    def test_provenance_binds_declared_sources(self):
        config = load(self.project)
        before = provenance(config, config["initial"])
        self.assertIn("objective.py", before["input_snapshots_base64"])
        with (self.project / "objective.py").open("a") as stream:
            stream.write("\n# New experiment version\n")
        after = provenance(config, config["initial"])
        self.assertNotEqual(before["inputs"], after["inputs"])

    def test_cli_complete_local_loop_and_events(self):
        root = Path(__file__).resolve().parents[1]
        result = subprocess.run([sys.executable, "-m", "crucible", "run", "--project", str(self.project)], cwd=root, capture_output=True, text=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr)
        state = json.loads(result.stdout)
        self.assertEqual(state["state"], "stopped")
        self.assertEqual(state["run_count"], 4)
        self.assertEqual(state["call_count"], 0)
        event_output = subprocess.run([sys.executable, "-m", "crucible", "--project", str(self.project), "events"], cwd=root, capture_output=True, text=True, timeout=10)
        event = json.loads(event_output.stdout)[0]
        acknowledgement = subprocess.run([sys.executable, "-m", "crucible", "--project", str(self.project), "ack", str(event["id"])], cwd=root, capture_output=True, text=True, timeout=10)
        self.assertEqual(acknowledgement.returncode, 0, acknowledgement.stderr)

    def test_cli_validation_failure_is_json_and_nonzero(self):
        self.edit(lambda c: c.update(initial={"action": "run", "experiment": "missing", "parameters": {}, "reason": "Invalid"}))
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = main(["--project", str(self.project), "validate"])
        self.assertEqual(code, 1)
        self.assertIn("error", json.loads(err.getvalue()))

    def test_status_does_not_initialize_an_unrelated_folder(self):
        other = self.project / "unrelated"
        other.mkdir()
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(main(["status", "--project", str(other)]), 1)
        self.assertFalse((other / ".crucible").exists())

    def test_status_and_report_omit_inline_source_snapshots(self):
        from crucible import engine
        state = engine.run(self.project, once=True)
        self.assertNotIn("input_snapshots_base64", json.dumps(state))
        self.assertNotIn("input_snapshots_base64", engine.report(self.project))

    def test_macos_job_identity_uses_process_start_and_command(self):
        from unittest.mock import patch
        from crucible import job
        completed = subprocess.CompletedProcess([], 0, "S Tue Nov  3 10:00:00 2026 python job.py token", "")
        with patch.object(job.sys, "platform", "darwin"), patch.object(job.subprocess, "run", return_value=completed):
            identity = job.identity(123)
            self.assertEqual(identity["pid"], 123)
            self.assertIn("job.py token", identity["process"])
        completed.stdout = "Z Tue Nov  3 10:00:00 2026 zombie"
        with patch.object(job.sys, "platform", "darwin"), patch.object(job.subprocess, "run", return_value=completed):
            self.assertIsNone(job.identity(123))

    def test_opencode_link_uses_local_shim_and_preserves_existing_plugin(self):
        from crucible.cli import link_opencode
        result = link_opencode(self.project)
        entry = Path(result["plugin"]) / "index.js"
        source = entry.read_text()
        self.assertIn("file:///", source)
        self.assertIn("bin/crucible", source)
        self.assertIn("options:", source)
        command = self.project / ".opencode/commands/crucible-debate.md"
        self.assertIn('$ARGUMENTS', command.read_text())
        self.assertIn('action "debate"', command.read_text())
        with self.assertRaises(ConfigError):
            link_opencode(self.project)
        self.assertEqual(entry.read_text(), source)

    def test_existing_debate_command_is_not_overwritten(self):
        from crucible.cli import link_opencode
        command = self.project / '.opencode/commands/crucible-debate.md'
        command.parent.mkdir(parents=True)
        command.write_text('User-authored command')
        with self.assertRaises(ConfigError):
            link_opencode(self.project)
        self.assertEqual(command.read_text(), 'User-authored command')
        self.assertFalse((self.project / '.opencode/plugins/crucible').exists())

    def test_debate_cli_missing_question_fails_without_launch(self):
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(main(['debate', '--project', str(self.project)]), 1)
        self.assertFalse((self.project / '.crucible/debates').exists())

    def test_debate_cli_saves_report_without_running_an_experiment(self):
        from unittest.mock import patch
        self.edit(lambda c: c.update(models=[{'name': 'a', 'backend': 'opencode', 'model': 'test/a'}, {'name': 'b', 'backend': 'opencode', 'model': 'test/b'}]))
        review = {'supported_findings': ['A question was supplied'], 'concerns': [], 'evidence_refs': ['question'], 'needs_rebuttal': False, 'next_test': {'hypothesis': 'Inspect actual evidence', 'predictions': {'observed': 'Resolve with evidence'}}}
        conclusion = {'conclusion': 'Insufficient empirical evidence', 'agreements': [], 'disagreements': [], 'next_checks': ['Run a diagnostic'], 'evidence_refs': ['question']}
        def invoke(_model, prompt, *_args):
            return {'text': json.dumps(conclusion if 'Synthesize all reviews' in prompt else review)}
        output = io.StringIO()
        with patch('crucible.debate.providers.invoke', side_effect=invoke), contextlib.redirect_stdout(output):
            code = main(['debate', '--project', str(self.project), '--question', 'Why might loss rise?'])
        self.assertEqual(code, 0)
        case = json.loads(output.getvalue())
        self.assertEqual(case['status'], 'complete')
        self.assertFalse((self.project / '.crucible/attempts').exists())
        report = self.project / '.crucible/debates' / case['id'] / 'REPORT.md'
        self.assertIn(conclusion['conclusion'], report.read_text())

    def test_review_example_blocks_without_models(self):
        from crucible import engine
        self.edit(lambda c: c["evaluator"]["command"].append("--review"))
        state = engine.run(self.project)
        self.assertEqual(state["state"], "failed")
        self.assertEqual(state["run_count"], 2)
        self.assertEqual(state["call_count"], 0)
        self.assertIn("planner", state["last_error"])

    def test_attempt_retains_immutable_configuration(self):
        from crucible import engine
        engine.run(self.project, once=True)
        snapshot = self.project / ".crucible/attempts/1/config.json"
        before = snapshot.read_bytes()
        self.edit(lambda c: c.update(goal="A later scope"))
        self.assertEqual(snapshot.read_bytes(), before)
        self.assertNotEqual(json.loads(before)["goal"], "A later scope")

    def test_operator_can_continue_a_hold_with_audited_bounded_decision(self):
        from crucible import engine
        decision = {"action": "hold", "reason": "Wait for operator input"}
        engine.decide(self.project, decision)
        self.assertEqual(engine.run(self.project)["state"], "held")
        engine.decide(self.project, load(self.project)["initial"])
        self.assertEqual(engine.run(self.project, once=True)["run_count"], 1)
        self.assertTrue(any(e["kind"] == "operator_decision" for e in engine.events(self.project)))

    def test_effort_variants_do_not_count_as_two_models(self):
        self.edit(lambda c: c.update(models=[
            {"name": "a", "backend": "opencode", "model": "provider/model#high"},
            {"name": "b", "backend": "opencode", "model": "provider/model#low"}]))
        with self.assertRaises(ConfigError):
            load(self.project)


if __name__ == "__main__":
    unittest.main()
