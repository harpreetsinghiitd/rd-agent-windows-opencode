---
name: rdagent-windows
description: Configure and run Microsoft RD-Agent from OpenCode on Windows with the OpenCode model backend, Docker, or native execution
---

# RD-Agent on Windows

This is Microsoft RD-Agent's upstream repository with a local Windows portability patch. Upstream `README.md:133-139` still states Linux-only support, so refer to this as a validated local port, not official parity. OpenCode supplies the models and the coding-agent layer; RD-Agent remains the autonomous experiment orchestrator.

## Model backend (no API key)

- `.env` uses `BACKEND=rdagent.oai.backend.OpenCodeAPIBackend`; RD-Agent stores no provider API key. Prompts go to the local OpenCode service and may be processed by the selected provider under that app's account.
- Enumerate options with `python -m rdagent.oai.backend.opencode list-models`; show the active one with `... current`.
- Switch models by editing `OPENCODE_PROVIDER` / `OPENCODE_MODEL` in `.env` — no code change required.
- `auto` is just an alias resolved once to `github-copilot/gpt-4.1` (Copilot's API rejects `id: "auto"` with `The requested model is not supported`); no fallback chain.
- If a model returns `429 Free usage limit reached`, choose another provider/model (`copilot/*`, `github-copilot/*` work).
- Never print or commit `.env`.

## Execution backend

1. Verify `rdagent data_science --help` and that the dataset exists under `DS_LOCAL_DATA_PATH`.
2. Set both phases consistently: `DS_Coder_CoSTEER_env_type` and `DS_Runner_CoSTEER_env_type`.
3. `docker` executes generated code inside the scenario image — the only safe default. Verify Docker Desktop's Linux engine first (`docker version`).
4. `local` executes generated code with the current user's Windows permissions and is not a sandbox; use only when explicitly accepted.
5. `conda` requires an environment that already has the scenario dependencies.
6. Do not silently install or enable WSL2/Docker components.

## Run and report

- Start only with an explicitly approved, synthetic non-clinical dataset. Do not use the ARF example or any Medical scenario; verify the dataset's domain before running.
- Inspect the session logs under `LOG_PATH` for the real failure cause; RD-Agent retries and surfaces errors in logs.
- Report the exact command, exit code, scenario, model, execution backend, and remaining limitations. Never claim full Windows parity.
- Keep datasets, `.env`, caches, logs and generated outputs out of commits. Large files belong on D: (Docker's data disk already lives there).
