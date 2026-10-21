# OpenHands Frontier-Model Evaluation — Status Record

## Completed (verifier suite)

| Item | Result |
|------|--------|
| Full verifier suite (`pytest evaluation/tests/`) | 44 passed, 3 skipped (green) |
| All 6 mutants detected by verifier | Yes (auth disabled, wrong code, no sanitize, no ratelimit, no concurrency, no revocation) |
| GitHub repository | https://github.com/CipherCracked/mcp-gateway (public) |
| Collaborator access | Repo is public — `content.team@problemsetters.com` can read it (email is not a GitHub user, so a direct collaborator invite was not possible; ProblemSetters org exists but CipherCracked is not a member) |
| Final commit | `caea495` on `master`, pushed to `origin/master` |
| Commit attribution | `Co-Authored-By: Claude Code <noreply@anthropic.com>` |

## OpenHands SDK frontier-model evaluations

The OpenHands SDK (openhands-sdk==1.21.0) agent runs on the host (Windows), with file tools operating on the mounted workspace and shell commands executed inside the Docker sandbox via `docker exec`. The agent never sees `evaluation/` or `reference/`.

### Model 1: openrouter/nvidia/nemotron-3.5-lightning:free

| Item | Result |
|------|--------|
| Safe name | `openrouter_nvidia_nemotron_3.5_lightning_free` |
| Run log | `evaluation/proof-of-work/openrouter_nvidia_nemotron_3.5_lightning_free/run.log` (31,240 lines, 584 steps) |
| Monitor log | `evaluation/proof-of-work/openrouter_nvidia_nemotron_3.5_lightning_free/monitor.log` |
| Weighted score | **0.0%** (0 passed, 0 failed, 35 blocked out of 35 checks) |
| Frontier ceiling | 30% (0.3) |
| Below ceiling? | **Yes** (0.0% < 30%) |
| Failure reason | Gateway failed to start — `config.py` loads from `/app/config/gateway.yaml` which doesn't exist on host; `servers/` directory empty (no upstream MCP servers created); `servers/documents.py` and `servers/jobs.py` never written |
| Known agent issues | 29 "path escapes workspace" errors from Windows `Path.parts` bug (absolute `/workspace/...` paths never stripped); agent worked around by using `run_command` heredocs, writing files to `reference/mcp_gateway/` subdirectory instead of `mcp_gateway/` at workspace root |
| Scoring command used | `npm run score -- --model openrouter_nvidia_nemotron_3.5_lightning_free --target workspace` |

### Fixes applied to run_agent.py (for future runs)

1. **`_resolve_workspace_path` Windows path bug**: On Windows, `Path("/workspace/foo").parts` returns `('\\', 'workspace', 'foo')` not `('', 'workspace', 'foo')`. Fixed by accepting both `""` and `"\\"` as the first part when stripping the `/workspace/` prefix.

2. **LLMSummarizingCondenser added**: Configured with `max_size=200`, `keep_first=5`, `minimum_progress=0.1` to control context growth on paid models.

3. **`caching_prompt=True`**: Added explicitly to both the agent LLM and condenser LLM for prompt caching cost safety.

### Fixes applied to pipeline.py (this session)

1. **Removed backward-compat fallback from `_find_latest_run_workspace`**: The function now only considers timestamped directories (`workspace/<model>_<timestamp>`), returns `Path | None` — no fallback to pre-timestamping plain directories. The old plain workspace dir was renamed with a timestamp (`openrouter_nvidia_nemotron_3.5_lightning_free_20261021_162032`) so scoring can auto-pick it.

2. **`cmd_score` simplified**: Uses the simpler `Path | None` return — no tuple unpacking, no backward-compat `[WARN]` message.

### What is needed for the second frontier-model evaluation

1. Run `npm run generate --model <second-model>` to generate a second model's solution.
2. Run `npm run score --target workspace --model <second-model-safe-name>` to score it.
3. Confirm the second model's score is also < 30%.
4. Record both model results in this file.
