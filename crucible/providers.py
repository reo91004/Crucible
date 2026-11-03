"""Packet-only CLI calls; this restricts model tools, not the host OS."""

import hashlib
import json
import math
import os
from pathlib import Path
import platform
import signal
import subprocess
import sys
import time
import uuid
from datetime import datetime, timezone

from .job import atomic as _write, identity as _identity, alive


class ProviderError(RuntimeError):
    pass


class ProviderUncertain(ProviderError):
    pass


def _hash(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _read(path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise ProviderUncertain(f"Unverified provider record: {path.name}") from error


def _parse_opencode(raw):
    records = [json.loads(line) for line in raw.splitlines() if line.strip()]
    if not records or any(not isinstance(r, dict) for r in records):
        raise ProviderError("OpenCode returned no event objects")
    sessions = {r.get("sessionID") for r in records}
    if len(sessions) != 1 or not next(iter(sessions)):
        raise ProviderError("OpenCode returned missing or inconsistent session IDs")
    for record in records:
        if record.get("type") == "error" or record.get("error"):
            raise ProviderError("OpenCode emitted an error; see stdout.jsonl")
        if record.get("type") == "tool_use":
            raise ProviderError("OpenCode attempted a tool in a packet-only call")
    finishes = [r for r in records if r.get("type") == "step_finish"]
    if not finishes or records[-1].get("type") != "step_finish":
        raise ProviderError("OpenCode missing terminal step_finish")
    terminal = finishes[-1]["part"]
    if terminal.get("reason") != "stop":
        raise ProviderError("OpenCode did not finish normally: " + str(terminal.get("reason")))
    message = terminal.get("messageID")
    texts = [r["part"] for r in records if r.get("type") == "text"]
    if not message or any(p.get("messageID") != message for p in texts) or len(finishes) != 1:
        raise ProviderError("OpenCode returned multiple or inconsistent response steps")
    text = "".join(p["text"] for p in texts)
    return {"text": text, "session_id": next(iter(sessions)), "observed_model": None,
            "usage": {"tokens": terminal.get("tokens", {}), "cost": terminal.get("cost")}}


def _parse_claude(raw):
    result = json.loads(raw)
    if not isinstance(result, dict) or result.get("type") != "result":
        raise ProviderError("Claude missing terminal result")
    if result.get("is_error") is not False or result.get("subtype") != "success":
        raise ProviderError("Claude reported failure; see stdout.jsonl")
    if result.get("terminal_reason") not in (None, "completed"):
        raise ProviderError("Claude reported abnormal terminal_reason")
    if result.get("stop_reason") not in ("end_turn", "stop_sequence"):
        raise ProviderError("Claude returned an incomplete response")
    if result.get("permission_denials"):
        raise ProviderError("Claude attempted a denied tool in a packet-only call")
    models = result.get("modelUsage", {})
    return {"text": result.get("result"), "session_id": result.get("session_id"),
            "observed_model": next(iter(models)) if len(models) == 1 else None,
            "usage": {"usage": result.get("usage", {}), "modelUsage": models,
                      "total_cost_usd": result.get("total_cost_usd")}}


def _invoke(spec: dict, prompt: str, call_dir: Path, timeout: float, binding: dict) -> dict:
    """Actor-owned transport; the caller process never supervises its deadline."""
    workspace = call_dir / "workspace"
    workspace.mkdir()
    (call_dir / "stdout.jsonl").touch()
    (call_dir / "stderr.log").touch()
    backend = spec.get("backend")
    model = spec.get("model")
    command = spec.get("command", [backend])
    request = {"backend": backend, "requested_model": model, "effort": spec.get("effort"),
               "command": command, "timeout_seconds": timeout,
               "started_at": datetime.now(timezone.utc).isoformat(),
               "python": sys.version, "platform": platform.platform()}
    _write(call_dir / "request.json", request)
    started = time.monotonic()
    proc = None
    try:
        if backend not in ("opencode", "claude") or not isinstance(model, str) or not model:
            raise ProviderError("An explicit supported backend and model are required")
        if not isinstance(command, list) or not command or not all(isinstance(x, str) and x for x in command):
            raise ProviderError("command must be a nonempty argv prefix")
        if timeout <= 0:
            raise ProviderError("timeout must be positive")
        env = os.environ.copy()
        if backend == "opencode":
            deny = {"action": "*", "resource": "*", "effect": "deny"}
            config = {"agents": {"crucible-reviewer": {
                "mode": "primary", "description": "Packet-only reviewer",
                "system": "Review only the supplied evidence packet. Do not use tools.",
                "permissions": [deny]}}, "permissions": [deny], "snapshots": False,
                "policies": [{"action": action, "resource": "*", "effect": "deny"}
                             for action in ("tool.use", "mcp.use", "skill.use", "plugin.use")]}
            _write(workspace / "opencode.json", config)
            for key in ("OPENCODE_CONFIG_CONTENT", "OPENCODE_SERVER", "OPENCODE_CLI_CONFIG_CONTENT"):
                env.pop(key, None)
            env.update(OPENCODE_CONFIG_DIR=str(workspace / "config"),
                       OPENCODE_CONFIG=str(workspace / "opencode.json"),
                       OPENCODE_CONFIG_PROJECT_DISABLE="1", OPENCODE_DISABLE_CLAUDE_CODE="1",
                       OPENCODE_DISABLE_CLAUDE_CODE_SKILLS="1")
            argv = command + ["run", "--standalone", "--format", "json", "--agent",
                              "crucible-reviewer", "--model", model]
        else:
            argv = command + ["--print", "--output-format", "json", "--model", model,
                              "--safe-mode", "--setting-sources", "", "--tools", "",
                              "--strict-mcp-config", "--mcp-config", '{"mcpServers":{}}',
                              "--disable-slash-commands", "--no-session-persistence"]
            if spec.get("effort"):
                argv += ["--effort", spec["effort"]]
        request["argv"] = argv
        _write(call_dir / "request.json", request)
        with (call_dir / "stdout.jsonl").open("wb") as stdout, (call_dir / "stderr.log").open("wb") as stderr:
            proc = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=stdout, stderr=stderr,
                                    cwd=workspace, env=env, start_new_session=True)
            try:
                _write(call_dir / "provider-child.json", {**binding, "identity": _identity(proc.pid)})
                proc.communicate(prompt.encode("utf-8"), timeout=timeout)
            except BaseException:
                # Kill the group even if its leader exited while a descendant held stdin.
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                proc.wait()
                raise
        request.update(exit_code=proc.returncode, elapsed_seconds=time.monotonic() - started)
        _write(call_dir / "request.json", request)
        if proc.returncode != 0:
            raise ProviderError(f"{backend} exited with status {proc.returncode}; see raw artifacts")
        raw = (call_dir / "stdout.jsonl").read_text(encoding="utf-8")
        response = (_parse_opencode if backend == "opencode" else _parse_claude)(raw)
        if not isinstance(response["text"], str) or not response["text"].strip() or not response["session_id"]:
            raise ProviderError("Provider returned empty text or missing session ID")
        response.update(requested_model=model, backend=backend)
        _write(call_dir / "response.json", response)
        return response
    except BaseException as error:
        _write(call_dir / "error.json", {"type": type(error).__name__, "message": str(error),
               "exit_code": proc.returncode if proc else None,
               "elapsed_seconds": time.monotonic() - started})
        if isinstance(error, (ProviderError, KeyboardInterrupt, SystemExit)):
            raise
        raise ProviderError(f"{backend} call failed: {error}") from error


def _binding(call_dir):
    invocation = _read(call_dir / "invocation.json")
    try:
        if (not isinstance(invocation, dict) or not isinstance(invocation["token"], str)
                or not invocation["token"] or not isinstance(invocation["spec"], dict) or _hash(call_dir / "prompt.txt") != invocation["prompt_sha256"]
                or not math.isfinite(invocation["timeout"]) or invocation["timeout"] <= 0):
            raise ValueError("invalid invocation")
    except (OSError, KeyError, TypeError, ValueError) as error:
        raise ProviderUncertain("Provider invocation binding is incomplete or changed") from error
    return invocation, {"token": invocation["token"], "invocation_sha256": _hash(call_dir / "invocation.json")}


def recover(call_dir: Path) -> dict:
    """Observe an existing invocation only; uncertainty never starts a replacement."""
    call_dir = call_dir.resolve()
    invocation, binding = _binding(call_dir)
    started = _read(call_dir / "runner-started.json")
    if (not isinstance(started, dict) or any(started.get(k) != v for k, v in binding.items())
            or not isinstance(started.get("identity"), dict)
            or not isinstance(started.get("deadline"), (int, float))
            or not math.isfinite(started["deadline"])):
        raise ProviderUncertain("Provider runner identity or binding is missing")
    # Bound recovery even if an on-disk clock/deadline was changed.
    wait_until = time.monotonic() + min(max(0, started["deadline"] - time.time()), invocation["timeout"] + 10)
    while not (call_dir / "terminal.json").exists():
        identity = started["identity"]
        if not isinstance(identity.get("pid"), int) or not alive(identity):
            # Check again after identity: the actor can commit its receipt just before exit.
            if (call_dir / "terminal.json").exists():
                break
            raise ProviderUncertain("Provider runner is not verifiably live and has no terminal receipt")
        if time.monotonic() >= wait_until:
            raise ProviderUncertain("Provider runner exceeded its recorded receipt deadline")
        time.sleep(0.05)
    terminal = _read(call_dir / "terminal.json")
    if (not isinstance(terminal, dict) or any(terminal.get(k) != v for k, v in binding.items())
            or terminal.get("identity") != started["identity"]
            or terminal.get("status") not in ("success", "error")):
        raise ProviderUncertain("Provider terminal receipt does not match its invocation")
    result_name = "response.json" if terminal["status"] == "success" else "error.json"
    artifacts = ["prompt.txt", "request.json", "stdout.jsonl", "stderr.log", result_name]
    hashes = terminal.get("sha256")
    try:
        if not isinstance(hashes, dict) or set(hashes) != set(artifacts):
            raise ValueError("incomplete receipt")
        if any(_hash(call_dir / name) != hashes[name] for name in artifacts):
            raise ValueError("artifact hash mismatch")
    except (OSError, ValueError) as error:
        raise ProviderUncertain("Provider receipt artifacts are missing or changed") from error
    result = _read(call_dir / result_name)
    if not isinstance(result, dict):
        raise ProviderUncertain("Provider result is not an object")
    if terminal["status"] == "error":
        raise ProviderError(f"Recorded provider failure ({result.get('type')}): {result.get('message')}")
    if (not isinstance(result.get("text"), str) or not result["text"].strip()
            or not result.get("session_id") or result.get("backend") != invocation["spec"].get("backend")
            or result.get("requested_model") != invocation["spec"].get("model")):
        raise ProviderUncertain("Provider success receipt has an invalid response")
    return result


def invoke(spec: dict, prompt: str, call_dir: Path, timeout: float) -> dict:
    """Launch one durable actor, or recover a matching existing call without retrying."""
    call_dir = call_dir.resolve()
    call_dir.mkdir(parents=True, exist_ok=True)
    if any(call_dir.iterdir()):
        invocation, _ = _binding(call_dir)
        if (invocation.get("spec") != spec or invocation["timeout"] != timeout
                or invocation["prompt_sha256"] != hashlib.sha256(prompt.encode("utf-8")).hexdigest()):
            raise ProviderUncertain("Existing call is bound to a different request")
        return recover(call_dir)
    if not isinstance(timeout, (int, float)) or not math.isfinite(timeout) or timeout <= 0:
        raise ProviderError("timeout must be finite and positive")
    try:
        (call_dir / ".invocation-claim").mkdir()
    except FileExistsError:
        return recover(call_dir)
    with (call_dir / "prompt.txt").open("x", encoding="utf-8") as stream:
        stream.write(prompt)
        stream.flush()
        os.fsync(stream.fileno())
    invocation = {"token": uuid.uuid4().hex, "spec": spec, "timeout": timeout,
                  "prompt_sha256": _hash(call_dir / "prompt.txt"), "created_at": time.time()}
    _write(call_dir / "invocation.json", invocation)
    environment = os.environ.copy()
    source = str(Path(__file__).resolve().parent.parent)
    environment["PYTHONPATH"] = source + (os.pathsep + environment["PYTHONPATH"] if environment.get("PYTHONPATH") else "")
    argv = [sys.executable, "-m", "crucible.providers", str(call_dir), invocation["token"],
            _hash(call_dir / "invocation.json")]
    with (call_dir / "runner.log").open("ab") as log:
        try:
            runner = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                                      cwd=call_dir, env=environment, start_new_session=True)
        except OSError as error:
            raise ProviderUncertain("Provider actor could not start; invocation retained") from error
    try:
        runner.wait(timeout=timeout + 10)
    except subprocess.TimeoutExpired:
        raise ProviderUncertain("Provider actor did not commit a receipt by its deadline")
    return recover(call_dir)


def _actor(call_dir, token, invocation_hash):
    invocation, binding = _binding(call_dir)
    if binding != {"token": token, "invocation_sha256": invocation_hash}:
        raise ProviderUncertain("Provider actor invocation changed before startup")
    # A second actor cannot execute the same invocation, even with matching arguments.
    (call_dir / ".runner-claim").mkdir()
    identity = _identity()
    _write(call_dir / "runner-started.json", {**binding, "identity": identity,
           "deadline": time.time() + invocation["timeout"] + 10})
    status = "success"
    try:
        _invoke(invocation["spec"], (call_dir / "prompt.txt").read_text(encoding="utf-8"),
                call_dir, invocation["timeout"], binding)
        if _binding(call_dir)[1] != binding:
            raise ProviderUncertain("Invocation changed during provider execution")
    except BaseException as error:
        status = "error"
        if not (call_dir / "error.json").exists():
            _write(call_dir / "error.json", {"type": type(error).__name__, "message": str(error)})
    result_name = "response.json" if status == "success" else "error.json"
    artifacts = ["prompt.txt", "request.json", "stdout.jsonl", "stderr.log", result_name]
    _write(call_dir / "terminal.json", {**binding, "identity": identity, "status": status,
           "finished_at": time.time(), "sha256": {name: _hash(call_dir / name) for name in artifacts}})
    return 0 if status == "success" else 1


if __name__ == "__main__":
    sys.exit(_actor(Path(sys.argv[1]).resolve(), sys.argv[2], sys.argv[3]))
