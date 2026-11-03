"""Example policy for a synthetic scalar objective, not a statistical test."""

import json
import sys
from pathlib import Path

payload = json.load(sys.stdin)
attempt = payload["attempt"]
metrics = json.loads(Path(attempt["artifacts"]["metrics.json"]).read_text())
loss, x, seed = metrics["loss"], metrics["x"], metrics["seed"]
output = {"event": "normal", "summary": "Synthetic objective evaluated; lower is better", "metrics": {"loss": loss}, "comparison_key": f"synthetic-quadratic-v1-seed-{seed}"}
if loss < 0.001:
    output["next"] = {"action": "stop", "reason": "Reached the predeclared synthetic objective target"}
elif "--review" in sys.argv:
    repeated = any(old["proposal"]["parameters"] == attempt["proposal"]["parameters"] and old["evaluation"].get("comparison_key") == output["comparison_key"] and old["evaluation"].get("metrics", {}).get("loss") == loss for old in payload["history"])
    if repeated:
        output["event"] = "stagnation"
        output["summary"] = "The registered repeat has identical objective; choose a discriminating x within bounds"
    elif not payload["history"]:
        output["next"] = {"action": "run", "experiment": "objective", "parameters": {"x": x, "seed": seed}, "reason": "Registered same-seed repeat to exercise the stagnation branch"}
    else:
        output["next"] = {"action": "run", "experiment": "objective", "parameters": {"x": min(x + 1, 3), "seed": seed}, "reason": "Continue the registered sweep after a changed point"}
else:
    output["next"] = {"action": "run", "experiment": "objective", "parameters": {"x": min(x + 1, 3), "seed": seed}, "reason": "Next point on the registered synthetic sweep"}
print(json.dumps(output, allow_nan=False))
