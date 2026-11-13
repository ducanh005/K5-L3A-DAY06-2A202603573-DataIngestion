"""Runnable CP5 verification script: run A/B/C dev+test evaluation, update experiment_log, and check traces."""
from __future__ import annotations

import csv
import json
import sys
from pathlib import Path

STARTER_DIR = Path(__file__).resolve().parents[1]
REPO_ROOT = STARTER_DIR.parent
if str(STARTER_DIR) not in sys.path:
    sys.path.insert(0, str(STARTER_DIR))

from src.run import run_pipeline  # noqa: E402


def main() -> None:
    summary = run_pipeline(reuse_existing_index=True)

    results_dir = REPO_ROOT / "results"
    expected_files = [
        results_dir / "traces_A.json",
        results_dir / "traces_B.json",
        results_dir / "traces_C.json",
        results_dir / "cp5_traces.json",
        results_dir / "cp5_eval_summary.json",
        results_dir / "manual_audit_worksheet.json",
        results_dir / "manual_audit_worksheet.md",
    ]
    for path in expected_files:
        if not path.exists():
            raise RuntimeError(f"Missing expected CP5 artifact: {path}")

    traces = json.loads((results_dir / "cp5_traces.json").read_text(encoding="utf-8"))
    if len(traces) != 66:
        raise RuntimeError(f"Expected 66 traces (22 questions x 3 strategies), got {len(traces)}.")

    worksheet = json.loads((results_dir / "manual_audit_worksheet.json").read_text(encoding="utf-8"))
    if len(worksheet["coherence_positions"]) != 10:
        raise RuntimeError(f"Expected 10 coherence positions, got {len(worksheet['coherence_positions'])}.")
    if len(worksheet["test_grounding_and_metadata"]) != 5:
        raise RuntimeError(f"Expected 5 test questions in manual audit worksheet, got {len(worksheet['test_grounding_and_metadata'])}.")

    with (REPO_ROOT / "artifacts" / "experiment_log.csv").open("r", encoding="utf-8", newline="") as f:
        csv_rows = list(csv.DictReader(f))
    for row in csv_rows:
        for col in ("dev_hit1", "dev_hit3", "test_hit1", "test_hit3"):
            if not row.get(col):
                raise RuntimeError(f"experiment_log.csv missing '{col}' for strategy {row.get('strategy')}.")

    env = summary["environment"]
    print(
        f"Env: Python {env['python']} | google-genai {env['google_genai']} | "
        f"psycopg {env['psycopg']} | PostgreSQL {env['postgres_server']} | pgvector {env['pgvector']}"
    )
    print(
        f"Query embed stats: calls={summary['embedding']['query_eval_stats']['api_calls']}, "
        f"elapsed_s={round(summary['embedding']['query_eval_stats']['elapsed_seconds'], 3)}"
    )
    for s in ("A", "B", "C"):
        m = summary["metrics"][s]
        print(
            f"Strategy {s} | dev Hit@1={m['dev']['hit1_fraction']} ({m['dev']['hit1_rate']:.3f}), "
            f"Hit@3={m['dev']['hit3_fraction']} ({m['dev']['hit3_rate']:.3f}), "
            f"split_ev={m['dev']['split_evidence_count']}, filter={m['dev']['filter_compliance_fraction']} | "
            f"test Hit@1={m['test']['hit1_fraction']} ({m['test']['hit1_rate']:.3f}), "
            f"Hit@3={m['test']['hit3_fraction']} ({m['test']['hit3_rate']:.3f}), "
            f"split_ev={m['test']['split_evidence_count']}, filter={m['test']['filter_compliance_fraction']}"
        )
    print("CP5 artifacts & manual audit worksheet verified successfully.")


if __name__ == "__main__":
    main()
