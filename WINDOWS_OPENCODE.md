# RD-Agent with OpenCode on Windows

This checkout runs the real upstream RD-Agent on Windows, driven by the OpenCode desktop app's models and Docker Desktop's Linux engine. Upstream `README.md:133-139` still documents Linux-only support, so treat Windows as a locally validated port rather than official parity.

## What works today (verified on this machine)

| Layer | Status |
| --- | --- |
| `local_ds:latest` image built from `rdagent/scenarios/kaggle/docker/DS_docker` | Built (72.8 GB), container imports pandas 2.3.3 / scikit-learn 1.6.1 |
| `DockerEnv` run + artifact writeback on Windows | Verified (exit 0, files written inside the container reappear in the host workspace) |
| LLM backend through OpenCode, no API key | Verified (`build_messages_and_create_chat_completion` returns real completions) |
| Embeddings without a vendor endpoint | Verified (local deterministic 256-dim vectors) |
| Native `local` execution backend | Available but unsandboxed |
| `conda` backend | Requires an existing environment |

## Model backend: OpenCode, no API key

`.env` sets:

```dotenv
BACKEND=rdagent.oai.backend.OpenCodeAPIBackend
OPENCODE_PROVIDER=github-copilot
OPENCODE_MODEL=auto
```

RD-Agent stores no provider API key; each prompt is sent to the locally running OpenCode service and may then be processed by the selected provider under that app's account. Select any model you have configured in OpenCode:

```powershell
python -m rdagent.oai.backend.opencode list-models   # everything OpenCode can serve
python -m rdagent.oai.backend.opencode current       # active provider/model
```

Change it by editing `OPENCODE_PROVIDER` / `OPENCODE_MODEL` in `.env`; the next RD-Agent process picks up the selection (restart a long-running RD-Agent process to reload it). No code change or OpenCode service restart is needed. Optional knobs: `OPENCODE_CLI` (used for model discovery), `OPENCODE_REQUEST_TIMEOUT` (default 900 s), `OPENCODE_EMBEDDING_DIM` (default 256).

`auto` is treated exactly like a literal model id: it is substituted once for `github-copilot/gpt-4.1` (`AUTO_ALIAS_MODEL` in `rdagent/oai/backend/opencode.py`) with no fallback chain, because Copilot's "Auto" is an in-app routing mode and the API rejects `id: "auto"` with `The requested model is not supported` (HTTP 502 through the OpenCode server).

Notes:
- Model discovery uses `opencode-cli`, found via `OPENCODE_CLI`, then `PATH`, then the desktop app's `resources` folder. Generation posts directly to the authenticated local service so large prompts avoid Windows' process-argument limit; it reads the OpenCode per-user `service.json` password in memory and sends it only to loopback. Do not copy or share that service registration. No provider API key is read by this backend.
- Free `opencode/*` models return `429 Free usage limit reached` quickly (`Daily free limit reached: $5.00 of model usage today`, resets 00:00 UTC); `copilot/*` and `github-copilot/*` work.
- OpenCode exposes no embedding endpoint, so embeddings are hashed locally. Retrieval quality is coarser than a real embedding model but functional and deterministic.

## Execution backends

```dotenv
DS_Coder_CoSTEER_env_type=docker
DS_Runner_CoSTEER_env_type=docker
```

Both must be set: generation and evaluation are separate CoSTEER phases.

- `docker` — generated code runs inside the scenario image. Recommended, and the only safe default for untrusted generated code.
- `local` — generated code runs as your Windows user. Not a sandbox; no Linux-only packages.

## Safe validation status

The authenticated HTTP transport was verified against the local OpenCode service with a synthetic prompt of 40,150 bytes; it returned a response from `github-copilot/gpt-4.1` without placing the prompt on a command line. Focused OpenCode backend tests: 16 passed. The bounded safe regression set (import paths, archive/Conda/Docker/artifact security, Windows and POSIX volume safety, console encoding, data-science parsing, Qlib no-implicit-download, Flask auth, OAI base, and OpenCode backend) passed 186 tests, 2 POSIX-only skips, 5 Pydantic deprecation warnings, and 3 subtests in 34.40s (re-run 2026-11-18); this suite used mocks/temp fixtures, with no external API or Docker execution. A separate ignored synthetic nonclinical fixture passed schema validation. A bounded DS attempt used `--competition rdagent-windows-synthetic-tabular --step-n 1 --loop-n 1 --timeout 3m`: OpenCode generated code, Docker executed it, evaluation returned true, and the expected scores/submission artifacts were written and inspected (train accuracy 1.0 on the intentionally deterministic toy data). The overall loop did not finish: it stopped at 40% on the configured step-count criterion after a CoSTEER fallback and `recover_ws_ckp failed due to one workspace is recovered twice.` Treat this as successful stage-level validation, not full RD-Agent E2E. No Kaggle route or download occurred.

Do not run the ARF demo dataset or any Medical scenario: it contains clinical data and is outside the requested scope. Future end-to-end checks must use a synthetic, non-clinical dataset created specifically for validation, after verifying the scenario's data domain.

The ARF run used during early infrastructure debugging confirmed that relative host volume paths and the Windows cleanup command now work. It then failed on generated sparse-data loading (`IndexError` from a malformed `X.npz` read); that dataset will not be rerun.

Large-prompt transport no longer uses the CLI command line, avoiding the prior Windows `WinError 206`. The transport change is verified with a synthetic 40 KB prompt, and the bounded synthetic workflow verified proposal, Docker code execution, evaluation, and artifact writeback, but not full loop completion. Logs go to `LOG_PATH` (default `git_ignore_folder/RD-Agent_workspace`); inspect session logs for workflow-stage failures.

## Windows fixes applied in this checkout

- `rdagent/utils/env.py`
  - `DockerEnv.prepare` no longer uses a rich spinner: on cp1252 consoles the braille character `\u2839` raised `UnicodeEncodeError` and masked real build errors. Build/pull progress is now logged.
  - Optional Docker SDK import with an actionable error; `get_docker_client()` wraps `docker.from_env()` with autostart of Docker Desktop and a bounded wait (`RDAGENT_DOCKER_AUTOSTART=0` disables, `RDAGENT_DOCKER_WAIT` defaults to 180 s).
  - `normalize_volumes` keeps POSIX container paths POSIX (previously `C:\kaggle\...`, rejected as `mount denied ... too many colons`) and joins relative paths with `posixpath` when the workspace lives in the container.
  - `normalize_volumes` also makes relative *host* paths absolute (e.g. `./git_ignore_folder/ds_data/...` from `DS_LOCAL_DATA_PATH`); Docker otherwise treats them as named volumes and fails with `includes invalid characters for a local volume name`.
  - Host cache mounts use `%TEMP%\rdagent\{sample,full}`; the literal `/tmp/full` host path is invalid on Windows.
  - `LocalEnv` uses Windows PATH handling, threaded stream readers, `taskkill /T /F` on timeout (exit 124), and copy-based volume staging with writeback.
- `rdagent/log/logger.py` — `_make_console_streams_encoding_safe()` reconfigures cp1252 stdout/stderr to `errors="replace"` (through colorama's `.wrapped` shim, skipping pytest capture), so any non-cp1252 glyph from a dataset script or generated code degrades to `?` instead of crashing the run.
- `rdagent/components/coder/data_science/conf.py` — `env_type` accepts `docker|conda|local`; Windows cleanup command is a `python -c` snippet with the file names passed as arguments and no single quotes (Docker envs wrap the entry in `sh -c '...'`, so quotes broke it).
- `rdagent/scenarios/data_science/loop.py` — `shutil`/`tarfile` instead of GNU `cp`/`tar`.
- `rdagent/oai/backend/opencode.py` — new backend (settings, authenticated loopback HTTP generation, local embeddings, CLI model discovery). CLI discovery uses `PATH`, `%LOCALAPPDATA%` on Windows, or conventional macOS/Linux locations; no user-specific Windows path is embedded. Dynamic `APIBackend()` loading and a live Copilot completion were verified (`DYNAMIC_BACKEND_OK`).

## Storage

This machine's Docker Desktop data disk was moved to a secondary drive and is reached through a junction at Docker Desktop's original data path. Keep large artifacts (images, datasets, logs) on the secondary drive; a full system drive can break Docker and the agent's own tooling.

## OpenCode integration

- `.opencode/commands/rdagent.md` — `/rdagent` guidance.
- `.opencode/skills/rdagent-windows/SKILL.md` — reusable platform notes.

OpenCode supplies the model and the coding-agent interaction; RD-Agent remains the experiment orchestrator. Do not commit `.env`, datasets, logs, or generated outputs.
