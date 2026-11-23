"""Regenerate the feedback template's TOML compatibility fixture."""
from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

try:
    import tomllib as toml
except ModuleNotFoundError:
    try:
        import tomli as toml
    except ModuleNotFoundError:
        print("no TOML parser available")
        raise SystemExit(1)

root = Path(__file__).resolve().parents[2]
template = root / "docs/templates/feedback-rules.example.toml"
data = template.read_bytes()
golden = {
    "_meta": {
        "template_sha256": hashlib.sha256(data).hexdigest(),
        "generated_with": f"{toml.__name__} {sys.version.split()[0]}",
    },
    "data": toml.loads(data.decode("utf-8")),
}
destination = root / "tests/data/feedback_rules_example.golden.json"
destination.parent.mkdir(parents=True, exist_ok=True)
destination.write_text(json.dumps(golden, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
print(f"generated {destination}")
