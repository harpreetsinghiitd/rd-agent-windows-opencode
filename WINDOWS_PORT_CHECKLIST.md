# RD-Agent Windows/OpenCode Port — Work Checklist

Goal: make the upstream RD-Agent checkout as reliable as practical with Windows as the host and OpenCode as the model gateway. This checklist records evidence, not promises of zero future failures.

## Non-negotiable validation guardrails

- [x] Do not run any Medical scenario or clinical dataset.
- [x] Do not make further ARF attempts. Historical note: an earlier ARF attempt occurred despite this guardrail and failed during generated sparse-data loading; it is documented honestly, and ARF is never to be rerun.
- [x] Do not install/enable WSL or a Linux distribution. Docker Desktop's existing Linux container engine is allowed when needed.
- [x] Do not use native `local` execution for generated code unless explicitly approved; it is unsandboxed. Prefer Docker.
- [x] Create and inspect a synthetic, non-clinical fixture before workflow validation (fixture and bounded stages recorded in Phase 4).
- [x] Keep `.env`, credentials, datasets, generated artifacts, and logs out of Git; verify ignored paths before any commit.

## Phases

### 1. Inventory and support matrix — INVENTORY COMPLETE; PORTABILITY PARTIAL

| Entry point / workflow | Windows host + Docker boundary | Data / providers / resources | Safe validation status |
|---|---|---|---|
| `data_science` (`rdagent/app/cli.py`) | Windows host starts Docker Desktop; Linux container executes generated code. CLI default is Kaggle, so configure a custom scenario for a safe fixture. | OpenCode provider; dataset-specific Python deps; no GPU intrinsically required. | Synthetic proposal/codegen, Docker execution, evaluation and artifact stages passed; overall loop incomplete. ARF is explicitly forbidden. |
| `fin_quant`, `fin_factor`, `fin_model` | Windows control plane with Linux Docker/Qlib; defaults include GPU enablement and 16 GB shared memory. | Qlib provider data, model/dependency images; defaults use 2008–2020 and validator hardcodes SH600000. | No workflow E2E; synthetic validation blocked until a production-safe provider/validator injection exists. |
| `fin_factor_report`, `general_model` | Docker/Qlib and report/PDF processing. | Nonclinical PDF, Qlib, OpenCode; local file path accepted by `general_model` CLI. README URL example differs from CLI signature. | No safe E2E; can use a synthetic nonclinical PDF only after dependencies/config are bounded. |
| `llm_finetune` | Linux/Docker or Conda; Windows host does not remove Linux training assumptions. | HuggingFace/model/dataset downloads, GPU, potentially long (100 h); dataset setup can occur even with `--dataset`. | Defer; no model download or training run. |
| Standard log UI / DS interaction | Streamlit on host; Flask log server requires `UI_SERVER_AUTH_TOKEN`. | Existing local logs; UI renders scenario HTML as trusted (`unsafe_allow_html=True`), so logs must be trusted. | Standard UI returned HTTP 200/7,863-byte HTML; visual/log content not checked. |
| `app/rl` / RL AutoRL-Bench (not main CLI) | Docker/Linux; POSIX `ps`, `os.getsid`, `os.killpg`, and WebShop Bash scripts block native Windows. Separate RL Streamlit viewer safe-resolves paths. | HuggingFace models/data, benchmarks, GPU and large resources. | Unsupported pending a dedicated port; not run/downloaded. |
| `app/kaggle` (not main CLI) | Docker/Linux and Kaggle CLI integration. | Competition download/token; optional automatic submission. | Not run; no downloads/submissions. |
| `app/CI/run.py` (interactive code repair) | Hard-coded `/usr/bin/git` paths are a native-Windows blocker; loop edits source files and writes reports. | OpenCode, Git, Ruff/mypy; repository changes. | Audited only, not run. |
| `app/finetune/data_science/loop.py` | Separate Fire loop; normal DS Docker/data requirements. | Requires competition data and `<competition>/prev_model`. | Not run; no data copied/downloaded. |
| `app/utils/ws.py`, `ws_ft.py` | Standalone Typer command runners inside DS/finetune Docker environments. | Docker plus local workspace/data/model mounts. | Not run; arbitrary supplied command executes in container. |
| `app/benchmark/{factor,model}` and `scenarios/finetune/benchmark` | Benchmark scripts read configured task/results; finetune benchmark defaults to Qwen3-1.7B, AIME25, one GPU. | Qlib/benchmark assets, model adapter and possibly judge API key. | Not run; model/assets not downloaded. |
| `scenarios/kaggle/knowledge_management` | Helpers process `.case` files and write graph/vector pickle files. | LLM and embeddings (OpenCode local deterministic embedding implementation). | Not run; no source corpus selected. |
| `scenarios/data_science/proposal/exp_gen/select/submit.py` | Offline selector evaluator; debug tarball path defaults to `/mnt/output` (Linux/container). | Existing logs/traces. | Not run; no log corpus selected. |
| `health_check`, `collect_info`, `grade_summary`, UI | Local host utilities/views; individual checks may probe Docker/ports. | Existing state/logs. | CLI surface inventoried; health/collect not executed here. |

- [x] Enumerate main CLI commands and the audited non-CLI application/scenario entrypoints (including Kaggle, RL, CI repair, benchmark, and dataset utilities).
- [x] Record known OS/Docker/data/provider/GPU blockers for the audited entrypoints; plugin-level test coverage and safe smoke tests remain incomplete.
- [x] Mark major known blockers unsupported instead of claiming native Windows parity. Upstream README itself says Linux-only.

### 2. Test-suite failure triage — PARTIAL
- [x] Preserve the broad baseline: `python -m pytest test\\utils test\\oai -q` reported 347 failed, 47 passed, 8 warnings, 2 errors, 12 subtests in 2127.18s. There is no saved pytest/JUnit report, so individual failures cannot be attributed retrospectively.
- [x] Failure classes identified: live LLM/embedding and Context7 calls; Docker/Qlib/Conda/hard-coded Linux-path tests; workflow/download tests; Windows portability candidates. `test_llm_connectivity.py::test_model` may be a missing-fixture collection error (the parameter has no visible fixture); not confirmed as a live network failure. The Docker memory error is consistent with the daemon being stopped.
- [x] Reproducible local regressions were fixed with regression coverage; external-service/data/download tests were kept out of the safe pass.
- [x] Latest bounded safe set: `python -m pytest test\\utils\\test_import_paths.py test\\utils\\test_archive_security.py test\\utils\\test_conda_security.py test\\utils\\test_docker_reconnect.py test\\utils\\test_artifact_transport.py test\\utils\\test_env_windows.py test\\utils\\test_env_posix_volumes.py test\\scenarios\\data_science\\test_secure_parsing.py test\\scenarios\\data_science\\test_debug_data.py test\\log\\server\\test_security.py test\\oai\\test_base.py test\\oai\\test_opencode_backend.py -q` → 175 passed, 2 skipped, 5 Pydantic deprecation warnings, 3 subtests in 34.36s. The POSIX `LocalEnv` integration tests skip on Windows; portable target-preservation helpers run. It includes pure import-filter tests; it does not import the full package tree. This set uses mocks/temp fixtures, with no external API or Docker execution.
- [ ] Run a broad platform-safe suite only after establishing reliable selection/markers; `offline` marker is not automatically excluded by pytest. Do not repeat the 35-minute suite without a specific hypothesis.
- [x] Latest bounded import/security/backend set includes Windows import-path tests and portable POSIX-volume target tests; full import sweep is intentionally excluded pending side-effect review.

### 3. Common Windows runtime and packaging — PARTIAL
- [x] OpenCode backend integration, selectable provider/model settings, authenticated loopback transport, local deterministic embeddings, and safe settings logging.
- [x] Windows LocalEnv PATH/timeout/stream/volume staging behavior (including deletion mirroring, rejection of host/staged symlinks, preservation when staging fails, and continuing cleanup across mounts after one copyback error); POSIX LocalEnv refuses to replace occupied volume targets (directory/file sentinels preserved; stale symlink can be removed). Docker volume normalization, bounded Docker Desktop autostart, and console encoding fixes have focused tests.
- [x] Audit high-risk shared assumptions: DockerEnv still wraps commands in `/bin/sh`, uses GNU `find`/`chmod` and Linux containers; native Conda preparation has unquoted shell commands; non-Windows live-output branch uses `select.poll`; these are not supported by native Windows without refactoring. Fixed the `test/utils/test_import.py` Windows path-to-module conversion; import-all sweep remains unrun because imports may have side effects.
- [x] Verify editable-install dependencies (`pip check` clean; `rdagent==1.0.1.dev0` resolves to this checkout) and CLI/backend startup. Converted OpenCode CLI candidates to `%LOCALAPPDATA%`/current-profile paths and tested an alternate profile; explicit `OPENCODE_CLI` remains available. A truly clean isolated clone/install has not been performed.
- [x] Docker disk image moved to `D:\DockerData\wsl\disk` behind the original C: path junction; current free space observed C: 85.26 GB, D: 20.33 GB (checklist artifacts are currently on C:, so maintain D: for large data).

### 4. Data-science workflow — bounded stage validation; full loop incomplete
- [x] Create and inspect an ignored deterministic, synthetic nonclinical binary-classification fixture (`git_ignore_folder\\ds_data\\rdagent-windows-synthetic-tabular`; 16 train, 4 test, 4 submission rows; target is a deterministic threshold). Validate schemas, label rule, IDs, nulls and Git ignore status; provide a matching prebuilt sample folder so no sampler/download is needed.
- [x] Configure `.env` with `DataScienceScen`, `DS_SAMPLE_DATA_BY_LLM=False`, both coder and runner env types `docker`; pass explicit `--competition rdagent-windows-synthetic-tabular` because the CLI default is Kaggle BMS. Run command: `python -m rdagent.app.cli data_science --competition rdagent-windows-synthetic-tabular --step-n 1 --loop-n 1 --timeout 3m`.
- [x] Bounded stage validation: OpenCode produced a proposal and generated code; `main.py` executed in `local_ds:latest` via `coverage`/`strace`; generated outputs were independently inspected. `scores.csv` contains `threshold_rule,1.0` and `ensemble,1.0`; `submission.csv` has `row_id,target` with four expected rows. Separate submission-format checker confirmed matching columns/rows. Evaluator returned `final_decision: true`. Logs/artifacts are under the ignored workspace `git_ignore_folder\\RD-Agent_workspace\\4c98aecbaad24eb394ebbe581c46f476`.
- [ ] Full RD-Agent loop completion. The bounded command stopped at 40% / `step_name=running` after reaching the configured step-count criterion. Before stopping, CoSTEER logged `Fallback to the fallback solution.` and `recover_ws_ckp failed due to one workspace is recovered twice.` The generated-code execution and artifacts succeeded, but the overall workflow did not complete; investigate this recovery/loop behavior before claiming full E2E.
- [x] Preflight: Docker was running; `local_ds:latest` had pandas 2.3.3, sklearn 1.6.1, coverage 7.17.0 and `/usr/bin/strace`. The output-format checker reads `sample_submission.csv`; the generated `main.py` itself reads only train/test files. No Kaggle route or download occurred.
- [ ] Fix reproducible workflow orchestration issues and add regression tests. Never substitute ARF/Medical data.

### 5. Financial workflows, especially `fin_quant` — AUDIT COMPLETE; UNSUPPORTED WITHOUT PROVIDER DATA
- [x] Read `fin_quant` config, Docker/Qlib boundaries and data validator; `rdagent/utils/qlib.py::validate_qlib_features` writes a validation script that calls bare `qlib.init()` and requests `D.features(["SH600000"], ..., 2008-01-01..2020-08-31)`. `QLIB_QUANT_` exposes dates/evolution/component settings but no provider URI or validator override.
- [x] Determined that synthetic provider substitution would require changing production validator/data-provider semantics; no safe no-code override was found. Leave synthetic `fin_quant` validation unsupported pending an explicit provider-injection design.
- [ ] Verify output artifacts/reporting and audit `fin_factor`, `fin_model`, and `fin_factor_report` beyond the shared initial inventory.

### 6. Other non-medical workflows — ENTRYPOINT INVENTORY COMPLETE; RUNTIME SUPPORT PARTIAL
- [x] Record prerequisites and defer finetuning (GPU/HF/data downloads/long duration), Kaggle downloads/submissions, RL model/data downloads, and interactive CI source edits.
- [x] Finish the hidden-entrypoint audit (including benchmark/scenario utilities) and record support boundaries; no downloads, submissions, or unapproved workflow runs were triggered.
- [ ] Consider a bounded nonclinical PDF-only `general_model`/factor-report validation only after their Qlib/provider requirements are understood.

### 7. UI and observability — PARTIAL
- [x] Historical standard log UI HTTP probe at `http://127.0.0.1:19899` returned `200` and Streamlit HTML (7,863 bytes); the server is not currently running.
- [x] Visual check using only an HMAC-signed `GeneralModelScenario` with checked-in static description on isolated port 19989. After waiting for readiness, the UI returned HTTP 200, rendered the scenario title/description and controls, and Playwright reported no page/console/request errors. Screenshot: `%TEMP%\rdagent-ui-trusted-fixture-brkqkpq1\ui-screenshot-final.png`. This validates static scenario rendering, not an experiment trace or model-generated log. Do not open arbitrary experiment logs: the UI unpickles logs and renders scenario HTML as trusted.
- [ ] Check the data-science UI and/or live user-interaction UI if dependencies and safe fixture permit; not included in the static standard-UI check above.
- [x] Documented risk: Flask log server requires `UI_SERVER_AUTH_TOKEN`; Streamlit scenario description is rendered as HTML and logs must be trusted. The UI is a log/interaction view, not a complete experiment-control dashboard. The trusted static UI check above passed; arbitrary experiment-log rendering remains intentionally untested.

### 8. OpenCode backend acceptance — PARTIAL
- [x] Live dynamic `APIBackend()` completion verified; 40,150-byte synthetic prompt succeeded via authenticated local service; focused backend tests pass.
- [x] Provider/model selectable without an API key stored by RD-Agent. Configured `auto` aliases to `github-copilot/gpt-4.1`; it is not Copilot's API-side Auto routing.
- [x] Authenticated loopback transport explicitly bypasses environment proxies (even with `HTTP_PROXY` set and `NO_PROXY` empty), and redirects are disabled; mocks verify handler configuration.
- [x] Mock-tested missing service registration, HTTP 401, timeout, malformed JSON, provider rate-limit, and missing/non-string response text; service password is redacted and exceptions do not chain transport details.
- [x] Documented no hosted embeddings (local deterministic hashed embeddings), no native response schema, and provider may process prompts.

### 9. UI, docs, and release checks — PARTIAL
- [x] Setup/model selection, Docker/local safety and known workflow limits documented in `WINDOWS_OPENCODE.md`, `.opencode/commands/rdagent.md`, and this checklist.
- [x] Focused safe suites and selected Ruff rules passed; latest bounded suite: 175 passed, 2 skipped, 5 Pydantic warnings, 3 subtests in 34.36s. `git diff --check` passed (Git emitted only the existing LF→CRLF notice for `rdagent/oai/backend/__init__.py`).
- [x] `.env` and generated `git_ignore_folder` artifacts are ignored; `git status` shows only intentional source/docs/tests plus those ignored items. No commit has been made.
- [x] Final source-diff review and secret/artifact scan: no credential-shaped token/private-key matches; `.env`, data, logs, caches, and generated outputs are ignored; only machine-local path literal is an assertion that discovery does not embed `C:\Users\HP`. No commit made. OpenCode install discovery does not embed this machine's user path; the Docker data junction and disk note are intentionally machine-specific operational documentation.

## Current evidence and limits

- The upstream README describes RD-Agent as Linux-only; this is a local Windows-host port, not official upstream Windows support. Main and audited non-CLI entrypoint inventory is complete; plugin-by-plugin runtime/test coverage is not.
- A bounded synthetic data-science attempt validated proposal/code generation, Docker execution, evaluation and output artifacts, but the loop stopped at its configured step-count criterion at 40% (`step_name=running`) after CoSTEER logged a fallback and `recover_ws_ckp failed due to one workspace is recovered twice.` This is not a complete RD-Agent workflow. The earlier ARF attempt is clinical; do not rerun.
- Latest bounded suite after the final LocalEnv host-symlink and multi-volume cleanup fixes: 175 passed, 2 skipped (POSIX-only integration checks on Windows), 5 Pydantic deprecation warnings, 3 subtests in 34.36s; no external API or Docker execution. Broad test run remains not green: 347 failed, 47 passed, 8 warnings, 2 errors, 12 subtests in 2127.18s; no pytest/JUnit report was preserved, so individual failures cannot now be assigned reliably.
- UI was visually checked with a trusted, signed static scenario fixture; real/model-generated experiment-log rendering was not checked because the viewer unpickles data and renders scenario HTML as trusted. `fin_quant` is left unsupported because its validator hardcodes local Qlib initialization, SH600000 and a historical date range. Docker Desktop was running for the synthetic DS attempt but is currently stopped; `local_ds:latest` was verified at run time with pandas 2.3.3, sklearn 1.6.1, coverage 7.17.0 and strace. Storage was observed at C: 85.3 GiB free and D: 20.3 GiB free; Docker data resides on D: behind the existing junction.
- Progress is updated phase-by-phase. Close an item only with a recorded command/result or explicit reason it cannot safely be done.
