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

## OpenHands frontier-model evaluations — BLOCKED

The plan requires two OpenHands frontier-model evaluations, each scoring < 30%. This could **not** be completed in this session for the following reasons:

1. **OpenHands platform not installed.** `pip install openhands` installs a 0.0.0 stub package with no CLI and no agent runtime. The real OpenHands platform (OpenHands/OpenHands) is a large Docker-based system (backend + frontend + agent SDK) that must be cloned and built separately.

2. **Docker daemon not running.** `docker ps` fails: `failed to connect to the docker API at npipe:////./pipe/dockerDesktopLinuxEngine`. Docker Desktop would need to be started (WSL2 Ubuntu is installed but the Docker engine socket is not reachable).

3. **No frontier-model API keys.** `ANTHROPIC_API_KEY` and `OPENAI_API_KEY` are both unset. The only key present (`ANTHROPIC_AUTH_TOKEN`) is an OpenRouter session token bound to this Claude Code session; it is not a frontier-model key available for powering evaluated agent runs, and using it for that purpose would not constitute a legitimate frontier-model evaluation.

## What is needed to complete the OpenHands evaluations

1. Start Docker Desktop (WSL2 backend) so the Docker daemon is reachable.
2. Clone and install the real OpenHands platform (OpenHands/OpenHands), following its setup instructions (`make build` / `uv sync`).
3. Obtain API keys for two frontier models (e.g., Anthropic Claude, OpenAI GPT) and configure them in an OpenHands LLM config.
4. Package the mcp-gateway task as an OpenHands evaluation instance (instruction + repo + verifier).
5. Run each model single-pass on the task and record: model name, logs, score, failure categories in `evaluation/proof-of-work/`.
6. Confirm both scores are < 30%.

Until those prerequisites are in place, no authentic OpenHands scores can be produced or recorded.
