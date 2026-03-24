#!/usr/bin/env python3
"""
Test script: register the feature_to_pr workflow and trigger a run.

Usage:
    PYTHONPATH=. python scripts/test_feature_to_pr.py \
        --feature "Add rate limiting to the /search endpoint" \
        --owner shubhamsWEB \
        --repo nexusCode_server \
        --base main \
        --pr-title "feat: add rate limiting to search endpoint"

The script:
  1. Reads the feature_to_pr.yaml template
  2. Registers (or updates) the workflow via POST /workflows
  3. Triggers a run via POST /workflows/{id}/run
  4. Polls for completion and prints the result including the PR URL
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

import httpx

TEMPLATE_PATH = Path(__file__).parent.parent / "src/workflows/templates/feature_to_pr.yaml"


def main():
    parser = argparse.ArgumentParser(description="Register and trigger the feature_to_pr workflow")
    parser.add_argument("--feature", required=True, help="Feature request description")
    parser.add_argument("--owner", required=True, help="GitHub repo owner")
    parser.add_argument("--repo", required=True, help="GitHub repo name")
    parser.add_argument("--base", default="main", help="Base branch (default: main)")
    parser.add_argument("--pr-title", default="", help="PR title (auto-generated if omitted)")
    parser.add_argument("--target-dir", default="", help="Target directory hint")
    parser.add_argument("--reviewers", default="", help="Comma-separated reviewer usernames")
    parser.add_argument("--api", default="http://localhost:8000", help="API base URL (default: http://localhost:8000)")
    args = parser.parse_args()

    base_url = args.api.rstrip("/")

    def _api(method: str, path: str, **kwargs) -> dict:  # noqa: F811
        url = f"{base_url}{path}"
        resp = httpx.request(method, url, timeout=30, **kwargs)
        if not resp.is_success:
            print(f"  ERROR {resp.status_code}: {resp.text[:300]}")
            sys.exit(1)
        return resp.json()

    yaml_definition = TEMPLATE_PATH.read_text()

    # ── 1. Register the workflow ────────────────────────────────────────────────
    print("\n1. Registering workflow 'feature_to_pr'...")
    result = _api("POST", "/workflows", json={
        "name": "feature_to_pr",
        "yaml_definition": yaml_definition,
        "description": "Feature request → GitHub PR pipeline",
    })
    workflow_id = result["id"]
    print(f"   OK  workflow_id={workflow_id}  (status: {result.get('status', 'active')})")

    # ── 2. Trigger a run ────────────────────────────────────────────────────────
    print(f"\n2. Triggering run for: '{args.feature}'")
    payload = {
        "feature_request": args.feature,
        "repo_owner": args.owner,
        "repo_name": args.repo,
        "base_branch": args.base,
        "pr_title": args.pr_title or f"feat: {args.feature[:60]}",
        "target_dir": args.target_dir,
        "reviewers": args.reviewers,
    }
    run_result = _api("POST", f"/workflows/{workflow_id}/run", json={"payload": payload})
    run_id = run_result["run_id"]
    print(f"   OK  run_id={run_id}")
    print(f"       Stream: {base_url}/workflows/runs/{run_id}/stream")

    # ── 3. Poll for completion ──────────────────────────────────────────────────
    print("\n3. Polling for completion (Ctrl-C to stop)...")
    last_step = ""
    poll_start = time.monotonic()

    while True:
        time.sleep(8)
        run = _api("GET", f"/workflows/runs/{run_id}")
        status = run.get("status", "unknown")
        elapsed = int(time.monotonic() - poll_start)

        # Print step progress
        steps = run.get("steps", [])
        for s in steps:
            key = f"{s.get('step_id')}:{s.get('status')}"
            if key != last_step:
                last_step = key
                tokens = s.get("tokens_used") or 0
                tok_str = f" ({tokens:,} tokens)" if tokens else ""
                print(f"   [{elapsed:>4}s] {s.get('status','?'):12} {s.get('step_id','?')}{tok_str}")

        if status in ("completed", "failed"):
            break

        if elapsed > 1800:  # 30-minute safety cap
            print("   TIMEOUT — run is taking too long, check the stream endpoint")
            break

    # ── 4. Print final results ──────────────────────────────────────────────────
    run = _api("GET", f"/workflows/runs/{run_id}/trace")
    status = run.get("status", "unknown")
    graph_state = run.get("graph_state") or {}

    print(f"\n4. Run finished  status={status}")
    print("=" * 60)

    pr_url = graph_state.get("github_pr_url", "")
    deployment_plan = graph_state.get("deployment_plan", "")

    # Extract PR_URL from deployment_plan text if not in typed field
    if not pr_url and deployment_plan:
        for line in deployment_plan.splitlines():
            if line.startswith("PR_URL:"):
                pr_url = line.split(":", 1)[1].strip()
                break

    if pr_url:
        print(f"\n  GitHub PR: {pr_url}")
    else:
        print("\n  No PR URL found in state (check deployment_plan below)")

    review = graph_state.get("review_verdict", "")
    if review:
        verdict_line = review.splitlines()[0] if review else "—"
        print(f"  Review verdict: {verdict_line}")

    tokens = run.get("total_tokens_used", 0)
    print(f"  Total tokens: {tokens:,}")

    if status == "failed":
        err = run.get("error_message", "")
        steps = run.get("steps", [])
        failed_steps = [s for s in steps if s.get("status") == "failed"]
        print(f"\n  Error: {err}")
        for s in failed_steps:
            print(f"  Failed step [{s['step_id']}]: {s.get('error_message', '')}")
        sys.exit(1)

    if deployment_plan:
        print(f"\n  Deployment plan (first 600 chars):\n  {deployment_plan[:600]}")

    print("\nDone.")


if __name__ == "__main__":
    main()
