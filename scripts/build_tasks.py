"""Build the company and the frozen task set.

uv run python scripts/build_tasks.py            # data/generated/company.db + data/tasks.json
"""

from __future__ import annotations

import json
from collections import Counter

from opsagent.config import DATA_DIR, GENERATED
from opsagent.sim.company import Company
from opsagent.tasks.generate import generate


def main() -> int:
    company = Company.create(GENERATED / "company.db")
    tasks = generate(company)
    out = DATA_DIR / "tasks.json"
    out.write_text(json.dumps([t.to_json() for t in tasks], indent=1), encoding="utf-8")
    cats = Counter(t.category for t in tasks)
    verdicts = Counter(t.expected.verdict for t in tasks)
    splits = Counter(t.split for t in tasks)
    print(f"{len(tasks)} tasks -> {out}")
    print("by category:", dict(cats))
    print("by expected verdict:", dict(verdicts))
    print("split:", dict(splits))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
