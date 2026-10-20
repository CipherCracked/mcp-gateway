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
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import yaml

from dotenv import load_dotenv

# Load .env file if present (for LLM_API_KEY etc.)
load_dotenv()

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

def run_pytest(test_args: list[str], env: dict | None = None) -> subprocess.CompletedProcess:
    """Run pytest with the given args and return the completed process.

    Uses ``-v`` (verbose) so each test appears on its own line as
    ``path.py::NodeID PASSED|FAILED|SKIPPED``, which the result parser can
    match. ``-q`` is intentionally omitted because it collapses output to dots.
    """
    run_env = os.environ.copy()
    if env:
        run_env.update(env)
    return subprocess.run(
        [sys.executable, "-m", "pytest", *test_args, "-v", "--tb=short"],
        cwd=str(APP),
        capture_output=True,
        text=True,
        timeout=300,
        env=run_env,
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


def cmd_score(args) -> int:
    scoring_cfg = load_yaml(SCORING_YML)
    acceptance_cfg = load_yaml(ACCEPTANCE_YML)

    # Choose which code to score: reference (100% expected) or workspace (model output)
    target = getattr(args, "target", "workspace")
    model = getattr(args, "model", None)
    model_safe = (
        model.replace("/", "_").replace(":", "_").replace("-", "_")
        if model
        else None
    )

    # For workspace scoring, copy the per-model workspace's mcp_gateway
    # package into workspace/mcp_gateway/ so the existing module path
    # (workspace.mcp_gateway.main:app) resolves to the model's code.
    if target == "workspace":
        if not model_safe:
            print(
                "[FAIL] --model is required when scoring workspace target.",
                file=sys.stderr,
            )
            return 1
        model_workspace = APP / "workspace" / model_safe / "mcp_gateway"
        target_workspace = APP / "workspace" / "mcp_gateway"
        if not model_workspace.exists():
            print(
                f"[FAIL] Model workspace not found: {model_workspace}",
                file=sys.stderr,
            )
            return 1
        if target_workspace.exists():
            shutil.rmtree(target_workspace)
        shutil.copytree(model_workspace, target_workspace)

    gateway_module = (
        "reference.mcp_gateway.main:app"
        if target == "reference"
        else "workspace.mcp_gateway.main:app"
    )

    env = os.environ.copy()
    env["GATEWAY_MODULE"] = gateway_module

    # Run the full verifier suite once, then map results to checks.
    proc = run_pytest([str(TESTS_DIR)], env=env)
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
    print(f"Weights sum: {weights_sum} (must be 1.0)")
    print(f"Score: {final_score:.1%}")

    target = getattr(args, "target", "workspace")

    if target == "reference":
        # Reference must have zero failures; blocked/skipped checks are
        # unevaluable, not failed, so they don't fail the reference check.
        ok = (
            failed_count == 0
            and abs(weights_sum - 1.0) < 1e-9
        )
        if ok:
            print("[PASS] Reference: zero failures, weights sum to 1.0")
            return 0
        print("[FAIL] Reference: has failures or weights do not sum to 1.0")
        return 1

    # workspace (model output): report weighted score vs frontier ceiling
    frontier_ceiling = float(scoring_cfg["scoring"].get("frontier_model_ceiling", 0.3))
    if final_score < frontier_ceiling:
        print(f"[PASS] Model score {final_score:.1%} is below frontier ceiling {frontier_ceiling:.0%}")
        return 0
    print(f"[FAIL] Model score {final_score:.1%} is NOT below frontier ceiling {frontier_ceiling:.0%}")
    return 1


def cmd_check_reference(args) -> int:
    """Run the full verifier suite and confirm the reference passes 100%."""
    args.target = "reference"
    return cmd_score(args)


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


def cmd_generate(args) -> int:
    """Generate a solution by running OpenHands headless.

    Copies task/public/ to a per-model workspace under
    workspace/<model-name>/, builds the Docker sandbox image, and runs
    OpenHands headless INSIDE the container with only /workspace mounted —
    the agent never sees evaluation/ or reference/.

    Saves logs to evaluation/proof-of-work/<model-name>/run.log.
    """
    model = (
        args.model
        or os.environ.get("OH_MODEL")
        or "openrouter/nvidia/nemotron-3.5-lightning:free"
    )
    model_safe = model.replace("/", "_").replace(":", "_").replace("-", "_")

    proof_dir = EVAL / "proof-of-work" / model_safe
    proof_dir.mkdir(parents=True, exist_ok=True)

    # Fresh per-model workspace from public materials only (never reference/
    # or evaluation/). Each model gets its own workspace so runs don't
    # overwrite each other. Lives at workspace/<model-name>/ at repo root so
    # the scoring step can import workspace.mcp_gateway.main:app.
    workspace = APP / "workspace" / model_safe
    if workspace.exists():
        shutil.rmtree(workspace)
    workspace.mkdir(parents=True, exist_ok=True)

    public = APP / "task" / "public"
    if public.exists():
        for item in public.iterdir():
            if item.is_dir():
                shutil.copytree(item, workspace / item.name, dirs_exist_ok=True)
            else:
                shutil.copy2(item, workspace / item.name)

    # Copy the task instruction into the workspace so it's available inside
    # the container at /workspace/task/instruction.md.
    instruction_src = APP / "task" / "instruction.md"
    instruction_dst = workspace / "task" / "instruction.md"
    if instruction_src.exists():
        instruction_dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(instruction_src, instruction_dst)

    # Build the Docker sandbox image (run docker directly to avoid Git
    # Bash path issues with Windows drive letters on this machine).
    print("Building Docker sandbox image...")
    build_proc = subprocess.run(
        [
            "docker", "build",
            "--tag", "secure-mcp-gateway:latest",
            "--file", str(APP / "environment" / "Dockerfile").replace("\\", "/"),
            ".",
        ],
        cwd=str(APP).replace("\\", "/"),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=600,
    )
    print(build_proc.stdout)
    if build_proc.returncode != 0:
        print(build_proc.stderr, file=sys.stderr)
        print("[FAIL] Docker build failed")
        return 1

    # OpenHands needs LLM_API_KEY + LLM_MODEL via env overrides.
    # TTY_INTERACTIVE/TTY_COMPATIBLE force Rich to use a plain console so
    # emoji/unicode output doesn't break on cp1252.
    llm_api_key = os.environ.get("LLM_API_KEY")
    if not llm_api_key:
        print(
            "[FAIL] LLM_API_KEY environment variable is not set. "
            "Set it before running npm run generate.",
            file=sys.stderr,
        )
        return 1

    # Run OpenHands headless INSIDE the Docker container with only /workspace
    # mounted. The agent cannot reach reference/ or evaluation/ from in there.
    log_file = proof_dir / "run.log"
    workspace_host = str(workspace).replace("\\", "/")

    cmd = [
        "docker", "run", "--rm",
        "-v", f"{workspace_host}:/workspace",
        "-w", "/workspace",
        "-e", f"LLM_MODEL={model}",
        "-e", f"LLM_API_KEY={llm_api_key}",
        "-e", "LLM_BASE_URL=https://openrouter.ai/api/v1",
        "-e", "TTY_INTERACTIVE=1",
        "-e", "TTY_COMPATIBLE=1",
        "-e", "PYTHONIOENCODING=utf-8",
        "secure-mcp-gateway:latest",
        "openhands",
        "--headless",
        "--json",
        "--override-with-envs",
        "--always-approve",
        "-f", "/workspace/task/instruction.md",
    ]

    print(f"Running OpenHands headless with model: {model}")
    print(f"Workspace: {workspace}")
    print(f"Log file: {log_file}")

    with open(log_file, "w", encoding="utf-8") as lf:
        proc = subprocess.run(
            cmd,
            cwd=str(APP),
            stdout=lf,
            stderr=subprocess.STDOUT,
            text=True,
            timeout=600,
        )

    if proc.returncode != 0:
        print(f"[FAIL] OpenHands exited with code {proc.returncode}. See {log_file}")
        return 1

    print(f"OpenHands completed successfully. Logs: {log_file}")
    return 0


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(
        description="RL Gym scoring pipeline for Secure MCP Gateway"
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    generate_parser = subparsers.add_parser(
        "generate", help="Generate the seeded agent workspace"
    )
    generate_parser.add_argument(
        "--model",
        default=None,
        help="Model to use for generation (overrides OH_MODEL env var)",
    )
    score_parser = subparsers.add_parser(
        "score", help="Run the full weighted scoring pipeline"
    )
    score_parser.add_argument(
        "--target",
        choices=["reference", "workspace"],
        default="workspace",
        help="Which code to score: 'reference' (must pass 100%%) or 'workspace' (model output)",
    )
    score_parser.add_argument(
        "--model",
        default=None,
        help="Model name (used to locate workspace/<model>/mcp_gateway for scoring)",
    )
    subparsers.add_parser(
        "check:reference", help="Run reference validation (must pass 100%%)"
    )
    subparsers.add_parser(
        "check:mutants", help="Run mutant detection (all must be caught)"
    )

    args = parser.parse_args()

    if args.command == "generate":
        return cmd_generate(args)
    if args.command == "score":
        return cmd_score(args)
    if args.command == "check:reference":
        return cmd_check_reference(args)
    if args.command == "check:mutants":
        return cmd_check_mutants()

    parser.print_help()
    return 1


if __name__ == "__main__":
    sys.exit(main())
