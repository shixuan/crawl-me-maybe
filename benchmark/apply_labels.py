"""Apply human-reviewed label overrides to the eval set.

Usage: python3 benchmark/apply_labels.py <patch_json>

The patch is a JSON list of [index, relevant] pairs.  After applying,
entries carry "labeler": "gold-review" and "review" is dropped.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

EVAL = Path("benchmark/data/embedding_eval.json")


def main() -> None:
    patch_path = Path(sys.argv[1])
    patch: list[list[int]] = json.loads(patch_path.read_text())

    data = json.loads(EVAL.read_text())
    entries = data["goal"]["candidates"]
    for idx, new_rel in patch:
        assert 0 <= idx < len(entries), f"index {idx} out of range"
        old = entries[idx]["relevant"]
        if old != new_rel:
            print(f"{idx:03d}: {int(old)} -> {int(new_rel)}  {entries[idx]['anchor'][:60]}")
        entries[idx]["relevant"] = bool(new_rel)
        entries[idx]["labeler"] = "gold-review"
        entries[idx].pop("review", None)

    n_rel = sum(1 for e in entries if e["relevant"])
    EVAL.write_text(json.dumps(data, indent=2, ensure_ascii=False))
    print(f"\napplied {len(patch)} overrides -> {EVAL} ({n_rel} relevant / {len(entries) - n_rel} irrelevant)")


if __name__ == "__main__":
    main()
