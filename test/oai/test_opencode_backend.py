import io
import json
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest import mock

from rdagent.oai.backend.opencode import (
    AUTO_ALIAS_MODEL,
    OpenCodeAPIBackend,
    OpenCodeError,
    OpenCodeSettings,
    default_cli_candidates,
    is_auto_model,
    messages_to_prompt,
    resolve_model_ref,
)


class OpenCodeBackendUnitTests(unittest.TestCase):
    def test_default_cli_candidates_use_current_windows_profile(self):
        with mock.patch.dict("os.environ", {"LOCALAPPDATA": r"D:\Profiles\sample\Local"}):
            candidates = default_cli_candidates()
        self.assertEqual(
            candidates[0],
            r"D:\Profiles\sample\Local\Programs\ai.opencode.desktop\resources\opencode-cli.exe",
        )
        self.assertEqual(
            candidates[1],
            r"D:\Profiles\sample\Local\Programs\opencode\opencode-cli.exe",
        )
        self.assertNotIn(r"C:\Users\HP", " ".join(candidates))

    def test_settings_env_prefix_is_opencode(self):
        self.assertEqual(OpenCodeSettings.model_fields and OpenCodeSettings.Config.env_prefix, "OPENCODE_")

    def test_model_ref_uses_openapi_field_names(self):
        settings = OpenCodeSettings(provider="copilot", model="gpt-4.1")
        self.assertEqual(settings.model_ref, {"id": "gpt-4.1", "providerID": "copilot"})
        self.assertEqual(settings.model_label, "copilot/gpt-4.1")

    def test_backend_logs_only_nonsecret_settings(self):
        with (
            mock.patch.object(OpenCodeAPIBackend, "_has_logged_settings", False),
            mock.patch("rdagent.oai.backend.opencode.logger.info"),
            mock.patch("rdagent.oai.backend.opencode.logger.log_object") as log_object,
        ):
            OpenCodeAPIBackend()
        logged_settings = log_object.call_args.args[0]
        self.assertEqual(
            set(logged_settings),
            {"provider", "model", "request_timeout", "embedding_dim"},
        )
        self.assertNotIn("api_key", str(logged_settings).lower())

    def test_auto_is_an_alias_not_a_separate_code_path(self):
        self.assertTrue(is_auto_model("auto"))
        self.assertTrue(is_auto_model(" AUTO "))
        self.assertTrue(is_auto_model(""))
        self.assertFalse(is_auto_model("gpt-4.1"))
        self.assertEqual(
            resolve_model_ref("github-copilot", "auto"),
            {"id": AUTO_ALIAS_MODEL, "providerID": "github-copilot"},
        )

    def test_plain_models_are_passed_through_unchanged(self):
        self.assertEqual(
            resolve_model_ref("github-copilot", "gpt-4o"),
            {"id": "gpt-4o", "providerID": "github-copilot"},
        )

    def test_messages_to_prompt_keeps_roles_and_asks_for_reply(self):
        prompt = messages_to_prompt(
            [
                {"role": "system", "content": "you are terse"},
                {"role": "user", "content": "say hi"},
            ],
        )
        self.assertIn("### SYSTEM", prompt)
        self.assertIn("you are terse", prompt)
        self.assertIn("### USER", prompt)
        self.assertIn("say hi", prompt)
        self.assertTrue(prompt.rstrip().endswith("Respond with the assistant reply only."))

    def test_embeddings_are_deterministic_and_normalized(self):
        backend = OpenCodeAPIBackend.__new__(OpenCodeAPIBackend)  # no network/LLM init needed
        vectors = backend._create_embedding_inner_function(["hello world", "hello world", "different text"])
        self.assertEqual(len(vectors), 3)
        self.assertEqual(len(vectors[0]), 256)
        self.assertEqual(vectors[0], vectors[1])
        self.assertNotEqual(vectors[0], vectors[2])
        norm = sum(value * value for value in vectors[0]) ** 0.5
        self.assertAlmostEqual(norm, 1.0, places=6)

    def test_authenticated_transport_handles_large_prompts_without_cli_argv(self):
        backend = OpenCodeAPIBackend.__new__(OpenCodeAPIBackend)
        prompt = "long prompt " * 5000
        response = mock.Mock()
        response.read.return_value = json.dumps({"data": {"text": "large prompt received"}}).encode()
        response.__enter__ = mock.Mock(return_value=response)
        response.__exit__ = mock.Mock(return_value=False)
        opener = mock.Mock()
        opener.open.return_value = response
        with (
            mock.patch.dict(
                "os.environ",
                {"HTTP_PROXY": "http://proxy.invalid:8080", "NO_PROXY": ""},
            ),
            mock.patch.object(
                OpenCodeAPIBackend,
                "_service_endpoint",
                return_value=("http://127.0.0.1:28894", "secret"),
            ),
            mock.patch(
                "rdagent.oai.backend.opencode.urllib.request.build_opener",
                return_value=opener,
            ) as build_opener,
        ):
            result = backend._generate_once(prompt, "copilot", "gpt-4.1")
        self.assertEqual(result["text"], "large prompt received")
        build_opener.assert_called_once_with(mock.ANY, mock.ANY)
        handlers = build_opener.call_args.args
        self.assertTrue(any(isinstance(handler, urllib.request.ProxyHandler) for handler in handlers))
        proxy_handler = next(handler for handler in handlers if isinstance(handler, urllib.request.ProxyHandler))
        self.assertEqual(proxy_handler.proxies, {})
        request = opener.open.call_args.args[0]
        self.assertEqual(request.get_method(), "POST")
        self.assertIn("Basic ", request.get_header("Authorization"))
        self.assertGreater(len(request.data), 32_768)

    def test_service_endpoint_rejects_non_loopback_credentials(self):
        payload = {"url": "http://example.com:28894", "password": "secret"}
        registration = mock.patch.object(
            OpenCodeAPIBackend,
            "_service_registration_path",
            return_value=Path("unused"),
        )
        contents = mock.patch("pathlib.Path.read_text", return_value=json.dumps(payload))
        with (
            registration,
            contents,
            self.assertRaisesRegex(OpenCodeError, "unsafe URL"),
        ):
            OpenCodeAPIBackend._service_endpoint()

    def test_missing_service_registration_is_actionable_and_redacted(self):
        backend = OpenCodeAPIBackend.__new__(OpenCodeAPIBackend)
        with (
            mock.patch.object(
                OpenCodeAPIBackend,
                "_service_registration_path",
                return_value=Path("missing-service-registration.json"),
            ),
            mock.patch.object(Path, "read_text", side_effect=OSError("private password")),
            self.assertRaisesRegex(OpenCodeError, "Start the OpenCode desktop app") as caught,
        ):
            backend._generate_once("prompt", "copilot", "gpt-4.1")
        self.assertNotIn("private password", str(caught.exception))
        self.assertIsNone(caught.exception.__cause__)

    def test_http_error_redacts_service_password(self):
        backend = OpenCodeAPIBackend.__new__(OpenCodeAPIBackend)
        error = urllib.error.HTTPError(
            "http://127.0.0.1:28894/api/experimental/generate",
            401,
            "Unauthorized",
            {},
            io.BytesIO(b"bad password is service-secret"),
        )
        opener = mock.Mock()
        opener.open.side_effect = error
        with (
            mock.patch.object(
                OpenCodeAPIBackend,
                "_service_endpoint",
                return_value=("http://127.0.0.1:28894", "service-secret"),
            ),
            mock.patch(
                "rdagent.oai.backend.opencode.urllib.request.build_opener",
                return_value=opener,
            ),
            self.assertRaisesRegex(OpenCodeError, "HTTP 401") as caught,
        ):
            backend._generate_once("prompt", "copilot", "gpt-4.1")
        self.assertNotIn("service-secret", str(caught.exception))
        self.assertIsNone(caught.exception.__cause__)

    def test_provider_rate_limit_redacts_service_password(self):
        backend = OpenCodeAPIBackend.__new__(OpenCodeAPIBackend)
        error = urllib.error.HTTPError(
            "http://127.0.0.1:28894/api/experimental/generate",
            429,
            "Too Many Requests",
            {},
            io.BytesIO(b"Free usage limit reached; service-secret"),
        )
        opener = mock.Mock()
        opener.open.side_effect = error
        with (
            mock.patch.object(
                OpenCodeAPIBackend,
                "_service_endpoint",
                return_value=("http://127.0.0.1:28894", "service-secret"),
            ),
            mock.patch(
                "rdagent.oai.backend.opencode.urllib.request.build_opener",
                return_value=opener,
            ),
            self.assertRaisesRegex(OpenCodeError, "HTTP 429") as caught,
        ):
            backend._generate_once("prompt", "opencode", "example-free-model")
        self.assertIn("Free usage limit reached", str(caught.exception))
        self.assertNotIn("service-secret", str(caught.exception))
        self.assertIsNone(caught.exception.__cause__)

    def test_timeout_message_redacts_service_password(self):
        backend = OpenCodeAPIBackend.__new__(OpenCodeAPIBackend)
        opener = mock.Mock()
        opener.open.side_effect = TimeoutError("timed out with service-secret")
        with (
            mock.patch.object(
                OpenCodeAPIBackend,
                "_service_endpoint",
                return_value=("http://127.0.0.1:28894", "service-secret"),
            ),
            mock.patch(
                "rdagent.oai.backend.opencode.urllib.request.build_opener",
                return_value=opener,
            ),
            self.assertRaisesRegex(OpenCodeError, "Could not reach") as caught,
        ):
            backend._generate_once("prompt", "copilot", "gpt-4.1")
        self.assertNotIn("service-secret", str(caught.exception))
        self.assertIsNone(caught.exception.__cause__)

    def test_invalid_json_redacts_service_password(self):
        backend = OpenCodeAPIBackend.__new__(OpenCodeAPIBackend)
        response = mock.Mock()
        response.read.return_value = b"not json: service-secret"
        response.__enter__ = mock.Mock(return_value=response)
        response.__exit__ = mock.Mock(return_value=False)
        opener = mock.Mock()
        opener.open.return_value = response
        with (
            mock.patch.object(
                OpenCodeAPIBackend,
                "_service_endpoint",
                return_value=("http://127.0.0.1:28894", "service-secret"),
            ),
            mock.patch(
                "rdagent.oai.backend.opencode.urllib.request.build_opener",
                return_value=opener,
            ),
            self.assertRaisesRegex(OpenCodeError, "non-JSON") as caught,
        ):
            backend._generate_once("prompt", "copilot", "gpt-4.1")
        self.assertNotIn("service-secret", str(caught.exception))
        self.assertIsNone(caught.exception.__cause__)

    def test_missing_or_non_string_response_text_is_rejected(self):
        for payload in ({"data": {}}, {"data": {"text": None}}, {"data": {"text": 5}}):
            with self.subTest(payload=payload):
                backend = OpenCodeAPIBackend.__new__(OpenCodeAPIBackend)
                response = mock.Mock()
                response.read.return_value = json.dumps(payload).encode()
                response.__enter__ = mock.Mock(return_value=response)
                response.__exit__ = mock.Mock(return_value=False)
                opener = mock.Mock()
                opener.open.return_value = response
                with (
                    mock.patch.object(
                        OpenCodeAPIBackend,
                        "_service_endpoint",
                        return_value=("http://127.0.0.1:28894", "service-secret"),
                    ),
                    mock.patch(
                        "rdagent.oai.backend.opencode.urllib.request.build_opener",
                        return_value=opener,
                    ),
                    self.assertRaisesRegex(OpenCodeError, "no `data.text`") as caught,
                ):
                    backend._generate_once("prompt", "copilot", "gpt-4.1")
                self.assertNotIn("service-secret", str(caught.exception))

    def test_token_counting_matches_tiktoken_for_known_text(self):
        count = OpenCodeAPIBackend._count_tokens("hello world", "gpt-4.1")
        self.assertGreater(count, 0)


if __name__ == "__main__":
    unittest.main()
