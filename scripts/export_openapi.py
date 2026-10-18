"""Save the app's OpenAPI spec to openapi/openapi.json.

FastAPI builds the spec from the code (routes, models, responses), so re-run
this after changing an endpoint to keep the saved file up to date.

Run from the project folder:
    python scripts/export_openapi.py
"""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.main import app  # noqa: E402  (import after fixing the path)

out = ROOT / "openapi" / "openapi.json"
out.parent.mkdir(exist_ok=True)
out.write_text(json.dumps(app.openapi(), indent=2) + "\n", encoding="utf-8")
print(f"Wrote {out.relative_to(ROOT)}")
