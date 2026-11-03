"""Project contracts, bounded proposals, and reproducible input identity."""

import hashlib
import base64
import json
import math
import platform
import re
import string
import subprocess
import sys
from pathlib import Path


class ConfigError(ValueError):
    pass


def _require(condition, message):
    if not condition:
        raise ConfigError(message)


def _number(value):
    return type(value) in (int, float) and math.isfinite(value)


def inside(project, relative):
    """Keep user-declared evidence and output paths within their project."""
    _require(isinstance(relative, str) and relative, "Expected a nonempty relative path")
    raw = Path(relative)
    _require(not raw.is_absolute() and ".." not in raw.parts, f"Unsafe path: {relative}")
    path = (Path(project).resolve() / raw).resolve()
    _require(path.is_relative_to(Path(project).resolve()), f"Path escapes project: {relative}")
    return path


def _argv(command, label):
    _require(isinstance(command, list) and command and all(isinstance(v, str) and v and "\0" not in v for v in command), f"{label} must be a nonempty argv list")


def _positive(value, label, integer=False):
    _require(_number(value) and value > 0 and (not integer or type(value) is int), f"{label} must be positive")


def _parameter(name, spec, value):
    kind = spec.get("type")
    valid = {"integer": type(value) is int, "number": _number(value), "string": isinstance(value, str), "boolean": type(value) is bool}
    _require(valid.get(kind, False), f"Parameter {name} must be {kind}")
    if "enum" in spec:
        _require(any(type(value) is type(item) and value == item for item in spec["enum"]), f"Parameter {name} outside enum")
    if kind in ("integer", "number"):
        if "min" in spec:
            _require(value >= spec["min"], f"Parameter {name} below min")
        if "max" in spec:
            _require(value <= spec["max"], f"Parameter {name} above max")
    if kind == "string":
        _require("\0" not in value, f"NUL in parameter {name}")


def validate_decision(config, decision):
    _require(isinstance(decision, dict), "Decision must be an object")
    _require(decision.get("action") in ("run", "stop", "hold"), "Decision action must be run, stop or hold")
    _require(isinstance(decision.get("reason"), str) and decision["reason"].strip(), "Decision needs a reason")
    if decision["action"] == "run":
        experiment = decision.get("experiment")
        _require(isinstance(experiment, str) and experiment in config["experiments"], "Undeclared experiment")
        specs = config["experiments"][experiment]["parameters"]
        values = decision.get("parameters")
        _require(isinstance(values, dict) and values.keys() == specs.keys(), "Parameters must exactly match declared names")
        for name, spec in specs.items():
            _parameter(name, spec, values[name])
    return decision


def render_command(config, decision, attempt, *, evaluator=False):
    spec = config["evaluator"] if evaluator else config["experiments"][decision["experiment"]]
    values = {"python": sys.executable, "project": config["_project"], "attempt": str(Path(attempt).resolve())}
    if not evaluator:
        values.update(decision["parameters"])
    result = []
    for arg in spec["command"]:
        for _, field, fmt, conversion in string.Formatter().parse(arg):
            if field is not None:
                _require(field in values and not fmt and not conversion, f"Unsupported command placeholder: {field}")
        result.append(arg.format_map(values))
    return result


def load(project):
    project = Path(project).resolve()
    state = project / ".crucible"
    _require(not state.is_symlink(), "State directory must not be a symlink")
    config = json.loads((state / "config.json").read_text())
    _require(isinstance(config, dict) and type(config.get("version")) is int and config["version"] == 1, "Expected configuration version 1")
    _require(isinstance(config.get("goal"), str) and config["goal"].strip(), "A research goal is required")
    config["_project"] = str(project)
    limits = config.setdefault("limits", {})
    _require(isinstance(limits, dict), "limits must be an object")
    for key, default in {"max_runs": 8, "max_calls": 12, "call_timeout_seconds": 120, "debate_rounds": 1}.items():
        limits.setdefault(key, default)
    _positive(limits["max_runs"], "max_runs", integer=True)
    _positive(limits["call_timeout_seconds"], "call_timeout_seconds")
    for key in ("max_calls", "debate_rounds"):
        _require(type(limits[key]) is int and limits[key] >= 0, f"{key} must be a nonnegative integer")
    models = config.setdefault("models", [])
    _require(isinstance(models, list), "models must be a list")
    names = set()
    identities = set()
    for model in models:
        _require(isinstance(model, dict), "Model specification must be an object")
        name = model.get("name")
        _require(isinstance(name, str) and re.fullmatch(r"[A-Za-z0-9_-]+", name) and name not in names, "Model names must be unique simple identifiers")
        names.add(name)
        _require(model.get("backend") in ("opencode", "claude"), "Model backend must be opencode or claude")
        _require(isinstance(model.get("model"), str) and model["model"].strip(), "Explicit model ID is required")
        identity = (model["backend"], model["model"].split("#", 1)[0])
        _require(identity not in identities, "Review models must have distinct declared identities; effort variants alone are not different models")
        identities.add(identity)
        model.setdefault("command", [model["backend"]])
        _argv(model["command"], "Model command")
        if "effort" in model:
            _require(isinstance(model["effort"], str) and model["effort"], "effort must be a string")
    if models:
        config.setdefault("planner", models[0]["name"])
        _require(config["planner"] in names, "planner must name a configured model")
    experiments = config.get("experiments")
    _require(isinstance(experiments, dict) and experiments, "At least one experiment is required")
    for name, spec in experiments.items():
        _require(re.fullmatch(r"[A-Za-z0-9_-]+", name) and isinstance(spec, dict), "Invalid experiment name/specification")
        _argv(spec.get("command"), f"Experiment {name} command")
        specs = spec.setdefault("parameters", {})
        _require(isinstance(specs, dict), "Parameter declarations must be an object")
        for key, parameter in specs.items():
            _require(re.fullmatch(r"[A-Za-z][A-Za-z0-9_]*", key) and key not in ("python", "project", "attempt"), "Invalid or reserved parameter name")
            _require(isinstance(parameter, dict) and parameter.get("type") in ("number", "integer", "string", "boolean"), "Unsupported parameter type")
            if "enum" in parameter:
                _require(isinstance(parameter["enum"], list) and parameter["enum"], "enum must be nonempty")
            for bound in ("min", "max"):
                if bound in parameter:
                    _require(parameter["type"] in ("number", "integer") and _number(parameter[bound]), "Numeric bounds must be finite")
            _require(parameter.get("min", -math.inf) <= parameter.get("max", math.inf), "min exceeds max")
        artifacts = spec.get("artifacts")
        _require(isinstance(artifacts, list) and artifacts and all(isinstance(v, str) and v for v in artifacts) and len(set(artifacts)) == len(artifacts), "Declare distinct required artifacts")
        for artifact in artifacts:
            inside(project, artifact)
        _require(isinstance(spec.get("inputs"), list) and spec["inputs"], "Declare experiment inputs for provenance")
        if spec.get("timeout_seconds") is not None:
            _positive(spec["timeout_seconds"], "experiment timeout_seconds")
    evaluator = config.get("evaluator")
    _require(isinstance(evaluator, dict), "Declare an evaluator")
    _argv(evaluator.get("command"), "Evaluator command")
    _require(isinstance(evaluator.get("inputs"), list) and evaluator["inputs"], "Declare evaluator inputs for provenance")
    evaluator.setdefault("timeout_seconds", 30)
    _positive(evaluator["timeout_seconds"], "evaluator timeout_seconds")
    for spec in [*experiments.values(), evaluator]:
        for relative in spec["inputs"]:
            path = inside(project, relative)
            _require(path.is_file() and ".crucible" not in Path(relative).parts, f"Missing or invalid input: {relative}")
    validate_decision(config, config.get("initial"))
    for name, spec in experiments.items():
        params = {key: "" for key in spec["parameters"]}
        render_command(config, {"experiment": name, "parameters": params}, state / "validation")
    render_command(config, {}, state / "validation", evaluator=True)
    return config


def _sha(path):
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def fingerprint(config):
    public = {key: value for key, value in config.items() if not key.startswith('_')}
    return hashlib.sha256(json.dumps(public, sort_keys=True, allow_nan=False).encode()).hexdigest()


def provenance(config, decision):
    project = Path(config["_project"])
    experiment = config["experiments"][decision["experiment"]]
    paths = sorted(set(experiment["inputs"] + config["evaluator"]["inputs"]))
    inputs, snapshots = {}, {}
    for relative in paths:
        path = inside(project, relative)
        inputs[relative] = _sha(path)
        # Keep small source inputs recoverable without embedding large datasets.
        if path.stat().st_size <= 1024 * 1024:
            snapshots[relative] = base64.b64encode(path.read_bytes()).decode("ascii")
    head = subprocess.run(["git", "-C", str(project), "rev-parse", "HEAD"], capture_output=True, text=True)
    diff = ""
    if head.returncode == 0:
        change = subprocess.run(["git", "-C", str(project), "diff", "--no-ext-diff", "--binary", "HEAD", "--", *paths], capture_output=True, text=True)
        _require(change.returncode == 0, "Could not capture declared-input Git diff")
        diff = change.stdout
    return {"config_sha256": fingerprint(config),
            "inputs": inputs, "input_snapshots_base64": snapshots,
            "git": {"head": head.stdout.strip() if head.returncode == 0 else None, "diff": diff},
            "python": sys.version, "platform": platform.platform()}
