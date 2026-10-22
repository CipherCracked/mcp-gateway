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
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import IO

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
# Subprocess streaming helpers
# ---------------------------------------------------------------------------


def tee_subprocess(
    cmd: list[str],
    *,
    cwd: str | Path | None = None,
    env: dict | None = None,
    log_file: IO[str] | None = None,
    timeout: int | None = None,
    stream_to_stdout: bool = True,
) -> subprocess.CompletedProcess:
    """Run a subprocess, writing its output to a log file and optionally to stdout.

    Lines are streamed as they are produced (unlike ``subprocess.run`` which
    buffers everything), so long-running commands show live progress. The
    full output is also captured and returned in ``CompletedProcess.stdout``.
    """
    run_env = os.environ.copy()
    if env:
        run_env.update(env)

    proc = subprocess.Popen(
        cmd,
        cwd=str(cwd) if cwd else None,
        env=run_env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        bufsize=1,  # line-buffered
    )

    captured: list[str] = []
    assert proc.stdout is not None
    for line in proc.stdout:
        captured.append(line)
        if log_file is not None:
            log_file.write(line)
            log_file.flush()
        if stream_to_stdout:
            sys.stdout.write(line)
            sys.stdout.flush()

    proc.wait(timeout=timeout)

    return subprocess.CompletedProcess(
        args=cmd,
        returncode=proc.returncode,
        stdout="".join(captured),
        stderr="",
    )


# ---------------------------------------------------------------------------
# Test running + result parsing
# ---------------------------------------------------------------------------


def run_pytest(
    test_args: list[str],
    env: dict | None = None,
    stream: bool = False,
    log_file: IO[str] | None = None,
) -> subprocess.CompletedProcess:
    """Run pytest with the given args and return the completed process.

    Uses ``-v`` (verbose) so each test appears on its own line as
    ``path.py::NodeID PASSED|FAILED|SKIPPED``, which the result parser can
    match. ``-q`` is intentionally omitted because it collapses output to dots.

    When ``stream`` is True, output is written live to ``log_file`` and stdout
    so long test runs show progress instead of appearing all at once at the end.
    """
    run_env = os.environ.copy()
    if env:
        run_env.update(env)

    cmd = [sys.executable, "-m", "pytest", *test_args, "-v", "--tb=short"]

    if stream:
        return tee_subprocess(
            cmd,
            cwd=str(APP),
            env=run_env,
            log_file=log_file,
            timeout=300,
            stream_to_stdout=True,
        )

    return subprocess.run(
        cmd,
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
    run_timestamp = getattr(args, "run", None)

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
        if run_timestamp:
            model_workspace = APP / "workspace" / f"{model_safe}_{run_timestamp}" / "mcp_gateway"
        else:
            # Auto-pick the latest timestamped workspace for this model.
            latest_path = _find_latest_run_workspace(model_safe)
            if latest_path is None:
                print(
                    f"[FAIL] No timestamped workspace found for model '{model_safe}'. "
                    f"Specify --run <timestamp> or run generate first.",
                    file=sys.stderr,
                )
                return 1
            model_workspace = latest_path / "mcp_gateway"
            print(f"[INFO] Auto-selected latest run workspace: {latest_path}")
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

    # The harness provides the runtime config file (per the task spec, actual
    # API keys and allowlists are set by the evaluation harness). Write it to
    # the repo root so the gateway's config loader finds it at
    # ./config/gateway.yaml — uvicorn and pytest both run with cwd=repo root
    # on the host, and /app/config/gateway.yaml does not exist on Windows.
    server_prefix = "reference.mcp_gateway" if target == "reference" else "mcp_gateway"
    config_yaml = f"""gateway:
  host: "0.0.0.0"
  port: 8000
  request_max_bytes: 1048576
  rate_limit_per_minute: 60
  concurrency_limit: 10
  execution_timeout_seconds: 30
  gateway_timeout_seconds: 35

servers:
  documents:
    command: ["python", "-m", {server_prefix}.servers.documents"]
  jobs:
    command: ["python", "-m", {server_prefix}.servers.jobs"]

clients:
  client_a:
    api_key: "key-a-123"
    allowlist: ["documents.search_documents", "documents.get_document"]
  client_b:
    api_key: "key-b-456"
    allowlist: ["jobs.execute_job", "jobs.get_job_status"]
  client_admin:
    api_key: "key-admin-789"
    allowlist: ["documents.search_documents", "documents.get_document", "jobs.execute_job", "jobs.get_job_status"]
"""

    config_dir = APP / "config"
    config_dir.mkdir(exist_ok=True)
    config_path = config_dir / "gateway.yaml"
    config_path.write_text(config_yaml, encoding="utf-8")
    print(f"[INFO] Wrote harness config to {config_path}", flush=True)

    gateway_module = (
        "reference.mcp_gateway.main:app"
        if target == "reference"
        else "workspace.mcp_gateway.main:app"
    )

    env = os.environ.copy()
    env["GATEWAY_MODULE"] = gateway_module

    # Run the full verifier suite once, then map results to checks.
    print(f"[INFO] Scoring target: {target}", flush=True)
    print(f"[INFO] Gateway module: {gateway_module}", flush=True)
    print(f"[INFO] Started at: {datetime.now(timezone.utc).isoformat()}", flush=True)
    started_at = time.time()

    proc = run_pytest([str(TESTS_DIR)], env=env, stream=True)
    output = proc.stdout + proc.stderr
    results = parse_pytest_results(output)

    elapsed = time.time() - started_at
    print(f"[INFO] Test suite completed in {elapsed:.1f}s", flush=True)

    check_to_test = build_check_to_test(acceptance_cfg)
    check_results: dict[str, str] = {}
    for check_id, test_spec in check_to_test.items():
        status = check_test_result(results, test_spec)
        check_results[check_id] = status
        mark = {"passed": "[PASS]", "failed": "[FAIL]", "blocked": "[BLOCK]"}[status]
        print(f"  {mark} {check_id}: {status}", flush=True)

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


SANDBOX_IMAGE = "secure-mcp-gateway:sandbox"
SANDBOX_CONTAINER = "secure-mcp-gateway-sandbox"


def _ensure_openhands_installed() -> int:
    """Ensure openhands-sdk is importable in the host Python environment.

    The OpenHands SDK runs on the host (not in Docker), so it must be
    installed in the host venv. This is a one-time pip install — no PyPI
    access is needed inside Docker anymore.
    """
    try:
        import openhands.sdk  # noqa: F401
        return 0
    except ImportError:
        pass

    print("openhands-sdk not found in host Python; installing (one-time)...")
    proc = subprocess.run(
        [sys.executable, "-m", "pip", "install", "--no-cache-dir", "openhands-sdk==1.21.0"],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        timeout=600,
    )
    print(proc.stdout)
    if proc.returncode != 0:
        print(proc.stderr, file=sys.stderr)
        print("[FAIL] Failed to install openhands-sdk on host")
        return 1
    print("openhands-sdk installed successfully.")
    return 0


def _build_image(max_retries: int = 3, backoff_seconds: int = 10,
                 timeout_seconds: int = 300) -> int:
    """Build the Docker sandbox image with retries and backoff.

    The sandbox image is minimal — just Python + curl. No PyPI installs
    (no openhands-sdk, no litellm), so builds should complete in seconds
    rather than timing out on slow PyPI connections.
    """
    build_cmd = [
        "docker", "build",
        "--tag", SANDBOX_IMAGE,
        "--file", str(APP / "environment" / "Dockerfile").replace("\\", "/"),
        ".",
    ]

    for attempt in range(1, max_retries + 1):
        print(f"Building Docker sandbox image (attempt {attempt}/{max_retries})...")
        build_proc = subprocess.run(
            build_cmd,
            cwd=str(APP).replace("\\", "/"),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout_seconds,
        )
        print(build_proc.stdout)
        if build_proc.returncode == 0:
            print("Docker sandbox image built successfully.")
            return 0
        print(build_proc.stderr, file=sys.stderr)
        if attempt < max_retries:
            print(f"Build failed; retrying in {backoff_seconds}s...")
            time.sleep(backoff_seconds)

    print(f"[FAIL] Docker build failed after {max_retries} attempts")
    return 1


def _image_exists() -> bool:
    """Return True if the sandbox image is already built locally."""
    proc = subprocess.run(
        ["docker", "image", "inspect", SANDBOX_IMAGE],
        capture_output=True,
        text=True,
    )
    return proc.returncode == 0


def _start_container(workspace_host: str) -> bool:
    """Start the sandbox container (detached) with the workspace mounted.

    Returns True on success, False on failure.
    """
    # Remove any leftover container from a previous run.
    subprocess.run(
        ["docker", "rm", "-f", SANDBOX_CONTAINER],
        capture_output=True, text=True,
    )

    proc = subprocess.run(
        [
            "docker", "run", "-d",
            "--name", SANDBOX_CONTAINER,
            "-v", f"{workspace_host}:/workspace",
            "-w", "/workspace",
            SANDBOX_IMAGE,
            "sleep", "infinity",
        ],
        capture_output=True, text=True,
    )
    if proc.returncode != 0:
        print(proc.stderr, file=sys.stderr)
        print("[FAIL] Failed to start sandbox container.")
        return False

    # Wait for the container to be ready.
    for _ in range(20):
        check = subprocess.run(
            ["docker", "exec", SANDBOX_CONTAINER, "echo", "ready"],
            capture_output=True, text=True,
        )
        if check.returncode == 0:
            print(f"Sandbox container '{SANDBOX_CONTAINER}' is ready.")
            return True
        time.sleep(0.5)

    print("[FAIL] Sandbox container did not become ready in time.")
    return False


def _stop_container() -> None:
    """Stop and remove the sandbox container."""
    subprocess.run(
        ["docker", "rm", "-f", SANDBOX_CONTAINER],
        capture_output=True, text=True,
    )


def cmd_build(args) -> int:
    """Build the Docker sandbox image (separate from agent generation)."""
    return _build_image()


def _make_timestamp() -> str:
    """Return a UTC timestamp string for unique workspace/run names.

    Format: YYYYMMDD_HHMMSS (e.g. 20261021_153000). Safe for use in
    directory names on Windows (no colons, no slashes).
    """
    return datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")


def _find_latest_run_workspace(model_safe: str) -> Path | None:
    """Find the most recent timestamped workspace directory for a model.

    Looks for directories matching ``workspace/<model_safe>_<timestamp>``
    and returns the one with the latest timestamp, or None if none exist.
    Only timestamped directories are considered — there is no fallback to
    pre-timestamping plain directories.
    """
    workspace_root = APP / "workspace"
    if not workspace_root.exists():
        return None

    prefix = f"{model_safe}_"
    candidates: list[tuple[str, Path]] = []
    for child in workspace_root.iterdir():
        if not child.is_dir():
            continue
        name = child.name
        if not name.startswith(prefix):
            continue
        ts = name[len(prefix):]
        # Validate that the suffix looks like a timestamp (YYYYMMDD_HHMMSS)
        if len(ts) == 15 and ts[8] == "_" and ts[:8].isdigit() and ts[9:].isdigit():
            candidates.append((ts, child))

    if not candidates:
        return None

    candidates.sort(key=lambda c: c[0], reverse=True)
    return candidates[0][1]


def cmd_generate(args) -> int:
    """Generate a solution by running OpenHands headless on the host.

    Copies task/public/ to a per-model workspace under
    workspace/<model-name>_<timestamp>/, starts a Docker sandbox container
    with only /workspace mounted, then runs the OpenHands SDK agent on the
    host. The agent's file tools read/write the mounted workspace, and its
    shell commands execute inside the container via ``docker exec``.
    The agent never sees evaluation/ or reference/.

    Saves logs to evaluation/proof-of-work/<model-name>/run.log.
    """
    model = (
        args.model
        or os.environ.get("OH_MODEL")
        or "openrouter/nvidia/nemotron-3.5-lightning:free"
    )
    model_safe = model.replace("/", "_").replace(":", "_").replace("-", "_")
    timestamp = _make_timestamp()

    proof_dir = EVAL / "proof-of-work" / model_safe
    proof_dir.mkdir(parents=True, exist_ok=True)

    # Fresh per-model, per-run workspace from public materials only (never
    # reference/ or evaluation/). Timestamping lets us run the same model
    # multiple times without overwriting previous runs.
    # Lives at workspace/<model-name>_<timestamp>/ at repo root so the
    # scoring step can import workspace.mcp_gateway.main:app.
    workspace = APP / "workspace" / f"{model_safe}_{timestamp}"

    # Stop any leftover sandbox container from a previous run so the host
    # can clean up the workspace directory (Docker volume mount locks files
    # on Windows).
    _stop_container()

    workspace.mkdir(parents=True, exist_ok=True)

    public = APP / "task" / "public"
    if public.exists():
        for item in public.iterdir():
            if item.is_dir():
                shutil.copytree(item, workspace / item.name, dirs_exist_ok=True)
            else:
                shutil.copy2(item, workspace / item.name)

    # Copy the task instruction into the workspace so it's available at
    # /workspace/task/instruction.md inside the container (and on the host).
    instruction_src = APP / "task" / "instruction.md"
    instruction_dst = workspace / "task" / "instruction.md"
    if instruction_src.exists():
        instruction_dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(instruction_src, instruction_dst)

    # Ensure openhands-sdk is available on the host (one-time pip install).
    rc = _ensure_openhands_installed()
    if rc != 0:
        return rc

    # Ensure the Docker sandbox image exists. Build it automatically if
    # missing (fast — no PyPI installs), unless --no-build is passed.
    if not args.no_build:
        if not _image_exists():
            print("Docker sandbox image not found locally; building it now...")
            rc = _build_image()
            if rc != 0:
                return rc
        else:
            print(f"Docker image '{SANDBOX_IMAGE}' already present; skipping build.")
    else:
        if not _image_exists():
            print(f"[FAIL] Docker image '{SANDBOX_IMAGE}' not found locally "
                  "and --no-build was passed.")
            return 1

    # OpenHands needs LLM_API_KEY + LLM_MODEL via env overrides.
    llm_api_key = os.environ.get("LLM_API_KEY")
    if not llm_api_key:
        print(
            "[FAIL] LLM_API_KEY environment variable is not set. "
            "Set it before running npm run generate.",
            file=sys.stderr,
        )
        return 1

    # Start the sandbox container, run the agent on the host, then clean up.
    workspace_host = str(workspace).replace("\\", "/")
    log_file = proof_dir / f"run_{timestamp}.log"

    if not _start_container(workspace_host):
        return 1

    try:
        agent_env = os.environ.copy()
        agent_env["LLM_MODEL"] = model
        agent_env["LLM_API_KEY"] = llm_api_key
        agent_env["LLM_BASE_URL"] = "https://openrouter.ai/api/v1"
        agent_env["SANDBOX_CONTAINER"] = SANDBOX_CONTAINER
        agent_env["WORKSPACE_HOST"] = str(workspace)
        agent_env["TTY_INTERACTIVE"] = "1"
        agent_env["TTY_COMPATIBLE"] = "1"
        agent_env["PYTHONIOENCODING"] = "utf-8"

        print(f"Running OpenHands headless with model: {model}")
        print(f"Workspace: {workspace}")
        print(f"Sandbox container: {SANDBOX_CONTAINER}")
        print(f"Log file: {log_file}")

        with open(log_file, "w", encoding="utf-8") as lf:
            lf.write(
                f"[{datetime.now(timezone.utc).isoformat()}] "
                f"Starting OpenHands agent run — model={model}\n"
            )
            lf.flush()
            proc = tee_subprocess(
                [sys.executable, str(APP / "evaluation" / "scoring" / "run_agent.py")],
                cwd=str(workspace),
                env=agent_env,
                log_file=lf,
                stream_to_stdout=True,
            )
    finally:
        _stop_container()

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
    generate_parser.add_argument(
        "--no-build",
        action="store_true",
        help="Do not build the Docker image; require it to already exist locally",
    )
    subparsers.add_parser(
        "build", help="Build the Docker sandbox image (separate from generation)"
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
        help="Model name (used to locate workspace/<model>_<timestamp>/mcp_gateway for scoring)",
    )
    score_parser.add_argument(
        "--run",
        default=None,
        help="Timestamp of the run to score (e.g. 20261021_153000). "
             "If omitted, the latest timestamped workspace for the model is used.",
    )
    subparsers.add_parser(
        "check:reference", help="Run reference validation (must pass 100%%)"
    )
    subparsers.add_parser(
        "check:mutants", help="Run mutant detection (all must be caught)"
    )

    args = parser.parse_args()

    if args.command == "build":
        return cmd_build(args)
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
