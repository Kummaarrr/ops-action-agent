"""Copy what the demo shows into app/samples/ (no model calls).

uv run python scripts/build_demo_samples.py evals/results/v1_haiku_test_r1.json
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from opsagent.config import DATA_DIR, PROJECT_ROOT

SAMPLES = PROJECT_ROOT / "app" / "samples"


def main(result_file: str) -> int:
    SAMPLES.mkdir(parents=True, exist_ok=True)
    data = json.loads(Path(result_file).read_text(encoding="utf-8"))
    for s in data["tasks"]:  # keep the demo file small
        s["trace"] = [
            {k: (str(v)[:600] if k == "result" else v) for k, v in step.items()}
            for step in s.get("trace", [])
        ]
    (SAMPLES / "results.json").write_text(json.dumps(data, indent=1), encoding="utf-8")
    tasks = json.loads((DATA_DIR / "tasks.json").read_text(encoding="utf-8"))
    keep = {s["id"] for s in data["tasks"]}
    slim = [
        {
            k: t[k]
            for k in (
                "id",
                "template",
                "category",
                "tier",
                "subject",
                "body",
                "requester_email",
                "channel",
                "expected",
            )
        }
        for t in tasks
        if t["id"] in keep
    ]
    (SAMPLES / "tasks.json").write_text(json.dumps(slim, indent=1), encoding="utf-8")
    print(f"{len(data['tasks'])} tasks -> {SAMPLES}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1] if len(sys.argv) > 1 else "evals/results/v1_haiku_test_r1.json"))
