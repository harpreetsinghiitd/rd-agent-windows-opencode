r"""OpenCode-backed :class:`~rdagent.oai.backend.base.APIBackend` implementation.

Why this exists
---------------
RD-Agent normally talks to an LLM vendor directly (OpenAI, Anthropic, ...) and
therefore needs an API key per vendor. When RD-Agent runs inside OpenCode, an
already authenticated OpenCode desktop app is sitting on the same machine and
can generate text with any provider/model the user has configured there.

This backend routes every RD-Agent LLM call through that local OpenCode server:

    RD-Agent  --(authenticated loopback HTTP)-->  OpenCode server  -->  provider/model

so no extra API key is required and the provider/model is selectable:

    OPENCODE_PROVIDER=copilot
    OPENCODE_MODEL=gpt-4.1
    LLM_SETTINGS.backend=rdagent.oai.backend.OpenCodeAPIBackend

Notes / limitations (be honest about them)
------------------------------------------
* The OpenCode HTTP endpoint requires the desktop app's local service password.
  This backend reads the per-user ``service.json`` registration and sends Basic auth
  only to a loopback address; no vendor API key is needed and secrets are not logged.
* The public generation endpoint is *text in / text out*. There is no
  ``response_format`` support and no ``finish_reason``; we therefore report
  ``supports_response_schema() == False`` and infer ``"length"`` truncation from
  the token budget so that ``APIBackend``'s auto-continue logic still works.
* OpenCode exposes no embedding endpoint, so embeddings are computed locally
  (deterministic hashed bag-of-words). They are good enough for the cosine
  similarity / vector-store call sites inside RD-Agent, but they are *not*
  semantic embeddings from a trained model.
"""

from __future__ import annotations

import base64
import hashlib
import ipaddress
import json
import math
import os
import re
import shutil
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
from functools import lru_cache
from pathlib import Path
from typing import TYPE_CHECKING, Any

import tiktoken
from rdagent.log import LogColors
from rdagent.log import rdagent_logger as logger
from rdagent.oai.backend.base import APIBackend
from rdagent.oai.llm_conf import LLMSettings

if TYPE_CHECKING:
    from pydantic import BaseModel

def default_cli_candidates() -> tuple[str, ...]:
    """Return conventional CLI locations without assuming a specific Windows user."""
    local_app_data = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
    return (
        str(local_app_data / "Programs" / "ai.opencode.desktop" / "resources" / "opencode-cli.exe"),
        str(local_app_data / "Programs" / "opencode" / "opencode-cli.exe"),
        "/Applications/OpenCode.app/Contents/Resources/opencode-cli",
        "/usr/local/bin/opencode-cli",
    )


class OpenCodeSettings(LLMSettings):

    class Config:
        env_prefix = "OPENCODE_"

    provider: str = "copilot"
    """OpenCode provider id, see ``python -m rdagent.oai.backend.opencode list-models``."""

    model: str = "gpt-4.1"
    """Model id inside ``provider``."""

    cli_path: str = ""
    """Path to ``opencode-cli``. Empty = auto-detect (PATH, then known install dirs)."""

    request_timeout: int = 900
    """Seconds before a single generation call is abandoned."""

    embedding_dim: int = 256
    """Dimensionality of the locally computed embeddings."""

    min_output_tokens: int = 16
    """Used to detect truncation when the endpoint does not report ``finish_reason``."""

    @property
    def model_ref(self) -> dict[str, str]:
        """The ``Model.Ref`` object the OpenCode API expects (field is ``id``, not ``modelID``)."""
        return resolve_model_ref(self.provider, self.model)

    @property
    def model_label(self) -> str:
        ref = resolve_model_ref(self.provider, self.model)
        return f"{ref['providerID']}/{ref['id']}"


OPENCODE_SETTINGS = OpenCodeSettings()
ACC_COST = 0.0


class OpenCodeError(RuntimeError):
    """Raised when the OpenCode CLI/server cannot serve a request."""


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Keep authenticated service calls on the validated loopback endpoint."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def resolve_cli_path(explicit: str | None = None) -> str:
    """Locate the ``opencode-cli`` binary."""
    for candidate in (explicit, os.environ.get("OPENCODE_CLI"), OPENCODE_SETTINGS.cli_path):
        if candidate:
            if Path(candidate).exists():
                return candidate
            msg = f"OPENCODE_CLI points to a missing file: {candidate}"
            raise OpenCodeError(msg)

    found = shutil.which("opencode-cli")
    if found:
        return found

    for candidate in default_cli_candidates():
        if Path(candidate).exists():
            return candidate

    msg = (
        "Could not find `opencode-cli`. Set OPENCODE_CLI to its full path "
        "(Windows: <OpenCode install>\\resources\\opencode-cli.exe)."
    )
    raise OpenCodeError(msg)


def _run_cli(args: list[str], timeout: int) -> str:
    """Run ``opencode-cli`` and return stdout, converting failures into clear errors."""
    cli = resolve_cli_path()
    try:
        proc = subprocess.run(  # noqa: S603 - fixed binary, argument list, no shell
            [cli, *args],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        msg = f"`opencode-cli {' '.join(args[:2])}` timed out after {timeout}s"
        raise OpenCodeError(msg) from exc
    except OSError as exc:  # binary disappeared / not executable
        msg = f"Failed to launch opencode-cli ({cli}): {exc}"
        raise OpenCodeError(msg) from exc

    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip()
        msg = f"opencode-cli exited with {proc.returncode}: {detail}"
        raise OpenCodeError(msg)
    return proc.stdout


@lru_cache(maxsize=1)
def list_models() -> list[dict[str, Any]]:
    """Return the models the local OpenCode server can serve (cached per process)."""
    raw = _run_cli(["api", "get", "/api/model"], timeout=120)
    payload = json.loads(raw)
    data = payload.get("data", payload) if isinstance(payload, dict) else payload
    if isinstance(data, dict):  # e.g. {"data": {"items": [...]}}
        data = data.get("items") or data.get("models") or []
    models: list[dict[str, Any]] = []
    for item in data or []:
        if not isinstance(item, dict):
            continue
        provider = item.get("providerID") or item.get("provider") or item.get("providerId")
        model_id = item.get("id") or item.get("modelID") or item.get("name")
        if provider and model_id:
            models.append({**item, "providerID": provider, "id": model_id})
    return models


@lru_cache(maxsize=256)
def _context_limit(provider: str, model: str) -> int | None:
    for item in list_models():
        if item["providerID"] == provider and item["id"] == model:
            limits = item.get("limit") or item.get("limits") or {}
            for key in ("context", "contextWindow", "context_length"):
                value = limits.get(key) if isinstance(limits, dict) else None
                if isinstance(value, int) and value > 0:
                    return value
    return None


AUTO_ALIAS_MODEL = "gpt-4.1"
"""Concrete Copilot model used when ``auto`` is selected.

Copilot's "Auto" is a routing mode inside the Copilot apps, not a model id the API
accepts: posting ``{"id": "auto", "providerID": "github-copilot"}`` returns
``ProviderRequestError: The requested model is not supported`` (HTTP 502). The alias is
resolved once here so ``auto`` behaves exactly like any other model selection.
"""


def is_auto_model(model: str | None = None) -> bool:
    """Whether the configured model means "let Copilot pick" (``auto`` / empty)."""
    value = (model if model is not None else OPENCODE_SETTINGS.model).strip().lower()
    return value in {"", "auto"}


def resolve_model_ref(provider: str | None = None, model: str | None = None) -> dict[str, str]:
    """The ``Model.Ref`` object to send, with the ``auto`` alias resolved."""
    prov = (provider or OPENCODE_SETTINGS.provider).strip()
    mod = (model if model is not None else OPENCODE_SETTINGS.model).strip()
    if is_auto_model(mod):
        mod = AUTO_ALIAS_MODEL
    return {"id": mod, "providerID": prov}


def _message_to_text(message: dict[str, Any]) -> str:
    role = str(message.get("role", "user")).upper()
    content = message.get("content", "")
    if isinstance(content, list):  # OpenAI-style multimodal parts
        parts: list[str] = []
        for part in content:
            if isinstance(part, dict):
                text = part.get("text") or part.get("content")
                if text:
                    parts.append(str(text))
            elif part:
                parts.append(str(part))
        content = "\n".join(parts)
    return f"### {role}\n{content}".rstrip()


def messages_to_prompt(messages: list[dict[str, Any]]) -> str:
    """Flatten a chat transcript into the single prompt the OpenCode endpoint accepts."""
    body = "\n\n".join(_message_to_text(m) for m in messages)
    return f"{body}\n\n### ASSISTANT\nRespond with the assistant reply only."


class OpenCodeAPIBackend(APIBackend):
    """APIBackend that delegates generation to the locally running OpenCode app."""

    _has_logged_settings: bool = False

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        if not OpenCodeAPIBackend._has_logged_settings:
            safe_settings = {
                "provider": OPENCODE_SETTINGS.provider,
                "model": OPENCODE_SETTINGS.model,
                "request_timeout": OPENCODE_SETTINGS.request_timeout,
                "embedding_dim": OPENCODE_SETTINGS.embedding_dim,
            }
            logger.info(f"OpenCode backend configured: {safe_settings}")
            logger.log_object(safe_settings, tag="OPENCODE_SETTINGS")
            OpenCodeAPIBackend._has_logged_settings = True
        super().__init__(*args, **kwargs)

    # ---------------------------------------------------------------- tokens
    @staticmethod
    def _count_tokens(text: str, model: str) -> int:
        if not text:
            return 0
        try:
            encoder = tiktoken.encoding_for_model(model)
        except KeyError:  # unknown model name -> cl100k_base is a fine approximation
            encoder = tiktoken.get_encoding("cl100k_base")
        return len(encoder.encode(text, disallowed_special=()))

    def _calculate_token_from_messages(self, messages: list[dict[str, Any]]) -> int:
        num_tokens = self._count_tokens(messages_to_prompt(messages), resolve_model_ref()["id"])
        logger.info(f"{LogColors.CYAN}Token count: {LogColors.END} {num_tokens}", tag="debug_opencode_token")
        return num_tokens

    # ------------------------------------------------------------ embeddings
    def _create_embedding_inner_function(self, input_content_list: list[str]) -> list[list[float]]:
        """Deterministic local embeddings (OpenCode has no embedding endpoint).

        Hashed bag-of-words projected into ``embedding_dim`` dimensions and
        L2-normalised, so cosine similarity behaves sanely and dimensionality is
        stable across calls.
        """
        dim = OPENCODE_SETTINGS.embedding_dim
        results: list[list[float]] = []
        for content in input_content_list:
            vector = [0.0] * dim
            tokens = re.findall(r"[a-z0-9_]+", (content or "").lower())
            if not tokens:
                results.append(vector)
                continue
            for token in tokens:
                digest = hashlib.blake2b(token.encode("utf-8"), digest_size=8).digest()
                index = int.from_bytes(digest[:4], "little") % dim
                sign = 1.0 if digest[4] & 1 else -1.0
                vector[index] += sign
            norm = math.sqrt(sum(v * v for v in vector))
            if norm > 0:
                vector = [v / norm for v in vector]
            results.append(vector)
        if OPENCODE_SETTINGS.log_llm_chat_content:
            logger.info(
                f"{LogColors.MAGENTA}Created {len(results)} local OpenCode embeddings{LogColors.END} "
                f"(dim={dim}), tag=debug_opencode_emb",
            )
        return results

    # ----------------------------------------------------------------- chat
    def supports_response_schema(self) -> bool:
        """The OpenCode generation endpoint is plain text; schemas must be prompted for."""
        return False

    @property
    def chat_token_limit(self) -> int:
        """Input budget derived from the model's context window when it is known."""
        model_ref = resolve_model_ref()
        context = _context_limit(model_ref["providerID"], model_ref["id"])
        if not context:
            return super().chat_token_limit
        reserved_output = OPENCODE_SETTINGS.chat_max_tokens or max(OPENCODE_SETTINGS.min_output_tokens, context // 4)
        return max(1024, context - reserved_output)

    @staticmethod
    def _service_registration_path() -> Path:
        """Return OpenCode's documented per-user service registration path."""
        state_dir = Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local" / "state"))
        return state_dir / "opencode" / "service.json"

    @staticmethod
    def _service_endpoint() -> tuple[str, str]:
        """Read the active OpenCode service URL and private password without logging secrets."""
        registration = OpenCodeAPIBackend._service_registration_path()
        try:
            info = json.loads(registration.read_text(encoding="utf-8"))
            url = str(info["url"]).rstrip("/")
            password = str(info["password"])
        except (OSError, ValueError, KeyError, TypeError):
            msg = "Could not read OpenCode's service registration. Start the OpenCode desktop app."
            raise OpenCodeError(msg) from None

        if not password:
            msg = "OpenCode service registration has no authentication password"
            raise OpenCodeError(msg)
        parsed = urllib.parse.urlparse(url)
        hostname = parsed.hostname or ""
        try:
            is_loopback = ipaddress.ip_address(hostname).is_loopback
        except ValueError:
            is_loopback = hostname.lower() == "localhost"
        if (
            parsed.scheme not in {"http", "https"}
            or not is_loopback
            or parsed.username
            or parsed.password
            or parsed.path not in {"", "/"}
            or parsed.query
            or parsed.fragment
        ):
            msg = "Refusing to send OpenCode service credentials to an unsafe URL"
            raise OpenCodeError(msg)
        try:
            parsed.port  # Validate malformed port syntax before making a request.
        except ValueError as exc:
            msg = "OpenCode service registration has an invalid URL port"
            raise OpenCodeError(msg) from exc
        return url, password

    @staticmethod
    def _redact_secret(value: str, secret: str) -> str:
        """Remove the local service password from untrusted transport diagnostics."""
        return value.replace(secret, "[REDACTED]") if secret else value

    def _generate_once(self, prompt: str, provider: str, model: str) -> dict[str, Any]:
        """POST one generation request through the authenticated local OpenCode service."""
        url, password = self._service_endpoint()
        endpoint = urllib.parse.urljoin(url + "/", "api/experimental/generate")
        request_body = json.dumps(
            {"prompt": prompt, "model": {"id": model, "providerID": provider}},
            ensure_ascii=False,
        ).encode()
        auth = base64.b64encode(f"opencode:{password}".encode()).decode("ascii")
        request = urllib.request.Request(  # noqa: S310 - endpoint is validated as loopback HTTP(S)
            endpoint,
            data=request_body,
            headers={"Authorization": f"Basic {auth}", "Content-Type": "application/json"},
            method="POST",
        )
        # This is a local authenticated service; environment proxies must not see
        # either the service password or prompt, even if NO_PROXY is misconfigured.
        opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({}),
            _NoRedirectHandler,
        )
        try:
            # The URL was validated as loopback above; redirects are disabled so
            # authentication cannot be forwarded to a different host.
            with opener.open(  # noqa: S310 - validated loopback URL; redirects are disabled
                request,
                timeout=OPENCODE_SETTINGS.request_timeout,
            ) as response:
                raw = response.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")[:500]
            detail = self._redact_secret(detail, password)
            msg = f"OpenCode generation returned HTTP {exc.code}: {detail}"
            raise OpenCodeError(msg) from None
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            detail = self._redact_secret(str(exc), password)
            msg = f"Could not reach the authenticated OpenCode service at {url}: {detail}"
            raise OpenCodeError(msg) from None
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            detail = self._redact_secret(raw[:500], password)
            msg = f"OpenCode returned non-JSON output: {detail}"
            raise OpenCodeError(msg) from None
        data = payload.get("data", payload) if isinstance(payload, dict) else {}
        if not isinstance(data, dict) or not isinstance(data.get("text"), str):
            detail = self._redact_secret(str(payload)[:500], password)
            msg = f"OpenCode response has no `data.text`: {detail}"
            raise OpenCodeError(msg)
        return data

    def _create_chat_completion_inner_function(  # type: ignore[no-untyped-def]
        self,
        messages: list[dict[str, Any]],
        response_format: dict | type[BaseModel] | None = None,
        *args: Any,
        **kwargs: Any,
    ) -> tuple[str, str | None]:
        model_ref = resolve_model_ref()
        active_label = f"{model_ref['providerID']}/{model_ref['id']}"
        if response_format is not None and OPENCODE_SETTINGS.log_llm_chat_content:
            logger.info(
                f"{LogColors.YELLOW}OpenCode backend ignores response_format "
                f"({active_label}); the prompt must describe the expected JSON.{LogColors.END}",
                tag="llm_messages",
            )

        if OPENCODE_SETTINGS.log_llm_chat_content:
            logger.info(self._build_log_messages(messages), tag="llm_messages")

        prompt = messages_to_prompt(messages)
        data = self._generate_once(prompt, model_ref["providerID"], model_ref["id"])
        content = str(data["text"])

        tokens = data.get("tokens") or {}
        prompt_tokens = int(tokens.get("input") or self._count_tokens(prompt, model_ref["id"]))
        completion_tokens = int(tokens.get("output") or self._count_tokens(content, model_ref["id"]))

        # The endpoint has no finish_reason; a reply that fills the configured output
        # budget is almost certainly truncated, which lets base's auto-continue kick in.
        finish_reason: str | None = "stop"
        max_output = OPENCODE_SETTINGS.chat_max_tokens
        if max_output and completion_tokens >= max_output:
            finish_reason = "length"

        cost = float(data.get("cost") or 0.0)
        global ACC_COST
        ACC_COST += cost
        if OPENCODE_SETTINGS.log_llm_chat_content:
            logger.info(
                f"{LogColors.BLUE}assistant:{LogColors.END} ({active_label})\n{content}",
                tag="llm_messages",
            )
            logger.info(
                f"Current Cost: ${cost:.10f}; Accumulated Cost: ${ACC_COST:.10f}; {finish_reason=}",
            )
        logger.log_object(
            {
                "model": active_label,
                "prompt_tokens": prompt_tokens,
                "completion_tokens": completion_tokens,
                "cost": cost,
                "accumulated_cost": ACC_COST,
            },
            tag="token_cost",
        )
        return content, finish_reason


def _cli_main(argv: list[str] | None = None) -> int:
    """``python -m rdagent.oai.backend.opencode [list-models|current]``."""
    args = list(sys.argv[1:] if argv is None else argv)
    command = args[0] if args else "list-models"
    if command == "current":
        ref = resolve_model_ref()
        print(f"provider={OPENCODE_SETTINGS.provider} model={OPENCODE_SETTINGS.model}")
        print(f"resolved={ref['providerID']}/{ref['id']}")
        print(f"cli={resolve_cli_path()}")
        return 0
    if command != "list-models":
        print(_cli_main.__doc__)
        return 2

    chat_models = [m for m in list_models() if (m.get("type") in (None, "chat"))]
    chat_models.sort(key=lambda m: (str(m["providerID"]), str(m["id"])))
    print(f"{'PROVIDER':<22}{'MODEL':<44}CONTEXT")
    for item in chat_models:
        context = _context_limit(str(item["providerID"]), str(item["id"])) or "-"
        print(f"{item['providerID']:<22}{item['id']:<44}{context}")
    print(f"\n{len(chat_models)} models. Select with OPENCODE_PROVIDER / OPENCODE_MODEL.")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(_cli_main())
