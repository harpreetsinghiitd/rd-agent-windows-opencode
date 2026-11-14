---
description: Run Microsoft RD-Agent workflows using this PC's OpenCode models and Docker on Windows
---

Use the installed upstream RD-Agent checkout in this repository (`RD-Agent`). Inspect the scenario config/README before inventing CLI flags. RD-Agent generates and executes code; treat generated code as untrusted.

## Model backend: OpenCode (no API key)

`BACKEND=rdagent.oai.backend.OpenCodeAPIBackend` in `.env` routes every completion through the OpenCode desktop app already signed in on this machine. RD-Agent stores no provider API key; prompts go to the local OpenCode service and may then be processed by the selected provider under that app's account.

- List what OpenCode can serve: `python -m rdagent.oai.backend.opencode list-models`
- Show the active selection: `python -m rdagent.oai.backend.opencode current`
- Change model without touching code: set `OPENCODE_PROVIDER` / `OPENCODE_MODEL` in `.env` (or the process env). Default is `github-copilot` / `auto`.
- `auto` is used like any other model id: it is resolved once to a concrete Copilot model (`github-copilot/gpt-4.1`), because Copilot's "Auto" is a routing mode inside the Copilot apps and the API rejects `id: "auto"` with `The requested model is not supported` (HTTP 502 via the OpenCode server). There is no fallback chain — one model, one code path.
- If a model errors with `429 ... Free usage limit reached`, pick a different provider/model; free `opencode/*` models have a small quota (`Daily free limit reached: $5.00 of model usage today`, resets 00:00 UTC).
- Model discovery uses the running OpenCode app's CLI (`OPENCODE_CLI` overrides discovery). Generation uses authenticated HTTP to the local service to support long prompts without a Windows command-line limit; the per-user `service.json` password is read in memory and sent only to loopback. Never print, copy, or share that registration/password.
- Embeddings have no OpenCode endpoint, so they are computed locally and deterministically (`OPENCODE_EMBEDDING_DIM`, default 256).

## Execution backend

- `docker` (default here) runs generated code inside the scenario image; on Windows use Docker Desktop with the Linux engine running.
- `local` runs generated code with your own Windows user permissions, unsandboxed. Only use it when explicitly accepted.
- Set both backends consistently: `DS_Coder_CoSTEER_env_type` and `DS_Runner_CoSTEER_env_type`.
- Do not silently install or enable WSL2/Linux distributions.

## Safe validation and known limits

Do not run the ARF demo dataset or any Medical scenario: it contains clinical data and is outside the requested scope. For future end-to-end validation, use a synthetic, non-clinical dataset created specifically for the test. Do not assume an upstream competition name is safe without checking its data/domain first.

Windows Docker fixes already applied:
- Relative host-side volume paths are converted to absolute paths before passing binds to Docker; otherwise Docker may interpret them as named volumes and reject them.
- The Windows cleanup entry uses a `python -c` snippet with file names passed as arguments and no nested single quotes, because the Docker wrapper runs commands through `sh -c '...'`.

Large-prompt transport was verified against the local OpenCode service using a synthetic 40 KB prompt; dynamic `APIBackend()` loading and a live Copilot completion also returned `DYNAMIC_BACKEND_OK`. Focused backend/Windows regression tests pass. Full RD-Agent workflows still need validation on a safe non-clinical dataset; do not treat the earlier ARF run as a success or rerun it. Logs are written under `LOG_PATH` (default `git_ignore_folder/RD-Agent_workspace`); inspect them for the real failure cause.

## Storage

This machine's Docker data disk was moved to a secondary drive and remains accessible through Docker Desktop's original data path via a junction. Keep new large artifacts on the secondary drive.

## Reporting

Report the exact command, exit status, scenario, model and execution backend, plus real limitations. Upstream `README.md:133-139` still documents Linux-only support, so do not claim full parity.
