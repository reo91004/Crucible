"""Synthetic deterministic-seed objective; this is a controller example."""

import argparse
import json
import random
from pathlib import Path

parser = argparse.ArgumentParser()
parser.add_argument("--x", type=float, required=True)
parser.add_argument("--seed", type=int, required=True)
args = parser.parse_args()
loss = (args.x - 3) ** 2 + random.Random(args.seed).random() * 0.001
Path("metrics.json").write_text(json.dumps({"origin": "synthetic", "loss": loss, "x": args.x, "seed": args.seed, "units": "squared_distance"}, allow_nan=False) + "\n")
