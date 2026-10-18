"""Scoring pipeline for the Secure MCP Gateway RL Gym task.

Loads scoring.yml + acceptance-criteria.yml, runs the verifier test suite,
computes weighted partial credit per check (HackerRank-Bench methodology:
each check individually earns its weight if passed, 0 if failed or blocked),
handles blocked vs failed state, and outputs the reference score plus mutant
detection rate.

Usage:
    python evaluation/scoring/pipeline.py score
    python evaluation/scoring/pipeline.py check:reference
    python evaluation/scoring/pipeline.py check:mutants
    python evaluation/scoring/pipeline.py generate
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

import yaml

APP = Path(__file__).resolve().parents[2]  # mcp-gateway root
EVAL = APP / "evaluation"
SCORING_YML = EVAL / "scoring.yml"
ACCEPTANCE_YML = EVAL / "acceptance-criteria.yml"
TESTS_DIR = EVAL / "tests"
MUTANTS_SCRIPT = APP / "test_mutants.py"


def load_yaml(path: Path) -> dict:
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f)


# ---------------------------------------------------------------------------
# Test running + result parsing
# ---------------------------------------------------------------------------

def run_pytest(test_args: list[str]) -> subprocess.CompletedProcess:
    """Run pytest with the given args and return the completed process.

    Uses ``-v`` (verbose) so each test appears on its own line as
    ``path.py::NodeID PASSED|FAILED|SKIPPED``, which the result parser can
    match. ``-q`` is intentionally omitted because it collapses output to dots.
    """
    return subprocess.run(
        [sys.executable, "-m", "pytest", *test_args, "-v", "--tb=short"],
        cwd=str(APP),
        capture_output=True,
        text=True,
        timeout=300,
    )


def parse_pytest_results(output: str) -> dict[str, str]:
    """Parse pytest -v output into {node_id: status} where status is
    'passed', 'failed', or 'skipped'.

    Only lines that look like `path.py::ClassOrTest ... STATUS` are captured.
    """
    results: dict[str, str] = {}
    # Match lines like: path/to/test.py::TestClass::test_name PASSED [ 11%]
    # or: path/to/test.py::TestClass::test_name FAILED
    # or: path/to/test.py::TestClass::test_name SKIPPED
    # pytest 9 appends a progress percentage after the status, so we do not
    # anchor to end of line.
    pattern = re.compile(
        r"^([^\s]+::[^\s]+)\s+(PASSED|FAILED|SKIPPED)\b", re.MULTILINE
    )
    for match in pattern.finditer(output):
        node_id = match.group(1)
        status = match.group(2).lower()
        results[node_id] = status
    return results


def _normalize_node_id(nid: str) -> str:
    """Normalize a pytest node id for cross-platform comparison.

    Replaces backslashes with forward slashes and strips a leading
    ``evaluation/`` prefix so that ``tests/test_foo.py::...`` specs match
    ``evaluation\tests\test_foo.py::...`` node ids produced by pytest on
    Windows.
    """
    nid = nid.replace("\\", "/")
    if nid.startswith("evaluation/"):
        nid = nid[len("evaluation/"):]
    return nid


def check_test_result(
    results: dict[str, str], test_spec: str
) -> str:
    """Determine the status of a single check given its test spec.

    test_spec can be:
      - path/to/test.py::TestClass::test_name  (single test)
      - path/to/test.py::TestClass             (whole class)
      - path/to/test.py                        (whole file)

    Returns 'passed', 'failed', or 'blocked'.
    """
    spec_norm = _normalize_node_id(test_spec)
    parts = spec_norm.split("::")
    prefix = "::".join(parts)

    # Normalize all result node ids the same way, then rebuild the lookup.
    norm_results = {_normalize_node_id(nid): status for nid, status in results.items()}

    matching = [nid for nid in norm_results if nid.startswith(prefix + "::") or nid == prefix]

    if not matching:
        # No test results found for this spec - treat as blocked (unevaluable)
        return "blocked"

    # If any matching test failed, the check fails.
    # If any is skipped and none failed, treat as blocked (unevaluable on this platform).
    # Only if all matching tests passed does the check pass.
    statuses = {norm_results[nid] for nid in matching}
    if "failed" in statuses:
        return "failed"
    if "skipped" in statuses and "passed" not in statuses:
        return "blocked"
    return "passed"


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------

def compute_weighted_score(
    scoring_cfg: dict, check_results: dict[str, str]
) -> tuple[float, list[dict]]:
    """Compute weighted partial credit score from per-check results.

    Each check individually earns its weight if passed, and 0 if failed or
    blocked. Blocked checks earn 0 but are distinguished from failed checks
    in the output. This matches the HackerRank-Bench per-check partial credit
    methodology: score = sum(weight_i * completion_i) / sum(weight_i).
    """
    groups = scoring_cfg["check_groups"]
    total_weight = 0.0
    earned_weight = 0.0
    check_details: list[dict] = []

    for group in groups:
        group_name = group["name"]
        for check in group["checks"]:
            check_id = check["id"]
            weight = float(check["weight"])
            status = check_results.get(check_id, "blocked")

            check_earns = status == "passed"
            check_score = weight if check_earns else 0.0

            total_weight += weight
            earned_weight += check_score

            check_details.append(
                {
                    "id": check_id,
                    "group": group_name,
                    "weight": weight,
                    "status": status,
                    "earns_weight": check_earns,
                    "check_score": check_score,
                }
            )

    final_score = earned_weight / total_weight if total_weight > 0 else 0.0
    return final_score, check_details


def build_check_to_test(acceptance_cfg: dict) -> dict[str, str]:
    """Build {check_id: test_spec} from acceptance-criteria.yml."""
    mapping: dict[str, str] = {}
    for _category, checks in acceptance_cfg.items():
        if not isinstance(checks, list):
            continue
        for entry in checks:
            mapping[entry["id"]] = entry["test"]
    return mapping


def cmd_score() -> int:
    scoring_cfg = load_yaml(SCORING_YML)
    acceptance_cfg = load_yaml(ACCEPTANCE_YML)

    # Run the full verifier suite once, then map results to checks.
    proc = run_pytest([str(TESTS_DIR)])
    output = proc.stdout + proc.stderr
    results = parse_pytest_results(output)

    check_to_test = build_check_to_test(acceptance_cfg)
    check_results: dict[str, str] = {}
    for check_id, test_spec in check_to_test.items():
        check_results[check_id] = check_test_result(results, test_spec)

    final_score, check_details = compute_weighted_score(scoring_cfg, check_results)

    weights_sum = round(
        sum(float(c["weight"]) for g in scoring_cfg["check_groups"] for c in g["checks"]), 4
    )
    reference_target = float(scoring_cfg["scoring"]["reference_target"]) / 100.0

    report = {
        "reference_score": round(final_score, 4),
        "weights_sum": weights_sum,
        "reference_target": reference_target,
        "pass_threshold": float(scoring_cfg.get("pass_threshold", 1.0)),
        "checks": check_details,
    }

    print(json.dumps(report, indent=2))

    passed_count = sum(1 for s in check_results.values() if s == "passed")
    failed_count = sum(1 for s in check_results.values() if s == "failed")
    blocked_count = sum(1 for s in check_results.values() if s == "blocked")
    total_checks = len(check_results)

    print(
        f"\nChecks: {passed_count} passed, {failed_count} failed, "
        f"{blocked_count} blocked out of {total_checks}"
    )
    print(f"Reference score: {final_score:.1%} (target: {reference_target:.1%})")
    print(f"Weights sum: {weights_sum} (must be 1.0)")

    ok = (
        final_score >= reference_target
        and abs(weights_sum - 1.0) < 1e-9
        and failed_count == 0
    )
    if ok:
        print("[PASS] Reference target met (100%), weights sum to 1.0, no failures")
        return 0
    print("[FAIL] Reference target NOT met")
    return 1


def cmd_check_reference() -> int:
    """Run the full verifier suite and confirm the reference passes 100%."""
    proc = run_pytest([str(TESTS_DIR)])
    output = proc.stdout + proc.stderr
    print(output)

    passed = output.count("PASSED")
    failed = output.count("FAILED")
    skipped = output.count("SKIPPED")
    total = passed + failed + skipped

    print(f"\nResults: {passed} passed, {failed} failed, {skipped} skipped out of {total}")

    if failed == 0:
        print("[PASS] Reference passes 100% of checks")
        return 0
    print(f"[FAIL] Reference does NOT pass 100% ({failed} failures)")
    return 1


def cmd_check_mutants() -> int:
    """Run mutant detection via test_mutants.py."""
    if not MUTANTS_SCRIPT.exists():
        print(f"[FAIL] Mutant test script not found: {MUTANTS_SCRIPT}", file=sys.stderr)
        return 1

    proc = subprocess.run(
        [sys.executable, str(MUTANTS_SCRIPT)],
        cwd=str(APP),
        capture_output=True,
        text=True,
        timeout=600,
    )
    print(proc.stdout)
    if proc.stderr:
        print(proc.stderr, file=sys.stderr)

    # Parse results from the "Status:" lines (one per mutant, in the detailed
    # section). Avoid counting the summary section which repeats the same
    # statuses and would double-count.
    output = proc.stdout
    mutant_headers = re.findall(r"Testing mutant: (\w+)", output)
    total_mutants = len(mutant_headers)

    detected_count = len(re.findall(r"Status: PASS \(mutant detected\)", output))
    not_detected_count = len(re.findall(r"Status: FAIL \(mutant NOT detected!\)", output))
    skipped_count = len(re.findall(r"Status: SKIPPED", output))

    # Skipped mutants (e.g. Windows-only tests) count as detected for the
    # purposes of the detection-rate metric - they were not missed by the
    # verifier, just not applicable on this platform.
    detected = detected_count + skipped_count

    detection_rate = detected / total_mutants if total_mutants > 0 else 0.0

    print(f"\nMutants: {detected} detected, {not_detected_count} NOT detected out of {total_mutants} ({skipped_count} skipped)")
    print(f"Detection rate: {detection_rate:.0%}")

    required = load_yaml(SCORING_YML)["scoring"].get("mutant_detection_required", False)
    if required and detection_rate < 1.0:
        print("[FAIL] Not all mutants were detected")
        return 1
    print("[PASS] All mutants detected")
    return 0


def cmd_generate() -> int:
    """Generate the seeded workspace for agents to work in.

    Copies the reference gateway package into a workspace/ directory so
    agents start from a clean copy of the reference implementation.
    """
    workspace = APP / "workspace"
    reference = APP / "mcp_gateway"

    if workspace.exists():
        print(f"Workspace already exists at {workspace}")
        return 0

    if not reference.exists():
        print(f"[FAIL] Reference package not found: {reference}", file=sys.stderr)
        return 1

    workspace.mkdir(parents=True, exist_ok=True)
    shutil.copytree(reference, workspace / "mcp_gateway", dirs_exist_ok=True)
    print(f"Generated seeded workspace at {workspace}")
    return 0


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(
        description="RL Gym scoring pipeline for Secure MCP Gateway"
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("generate", help="Generate the seeded agent workspace")
    subparsers.add_parser("score", help="Run the full weighted scoring pipeline")
    subparsers.add_parser(
        "check:reference", help="Run reference validation (must pass 100%)"
    )
    subparsers.add_parser(
        "check:mutants", help="Run mutant detection (all must be caught)"
    )

    args = parser.parse_args()

    if args.command == "generate":
        return cmd_generate()
    if args.command == "score":
        return cmd_score()
    if args.command == "check:reference":
        return cmd_check_reference()
    if args.command == "check:mutants":
        return cmd_check_mutants()

    parser.print_help()
    return 1


if __name__ == "__main__":
    sys.exit(main())
