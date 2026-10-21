# Secure MCP Gateway — RL Gym Evaluation Task

A repository-level RL Gym task for evaluating autonomous AI coding agents. The task is a **Secure MCP Gateway with Policy Enforcement** that aggregates multiple upstream MCP servers behind a unified interface with fine-grained access control.

## What this is

This is an evaluation harness for frontier AI coding agents. The model is given a task specification (`task/instruction.md`) and public materials (`task/public/`), then must implement a Secure MCP Gateway from scratch. The model's solution is scored by a hidden verifier suite using weighted partial credit (HackerRank-Bench methodology), with mutant injection to prove the test suite reliably catches failures.

The goal: two different frontier models should score **below 30%** on a single-pass evaluation, proving the task is sufficiently challenging.

## Repository structure

```
secure-mcp-gateway/
├── README.md                 # This file
├── package.json              # Root scripts: generate, score, check:mutants, check:reference, build
├── PLAN.md                   # Internal implementation plan (not committed)
├── task/                     # Model-facing materials (visible to the agent)
│   ├── instruction.md        # Task specification given to the model
│   └── public/               # Public starter materials
│       └── gateway.yaml.example
├── environment/              # Docker sandbox definition
│   └── Dockerfile            # Minimal sandbox (python:3.12-slim + curl)
├── app-setup/                # Task lifecycle scripts
│   ├── build.sh              # Build Docker image
│   ├── start.sh              # Start gateway container
│   └── reset.sh              # Reset container + state
├── evaluation/               # Hidden verifier suite (NOT visible to the agent)
│   ├── tests/                # Black-box verifier tests
│   ├── acceptance-criteria.yml  # Requirement -> check ID mapping
│   ├── scoring.yml           # Check weights (sum to 1.0)
│   └── proof-of-work/        # Run logs and scores per model
│       └── <model-name>/run.log
├── reference/                # Reference solution (passes 100%)
│   └── mcp_gateway/
├── mutants/                  # Mutant overlays for detection testing
├── workspace/                # Per-model agent workspaces (generated at runtime)
│   └── <model-safe-name>/
├── evaluation/scoring/
│   ├── pipeline.py           # Scoring pipeline (generate / score / check:mutants / check:reference)
│   └── run_agent.py          # OpenHands SDK agent driver (runs on host)
└── test_mutants.py           # Mutant injection test script
```

## How it works

### Architecture

The OpenHands SDK agent runs **on the host** (Windows). Its file tools read/write the model's workspace directory, which is mounted into a Docker sandbox container at `/workspace`. Shell commands execute inside the container via `docker exec` — the agent never runs code directly on the host, and never sees `evaluation/` or `reference/`.

```
Host (Windows)                          Docker Sandbox Container
─────────────────                       ────────────────────────
OpenHands SDK Agent                     python:3.12-slim
  read_file/write_file  <------->   /workspace/ (mounted)
  run_command  -------->  docker exec  sleep infinity
```

### Evaluation flow

1. **Generate**: `npm run generate -- --model <model-name>`
   - Copies `task/public/` to a per-model, per-run workspace at
     `workspace/<model-safe-name>_<timestamp>/` (timestamped so multiple runs
     per model don't overwrite each other)
   - Starts the Docker sandbox container with only `/workspace` mounted
   - Runs the OpenHands SDK agent on the host against the task instruction
   - The agent writes code, runs commands, and iterates until done
   - Logs stream live to stdout and are saved to
     `evaluation/proof-of-work/<model-name>/run.log`

2. **Score**: `npm run score -- --model <model-name> --target workspace`
   - Copies the model's `workspace/<model-safe-name>_<timestamp>/mcp_gateway`
     into `workspace/mcp_gateway/`
   - Use `--run <timestamp>` to score a specific run; if omitted, the latest
     timestamped workspace for that model is auto-selected
   - Sets `GATEWAY_MODULE=workspace.mcp_gateway.main:app`
   - Starts the gateway container via `app-setup/start.sh`
   - Runs the hidden verifier test suite
   - Computes weighted partial credit score (sum(weight_i * completion_i) / sum(weight_i))
   - Reports pass/fail against the 30% frontier ceiling

### package.json scripts

| Script | Purpose |
|--------|---------|
| `npm run build` | Build the Docker sandbox image |
| `npm run generate -- --model <model>` | Run OpenHands agent to generate a model solution |
| `npm run score -- --model <model> --target workspace` | Score a model's solution |
| `npm run score -- --target reference` | Score the reference solution (must pass 100%) |
| `npm run check:mutants` | Run mutant detection (all must be caught) |
| `npm run check:reference` | Validate the reference passes 100% |

## Prerequisites

- **Docker** (Docker Desktop on Windows) — running and accessible
- **Python 3.11+** with `uv` venv at `.venv/`
- **OpenHands SDK** (`openhands-sdk==1.21.0`) installed in the host venv
- **OpenRouter API key** — set `LLM_API_KEY` in your environment or `.env` file
  - Never commit or share API key values

## Quick start

```bash
# Clone or navigate to the repo
cd D:\Anirudh\hackathons\mcp-gateway

# Ensure Docker is running

# Set your OpenRouter API key (never commit this)
set LLM_API_KEY=your-openrouter-api-key

# Build the Docker sandbox image (one-time)
npm run build

# Generate a solution with a frontier model
npm run generate -- --model openrouter/nvidia/nemotron-3.5-lightning:free

# Score the generated solution
npm run score -- --model openrouter_nvidia_nemotron_3_5_lightning_free --target workspace

# Validate the reference solution (must pass 100%)
npm run check:reference

# Run mutant detection (all must be caught)
npm run check:mutants
```

### Model name formatting

The `--model` flag accepts OpenRouter model names like `openrouter/nvidia/nemotron-3.5-lightning:free`. Internally it's converted to a filesystem-safe name by replacing `/`, `:`, and `-` with `_`. For scoring, use the converted name:

- Model: `openrouter/nvidia/nemotron-3.5-lightning:free`
- Safe name: `openrouter_nvidia_nemotron_3_5_lightning_free`

## Acceptance criteria

- Reference solution passes 100% of score-bearing verifier checks
- Two different frontier models score below 30% on single-pass evaluation
- Realistic mutants are all caught by the verifier
- Repeated verifier runs are deterministic
- Scoring weights sum exactly to 1.0
- Private verifier assets (`evaluation/`, `reference/`) remain isolated from model-facing files
- Docker build/start/reset works reproducibly

## Privacy and security

- The agent only sees `task/` (instruction + public materials) — never `evaluation/` or `reference/`
- The sandbox container mounts only the model's workspace directory at `/workspace`
- API keys are read from environment variables, never stored in the repo
- Run logs are saved to `evaluation/proof-of-work/<model-name>/run.log`

## Key files

| File | Purpose |
|------|---------|
| `evaluation/scoring/pipeline.py` | Scoring pipeline: generate, score, check:mutants, check:reference |
| `evaluation/scoring/run_agent.py` | OpenHands SDK agent driver with custom file/shell tools |
| `environment/Dockerfile` | Minimal sandbox image (no PyPI, no OpenHands inside) |
| `app-setup/start.sh` | Starts the gateway container for scoring |
| `app-setup/build.sh` | Builds the Docker image |
| `app-setup/reset.sh` | Resets container and persisted state |
| `test_mutants.py` | Mutant injection and detection testing |
