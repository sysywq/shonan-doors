"""Export per-run publication-gate outcomes without external API calls.

This reads the final daily run report, not inferred article status. Store the
result persistently before the Actions runner is destroyed to enable historical
held/rejected cost attribution.
"""
import argparse
import json
import os
from pathlib import Path


def extract(report, run_id):
    day = report.get("date")
    accepted = [{"article_id": x, "outcome": "confirmed_for_publication"}
                for x in report.get("accepted_ids", [])]
    held = [{"article_id": h.get("id"), "title": h.get("title"),
             "outcome": "held", "verdict": h.get("verdict"),
             "reasons": h.get("reasons", [])}
            for h in report.get("held", []) if isinstance(h, dict)]
    rejected = [{"article_id": None, "title": h.get("title"),
                 "outcome": "gate_rejected", "reasons": h.get("reasons", [])}
                for h in report.get("gate_rejected", []) if isinstance(h, dict)]
    freshness = [{"article_id": None, "title": h.get("title"),
                  "outcome": "freshness_rejected"}
                 for h in report.get("freshness_rejected", []) if isinstance(h, dict)]
    return {"run_id": str(run_id), "date_jst": day, "outcomes": accepted + held + rejected + freshness,
            "status_note": "confirmed_for_publication means passed gate, not verified HTTP 200"}


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--report", type=Path, required=True)
    p.add_argument("--output", type=Path, required=True)
    p.add_argument("--run-id", default=os.environ.get("GITHUB_RUN_ID", ""))
    args = p.parse_args()
    source = json.loads(args.report.read_text(encoding="utf-8"))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(extract(source, args.run_id), ensure_ascii=False, indent=2) + "\n",
                           encoding="utf-8")


if __name__ == "__main__":
    main()
