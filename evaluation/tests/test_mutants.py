"""Test each mutant overlay: apply, run in Docker, confirm failures, restore, repeat."""

import shutil
import subprocess
import sys
import time
from pathlib import Path

APP = Path(__file__).resolve()
REPO_ROOT = APP.parents[2]
MUTANTS = REPO_ROOT / "mutants"

# (mutant_dir, source_file, test_spec, min_failures)
MUTANT_TESTS = [
    ("mut_auth_disabled", "main.py",
     "evaluation/tests/test_auth_discovery.py::TestAuthentication::test_missing_api_key_returns_401 "
     "evaluation/tests/test_auth_discovery.py::TestAuthentication::test_invalid_api_key_returns_401",
     1),
    ("mut_wrong_code", "errors.py",
     "evaluation/tests/test_invocation_validation.py::TestToolInvocation::test_forbidden_tool_returns_403",
     1),
    ("mut_no_sanitize", "errors.py",
     "evaluation/tests/test_error_sanitization.py::TestErrorSanitization::test_auth_errors_sanitized "
     "evaluation/tests/test_error_sanitization.py::TestErrorSanitization::test_unhandled_exception_sanitized",
     1),
    ("mut_no_ratelimit", "limits.py",
     "evaluation/tests/test_rate_limiting_concurrency.py::TestRateLimiting::test_token_bucket_allows_burst_up_to_capacity",
     1),
    ("mut_no_concurrency", "limits.py",
     "evaluation/tests/test_rate_limiting_concurrency.py::TestConcurrency::test_concurrency_limit_rejects_overflow",
     1),
    ("mut_no_revocation", "main.py",
     "evaluation/tests/test_revocation.py::TestRevocation::test_sighup_revokes_tool_for_new_invocations",
     0),  # skipped on Windows
]


def overlay(mutant_dir: str, filename: str) -> Path:
    """Copy mutant file over source file. Returns backup path."""
    src = REPO_ROOT / "reference" / "mcp_gateway" / filename
    mutant = MUTANTS / mutant_dir / filename
    backup = src.with_suffix(src.suffix + ".bak")
    shutil.copy2(src, backup)
    shutil.copy2(mutant, src)
    return backup


def restore(backup: Path, src: Path):
    """Restore original file from backup. Best-effort with error logging."""
    try:
        if backup.exists():
            shutil.copy2(backup, src)
            print(f"[INFO] Restored {src} from {backup}")
        else:
            print(f"[WARN] Backup not found: {backup}", file=sys.stderr)
    except Exception as e:
        print(f"[ERROR] Failed to restore {src}: {e}", file=sys.stderr)
    finally:
        # Always try to remove backup file
        try:
            if backup.exists():
                backup.unlink()
        except Exception:
            pass


def run_cmd(cmd: list[str], cwd: Path, timeout: int = 120) -> tuple[int, str]:
    """Run command and return (exit_code, output)."""
    result = subprocess.run(
        cmd,
        cwd=str(cwd),
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    return result.returncode, result.stdout + result.stderr


def build_reference() -> tuple[int, str]:
    """Run build.py for reference target."""
    return run_cmd(
        [sys.executable, str(REPO_ROOT / "app-setup" / "build.py"), "reference"],
        REPO_ROOT,
        timeout=60,
    )


def start_gateway() -> tuple[int, str]:
    """Run start.py for reference target."""
    return run_cmd(
        [sys.executable, str(REPO_ROOT / "app-setup" / "start.py"), "reference", str(REPO_ROOT)],
        REPO_ROOT,
        timeout=120,
    )


def reset_gateway() -> tuple[int, str]:
    """Run reset.py."""
    return run_cmd(
        [sys.executable, str(REPO_ROOT / "app-setup" / "reset.py")],
        REPO_ROOT,
        timeout=60,
    )


def run_tests(test_spec: str) -> tuple[int, str]:
    """Run pytest with given test spec."""
    test_args = test_spec.split()
    return run_cmd(
        [sys.executable, "-m", "pytest", *test_args, "-v", "--tb=short"],
        REPO_ROOT,
        timeout=120,
    )


def main():
    results = {}
    overall_pass = True

    for mutant_dir, filename, test_spec, min_failures in MUTANT_TESTS:
        src = REPO_ROOT / "reference" / "mcp_gateway" / filename
        backup = overlay(mutant_dir, filename)

        try:
            print(f"\n{'='*60}")
            print(f"Testing mutant: {mutant_dir}")
            print(f"Overlay: {filename}")
            print(f"Tests: {test_spec}")
            print(f"{'='*60}")

            # Build reference (writes config with mutated code path)
            print("[INFO] Building reference...")
            rc, out = build_reference()
            if rc != 0:
                print(f"[FAIL] Build failed: {out}")
                results[mutant_dir] = "FAIL (build failed)"
                overall_pass = False
                continue

            # Start gateway in Docker with mutated code
            print("[INFO] Starting gateway in Docker...")
            rc, out = start_gateway()
            if rc != 0:
                print(f"[FAIL] Gateway start failed: {out}")
                results[mutant_dir] = "FAIL (gateway start failed)"
                overall_pass = False
                continue

            try:
                # Run the specific tests
                print("[INFO] Running tests...")
                rc, out = run_tests(test_spec)

                # Count results
                fail_count = out.count("FAILED")
                pass_count = out.count("PASSED")
                skip_count = out.count("SKIPPED")
                print(f"\nResults: {pass_count} passed, {fail_count} failed, {skip_count} skipped")

                if fail_count >= min_failures:
                    status = "PASS (mutant detected)"
                elif skip_count > 0 and fail_count == 0:
                    status = "SKIPPED (Windows - test skipped)"
                else:
                    status = "FAIL (mutant NOT detected!)"
                    overall_pass = False

                results[mutant_dir] = status
                print(f"Status: {status}")

                if fail_count > 0:
                    for line in out.splitlines():
                        if "FAILED" in line or "AssertionError" in line or "assert" in line:
                            print(f"  {line.strip()}")
            finally:
                # Always reset gateway
                print("[INFO] Resetting gateway...")
                reset_gateway()

        finally:
            # Always restore original file
            restore(backup, src)

    print(f"\n{'='*60}")
    print("MUTANT TEST SUMMARY")
    print(f"{'='*60}")
    for mutant, status in results.items():
        print(f"  {mutant}: {status}")

    # Final safety: verify reference files are restored
    for _, filename, _, _ in MUTANT_TESTS:
        src = REPO_ROOT / "reference" / "mcp_gateway" / filename
        bak = src.with_suffix(src.suffix + ".bak")
        if bak.exists():
            print(f"[WARN] Backup file still exists: {bak}, attempting restore...")
            try:
                shutil.copy2(bak, src)
                bak.unlink()
                print(f"[INFO] Restored {src} from leftover backup")
            except Exception as e:
                print(f"[ERROR] Failed to restore from leftover backup: {e}")

    if overall_pass:
        print("\n[PASS] All mutants detected")
        return 0
    else:
        print("\n[FAIL] Not all mutants were detected")
        return 1


if __name__ == "__main__":
    sys.exit(main())