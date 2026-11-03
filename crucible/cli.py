"""Command-line entry points; research state belongs to the selected project."""

import argparse
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

from . import __version__
from .config import ConfigError, load


def _binary(name):
    found = shutil.which(name)
    if found:
        return found
    candidate = Path.home() / (".opencode/bin/opencode" if name == "opencode" else ".local/bin/claude")
    return str(candidate) if candidate.is_file() else name


def init(project, *, review=False, opencode_model=None, claude_model=None):
    project = Path(project).resolve()
    state = project / ".crucible"
    if state.exists():
        raise ConfigError(".crucible already exists; init never replaces project state")
    for filename in ("objective.py", "evaluate.py"):
        if (project / filename).exists():
            raise ConfigError(f"Refusing to replace {filename}")
    project.mkdir(parents=True, exist_ok=True)
    state.mkdir()
    source = Path(__file__).parent / "templates"
    for filename in ("objective.py", "evaluate.py"):
        shutil.copyfile(source / filename, project / filename)
    models = []
    for name, backend, model in (("gpt", "opencode", opencode_model), ("claude", "claude", claude_model)):
        if model:
            models.append({"name": name, "backend": backend, "command": [_binary(backend)], "model": model})
    evaluator_command = ["{python}", "{project}/evaluate.py"] + (["--review"] if review else [])
    config = {"version": 1, "goal": "Minimize the synthetic same-seed quadratic objective; target loss < 0.001",
              "limits": {"max_runs": 8, "max_calls": 12, "call_timeout_seconds": 120, "debate_rounds": 1},
              "models": models, "initial": {"action": "run", "experiment": "objective", "parameters": {"x": 0, "seed": 7}, "reason": "Registered synthetic baseline"},
              "experiments": {"objective": {"command": ["{python}", "{project}/objective.py", "--x", "{x}", "--seed", "{seed}"],
                  "parameters": {"x": {"type": "number", "min": -10, "max": 10}, "seed": {"type": "integer", "min": 0, "max": 100000}},
                  "artifacts": ["metrics.json"], "inputs": ["objective.py"], "timeout_seconds": 60}},
              "evaluator": {"command": evaluator_command, "inputs": ["evaluate.py"], "timeout_seconds": 30}}
    if models:
        config["planner"] = models[0]["name"]
    (state / "config.json").write_text(json.dumps(config, indent=2) + "\n")
    load(project)
    return {"state": "initialized", "project": str(project), "origin": "synthetic", "config": str(state / "config.json"), "review": review}


def doctor(project):
    checks = {"python": sys.version, "python_supported": sys.version_info >= (3, 11), "git": shutil.which("git")}
    checks["providers"] = []
    config_path = project / ".crucible" / "config.json"
    if config_path.exists():
        for spec in load(project)["models"]:
            executable = spec["command"][0]
            binary = shutil.which(executable)
            item = {"name": spec["name"], "backend": spec["backend"], "requested_model": spec["model"], "executable": binary, "authentication": "not checked"}
            if binary:
                result = subprocess.run([*spec["command"], "--version"], capture_output=True, text=True, timeout=15)
                item["version"] = result.stdout.strip()
                item["returncode"] = result.returncode
            checks["providers"].append(item)
    checks["ok"] = checks["python_supported"] and all(p["executable"] and p.get("returncode") == 0 for p in checks["providers"])
    return checks


def link_opencode(project):
    load(project)
    from .config import inside
    target = inside(project, ".opencode/plugins/crucible")
    command_path = inside(project, ".opencode/commands/crucible-debate.md")
    if target.exists():
        raise ConfigError("OpenCode Crucible plugin already exists; refusing to overwrite it")
    if command_path.exists():
        raise ConfigError("OpenCode crucible-debate command already exists; refusing to overwrite it")
    module = Path(__file__).parent / "integrations/opencode/index.js"
    source_launcher = Path(__file__).resolve().parent.parent / "bin/crucible"
    command = [sys.executable, str(source_launcher)] if source_launcher.is_file() else [sys.executable, "-m", "crucible"]
    target.mkdir(parents=True)
    (target / "package.json").write_text(json.dumps({"name": "crucible-project-bridge", "type": "module"}) + "\n")
    shim = "import bridge from " + json.dumps(module.resolve().as_uri()) + ";\n"
    shim += "export default { id: bridge.id, setup(ctx) { return bridge.setup({ ...ctx, options: { ...ctx.options, command: " + json.dumps(command) + " } }); } };\n"
    (target / "index.js").write_text(shim)
    command_path.parent.mkdir(parents=True, exist_ok=True)
    command_path.write_text('---\ndescription: Start an explicit Crucible research debate\n---\nUse the native crucible tool with action "debate" and the following question. This discussion must not start an experiment or change an active loop.\n\n$ARGUMENTS\n')
    return {"state": "linked", "project": str(project), "plugin": str(target), "tool": "crucible", "command": "/crucible-debate", "restart": "Reopen this project's OpenCode session to load the plugin"}


def _spawn(project, arguments, log):
    root = Path(__file__).resolve().parent.parent
    environment = os.environ.copy()
    environment["PYTHONPATH"] = str(root) + (os.pathsep + environment["PYTHONPATH"] if environment.get("PYTHONPATH") else "")
    command = [sys.executable, "-m", "crucible", "--project", str(project), *arguments]
    with log.open("ab") as stream:
        worker = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=stream, stderr=stream, cwd=project, env=environment, start_new_session=True)
    return {"state": "worker_spawned", "pid": worker.pid, "project": str(project), "log": str(log), "note": "Use status to verify worker ownership and progress"}


def _background(project, *, once=False):
    load(project)
    return _spawn(project, ["run", *(["--once"] if once else [])], project / ".crucible/worker.log")


def _debate(project, args):
    from . import debate
    if args.resume:
        if args.question or args.evidence or args.rounds is not None or args.max_calls is not None:
            raise ConfigError("Resume reuses its captured question, evidence and budget")
        case = debate.status(project, args.resume)
    else:
        if args.retry:
            raise ConfigError("--retry requires --resume CASE_ID")
        if not args.question:
            raise ConfigError("Provide --question TEXT or --resume CASE_ID")
        case = debate.start(project, args.question, evidence=args.evidence, rounds=args.rounds, max_calls=args.max_calls)
    if args.background:
        case_dir = project / ".crucible/debates" / case["id"]
        arguments = ["debate", "--resume", case["id"], *(["--retry"] if args.retry else [])]
        worker = _spawn(project, arguments, case_dir / "worker.log")
        return {**worker, "state": "debate_queued", "case_id": case["id"], "case_path": str(case_dir), "report": str(case_dir / "REPORT.md")}
    return debate.run(project, case["id"], retry=args.retry)


def parser():
    result = argparse.ArgumentParser(prog="crucible", description="Bounded research execution and conditional model review")
    result.add_argument("--version", action="version", version=__version__)
    result.add_argument("--project", type=Path, default=Path.cwd(), help="Research project directory")
    commands = result.add_subparsers(dest="command", required=True)
    for name in ("init", "validate", "link-opencode", "run", "status", "pause", "resume", "stop", "cancel", "reconcile", "retry", "decide", "debate", "events", "ack", "doctor", "report"):
        child = commands.add_parser(name)
        child.add_argument("--project", type=Path, default=argparse.SUPPRESS)
        if name == "init":
            child.add_argument("--review", action="store_true", help="Include a registered stagnation scenario requiring configured models")
            child.add_argument("--opencode-model", help="Explicit provider/model#variant from your OpenCode catalog")
            child.add_argument("--claude-model", help="Explicit Claude Code model ID")
        if name in ("run", "resume", "retry"):
            child.add_argument("--once", action="store_true")
            child.add_argument("--background", action="store_true")
        if name == "events":
            child.add_argument("--all", action="store_true")
        if name == "ack":
            child.add_argument("event_id", type=int)
        if name == "decide":
            child.add_argument("--file", type=Path, required=True, help="Validated run/stop/hold decision JSON")
        if name == "debate":
            child.add_argument("--question")
            child.add_argument("--evidence", action="append", help="Project-relative evidence file; repeat for multiple files")
            child.add_argument("--rounds", type=int, help="Maximum requested rebuttal rounds (default: 0)")
            child.add_argument("--max-calls", type=int, help="Per-debate call cap, separate from the experiment loop")
            child.add_argument("--resume", help="Resume an existing case without changing its evidence")
            child.add_argument("--retry", action="store_true", help="Explicitly retry a known failed call on resume")
            child.add_argument("--background", action="store_true")
    return result


def main(argv=None):
    args = parser().parse_args(argv)
    project = args.project.resolve()
    try:
        if args.command == "init":
            output = init(project, review=args.review, opencode_model=args.opencode_model, claude_model=args.claude_model)
        elif args.command == "validate":
            config = load(project)
            output = {"valid": True, "project": str(project), "experiments": list(config["experiments"]), "models": [m["name"] for m in config["models"]]}
        elif args.command == "doctor":
            output = doctor(project)
        elif args.command == "link-opencode":
            output = link_opencode(project)
        elif args.command == "debate":
            output = _debate(project, args)
        else:
            from . import engine
            if args.command in ("run", "resume", "retry"):
                if args.command == "resume":
                    engine.control(project, "resume")
                elif args.command == "retry":
                    engine.retry(project)
                output = _background(project, once=args.once) if args.background else engine.run(project, once=args.once)
            elif args.command in ("pause", "stop", "cancel"):
                output = engine.control(project, args.command)
            elif args.command == "events":
                output = engine.events(project, all_events=args.all)
            elif args.command == "ack":
                output = engine.ack(project, args.event_id)
            elif args.command == "decide":
                output = engine.decide(project, json.loads(args.file.read_text()))
            elif args.command == "report":
                output = engine.report(project)
                print(output)
                return 0
            else:
                output = getattr(engine, args.command)(project)
        print(json.dumps(output, indent=2, ensure_ascii=False, allow_nan=False))
        if isinstance(output, dict):
            if (output.get("state") or output.get("status")) in ("failed", "held", "reconciliation_required", "call_budget", "run_budget", "budget_exhausted"):
                return 2
            if output.get("ok") is False:
                return 1
        return 0
    except (ValueError, RuntimeError, OSError, subprocess.TimeoutExpired) as error:
        print(json.dumps({"error": str(error), "type": type(error).__name__}, ensure_ascii=False), file=sys.stderr)
        return 1
