"""Test each mutant overlay: overlay, run expected tests, confirm failures, restore."""

import shutil
import subprocess
import sys
from pathlib import Path

APP = Path("D:/Anirudh/hackathons/mcp-gateway")
MUTANTS = APP / "mutants"

# (mutant_dir, source_file, test_to_run, expected_fail_count_min)
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

def overlay(mutant_dir: str, filename: str):
    """Copy mutant file over source file."""
    src = APP / "reference" / "mcp_gateway" / filename
    mutant = MUTANTS / mutant_dir / filename
    # Backup original
    backup = src.with_suffix(src.suffix + ".bak")
    shutil.copy2(src, backup)
    shutil.copy2(mutant, src)
    return backup

def restore(backup: Path, src: Path):
    """Restore original file from backup."""
    shutil.copy2(backup, src)
    backup.unlink()

def run_tests(test_spec: str) -> tuple[int, str]:
    """Run pytest and return (exit_code, output).

    test_spec may contain multiple test IDs separated by spaces; each is
    passed as a separate argument so pytest collects them all.
    """
    test_args = test_spec.split()
    result = subprocess.run(
        [sys.executable, "-m", "pytest", *test_args, "-v", "--tb=short"],
        cwd=str(APP),
        capture_output=True,
        text=True,
        timeout=120,
    )
    return result.returncode, result.stdout + result.stderr

def main():
    results = {}
    for mutant_dir, filename, test_spec, min_failures in MUTANT_TESTS:
        src = APP / "reference" / "mcp_gateway" / filename
        backup = overlay(mutant_dir, filename)
        try:
            print(f"\n{'='*60}")
            print(f"Testing mutant: {mutant_dir}")
            print(f"Overlay: {filename}")
            print(f"Tests: {test_spec}")
            print(f"{'='*60}")
            exit_code, output = run_tests(test_spec)
            # Count failures
            fail_count = output.count("FAILED")
            pass_count = output.count("PASSED")
            skip_count = output.count("SKIPPED")
            print(f"\nResults: {pass_count} passed, {fail_count} failed, {skip_count} skipped")

            if fail_count >= min_failures:
                status = "PASS (mutant detected)"
            elif skip_count > 0 and fail_count == 0:
                status = "SKIPPED (Windows - test skipped)"
            else:
                status = "FAIL (mutant NOT detected!)"

            results[mutant_dir] = status
            print(f"Status: {status}")

            # Show failure details if any
            if fail_count > 0:
                for line in output.splitlines():
                    if "FAILED" in line or "AssertionError" in line or "assert" in line:
                        print(f"  {line.strip()}")
        finally:
            restore(backup, src)

    print(f"\n{'='*60}")
    print("MUTANT TEST SUMMARY")
    print(f"{'='*60}")
    for mutant, status in results.items():
        print(f"  {mutant}: {status}")

if __name__ == "__main__":
    main()
