import io
import json
import sys
import threading
import time
import unittest
import urllib.error
from email.message import Message
from pathlib import Path
from unittest import mock


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from model_council import (  # noqa: E402
    AnalystOpinion,
    AnalystProviderError,
    CouncilConfig,
    InvalidAnalystResponse,
    ModelCouncil,
    OpenAICompatibleAnalyst,
    build_from_env,
    parse_analyst_json,
    validate_analyst_output,
)


def opinion(
    verdict="support",
    confidence=0.75,
    summary="The supplied evidence supports the research scenario.",
    evidence_ids=None,
    risks=None,
    missing_data=None,
):
    return {
        "verdict": verdict,
        "confidence": confidence,
        "summary": summary,
        "evidence_ids": ["price:1"] if evidence_ids is None else evidence_ids,
        "risks": ["Recent volume is incomplete."] if risks is None else risks,
        "missing_data": [] if missing_data is None else missing_data,
    }


def enabled_config(**overrides):
    values = {
        "enabled": True,
        "glm_api_key": "glm-test-secret",
        "deepseek_api_key": "deepseek-test-secret",
        "request_timeout_seconds": 0.5,
        "overall_timeout_seconds": 0.5,
        "cache_ttl_seconds": 900.0,
        "cache_max_entries": 16,
    }
    values.update(overrides)
    return CouncilConfig(**values)


class StaticAdapter:
    def __init__(self, result=None, *, error=None, barrier=None, delay=0.0):
        self.result = opinion() if result is None else result
        self.error = error
        self.barrier = barrier
        self.delay = delay
        self.calls = 0
        self.seen_evidence = []

    def analyze(self, evidence, *, allowed_evidence_ids):
        self.calls += 1
        self.seen_evidence.append((evidence, tuple(allowed_evidence_ids)))
        if self.barrier is not None:
            self.barrier.wait(timeout=1.0)
        if self.delay:
            time.sleep(self.delay)
        if self.error is not None:
            raise self.error
        return self.result


def provider_adapter(provider="glm"):
    return OpenAICompatibleAnalyst(
        provider=provider,
        role=(
            "fundamental_evidence_analyst"
            if provider == "glm"
            else "risk_challenge_analyst"
        ),
        api_key="adapter-secret",
        base_url="https://api.example.test/v4",
        model=f"{provider}-test",
        timeout_seconds=1,
        max_output_tokens=200,
    )


def http_error(status, body, *, retry_after=None):
    headers = Message()
    if retry_after is not None:
        headers["Retry-After"] = str(retry_after)
    return urllib.error.HTTPError(
        "https://api.example.test/v4/chat/completions",
        status,
        "provider failure",
        headers,
        io.BytesIO(json.dumps(body).encode("utf-8")),
    )


class ModelCouncilTests(unittest.TestCase):
    def test_build_from_env_is_a_disabled_safe_default(self):
        council = build_from_env({})
        self.assertIsInstance(council, ModelCouncil)
        self.assertFalse(council.enabled)

    def test_each_provider_is_independently_opted_in(self):
        blank = CouncilConfig.from_env({})
        self.assertFalse(blank.effective_enabled)
        self.assertEqual(blank.glm_api_key, "")
        self.assertEqual(blank.deepseek_api_key, "")

        glm = StaticAdapter()
        deepseek = StaticAdapter()
        config = CouncilConfig(
            enabled=True,
            glm_api_key="only-one-key",
            deepseek_api_key="",
        )
        council = ModelCouncil(config, glm_adapter=glm, deepseek_adapter=deepseek)
        report = council.review({"symbol": "FPT"}, evidence_ids=("price:1",))

        self.assertTrue(council.enabled)
        self.assertEqual(council.configured_providers(), ("glm",))
        self.assertEqual(report.status, "partial")
        self.assertEqual([review.status for review in report.reviews], ["ok", "disabled"])
        self.assertEqual(glm.calls, 1)
        self.assertEqual(deepseek.calls, 0)

        keys_are_the_opt_in = CouncilConfig(
            glm_api_key="glm-key",
            deepseek_api_key="deepseek-key",
        )
        self.assertTrue(keys_are_the_opt_in.effective_enabled)

        shared_cooldown = CouncilConfig.from_env(
            {"MODEL_COUNCIL_ERROR_COOLDOWN": "321"}
        )
        self.assertEqual(shared_cooldown.rate_limit_cooldown_seconds, 321)
        self.assertEqual(shared_cooldown.configuration_cooldown_seconds, 321)
        self.assertEqual(shared_cooldown.transient_cooldown_seconds, 321)

    def test_secret_values_are_absent_from_config_and_adapter_repr(self):
        config = enabled_config()
        rendered = repr(config)
        self.assertNotIn("glm-test-secret", rendered)
        self.assertNotIn("deepseek-test-secret", rendered)

        adapter = OpenAICompatibleAnalyst(
            provider="glm",
            role="evidence_analyst",
            api_key="never-render-this-key",
            base_url="https://example.test/v4",
            model="test-model",
            timeout_seconds=1,
            max_output_tokens=200,
            transport=lambda request, timeout: b"{}",
        )
        self.assertNotIn("never-render-this-key", repr(adapter))

    def test_http_errors_are_safely_classified_without_raw_body_leak(self):
        cases = [
            (
                429,
                {"error": {"code": 1113, "message": "Insufficient balance or no resource package"}},
                "billing",
            ),
            (402, {"error": {"type": "invalid_request_error", "message": "Insufficient Balance"}}, "billing"),
            (429, {"error": {"code": "rate_limit_exceeded", "message": "slow down"}}, "rate_limit"),
            (401, {"error": {"code": "authentication", "message": "bad key"}}, "auth"),
            (404, {"error": {"code": "model_not_found", "message": "unknown"}}, "model"),
            (400, {"error": {"code": "invalid_request", "message": "bad input"}}, "request"),
            (503, {"error": {"code": "service_unavailable", "message": "retry"}}, "server"),
        ]
        for status, body, expected_kind in cases:
            body["error"]["message"] += " raw-secret-must-not-leak"
            with self.subTest(status=status, expected_kind=expected_kind):
                adapter = provider_adapter()
                failure = http_error(status, body, retry_after=45)
                with mock.patch(
                    "model_council.urllib.request.urlopen",
                    side_effect=failure,
                ):
                    with self.assertRaises(AnalystProviderError) as raised:
                        adapter.analyze(
                            {"symbol": "FPT"},
                            allowed_evidence_ids=("price:1",),
                        )
                error = raised.exception
                self.assertEqual(error.kind, expected_kind)
                self.assertEqual(error.http_status, status)
                self.assertEqual(error.retry_after_seconds, 45)
                self.assertIsNone(error.__context__)
                rendered = f"{error!s} {error!r} {vars(error)!r}"
                self.assertNotIn("raw-secret-must-not-leak", rendered)
                self.assertNotIn("adapter-secret", rendered)

    def test_transport_failure_is_classified_without_retaining_reason(self):
        adapter = provider_adapter()
        with mock.patch(
            "model_council.urllib.request.urlopen",
            side_effect=urllib.error.URLError("raw-secret-must-not-leak"),
        ):
            with self.assertRaises(AnalystProviderError) as raised:
                adapter.analyze({"symbol": "FPT"}, allowed_evidence_ids=("price:1",))

        self.assertEqual(raised.exception.kind, "transport")
        self.assertIsNone(raised.exception.__context__)
        self.assertNotIn("raw-secret-must-not-leak", repr(raised.exception))

    def test_valid_output_is_normalized_to_narrow_contract(self):
        result = validate_analyst_output(
            opinion(summary="  Evidence is consistent.  "),
            allowed_evidence_ids=("price:1",),
        )
        self.assertIsInstance(result, AnalystOpinion)
        self.assertEqual(result.verdict, "support")
        self.assertEqual(result.confidence, 0.75)
        self.assertEqual(result.summary, "Evidence is consistent.")
        self.assertEqual(result.evidence_ids, ("price:1",))

    def test_strict_json_rejects_fences_trailing_text_duplicate_keys_and_nan(self):
        valid_json = json.dumps(opinion())
        invalid_values = [
            f"```json\n{valid_json}\n```",
            valid_json + " trailing",
            valid_json.replace('"verdict": "support"', '"verdict": "support", "verdict": "neutral"'),
            valid_json.replace("0.75", "NaN"),
        ]
        for raw in invalid_values:
            with self.subTest(raw=raw[:35]):
                with self.assertRaises(InvalidAnalystResponse):
                    parse_analyst_json(raw, allowed_evidence_ids=("price:1",))

    def test_validator_rejects_extra_missing_wrong_types_ranges_and_unknown_evidence(self):
        invalid_objects = []

        extra = opinion()
        extra["score_delta"] = 5
        invalid_objects.append(extra)

        missing = opinion()
        missing.pop("risks")
        invalid_objects.append(missing)

        invalid_objects.extend(
            [
                opinion(verdict="buy"),
                opinion(confidence=True),
                opinion(confidence=1.01),
                opinion(evidence_ids=["invented:id"]),
                opinion(verdict="reject", evidence_ids=[]),
                opinion(risks="not-an-array"),
            ]
        )

        for value in invalid_objects:
            with self.subTest(value=value):
                with self.assertRaises(InvalidAnalystResponse):
                    validate_analyst_output(value, allowed_evidence_ids=("price:1",))

    def test_validator_rejects_trade_recommendations(self):
        value = opinion(summary="Khuyến nghị mua cổ phiếu ngay.")
        with self.assertRaises(InvalidAnalystResponse):
            validate_analyst_output(value, allowed_evidence_ids=("price:1",))

    def test_openai_adapter_posts_json_mode_without_tools_and_validates_content(self):
        captured = {}

        def transport(request, timeout):
            captured["url"] = request.full_url
            captured["timeout"] = timeout
            captured["payload"] = json.loads(request.data.decode("utf-8"))
            captured["authorization"] = request.get_header("Authorization")
            envelope = {
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "content": json.dumps(opinion()),
                        }
                    }
                ]
            }
            return json.dumps(envelope).encode("utf-8")

        adapter = OpenAICompatibleAnalyst(
            provider="glm",
            role="fundamental_evidence_analyst",
            api_key="adapter-secret",
            base_url="https://api.example.test/v4",
            model="glm-test",
            timeout_seconds=3.0,
            max_output_tokens=300,
            transport=transport,
        )
        result = adapter.analyze(
            {"symbol": "FPT", "score": 80},
            allowed_evidence_ids=("price:1",),
        )

        self.assertEqual(result.verdict, "support")
        self.assertEqual(captured["url"], "https://api.example.test/v4/chat/completions")
        self.assertEqual(captured["timeout"], 3.0)
        self.assertEqual(captured["authorization"], "Bearer adapter-secret")
        self.assertEqual(captured["payload"]["response_format"], {"type": "json_object"})
        self.assertNotIn("tools", captured["payload"])
        self.assertNotIn("tool_choice", captured["payload"])
        self.assertNotIn("adapter-secret", request_json := json.dumps(captured["payload"]))
        self.assertIn("not a future win probability", request_json)
        self.assertIn("business-fundamentals and valuation analyst", request_json)
        self.assertIn("operating and free cash flow", request_json)

    def test_provider_prompts_enforce_distinct_analyst_roles(self):
        captured = {}

        def transport(request, timeout):
            payload = json.loads(request.data.decode("utf-8"))
            captured["system"] = payload["messages"][0]["content"]
            return json.dumps(
                {
                    "choices": [
                        {
                            "message": {
                                "role": "assistant",
                                "content": json.dumps(opinion()),
                            }
                        }
                    ]
                }
            ).encode("utf-8")

        adapter = OpenAICompatibleAnalyst(
            provider="deepseek",
            role="risk_challenge_analyst",
            api_key="secret",
            base_url="https://api.example.test",
            model="deepseek-test",
            timeout_seconds=1,
            max_output_tokens=200,
            transport=transport,
        )
        adapter.analyze({"symbol": "FPT"}, allowed_evidence_ids=("price:1",))

        self.assertIn("adversarial technical and risk challenger", captured["system"])
        self.assertIn("Wilson bound", captured["system"])
        self.assertIn("Do not duplicate the fundamental valuation review", captured["system"])

    def test_openai_adapter_rejects_provider_tool_calls(self):
        def transport(request, timeout):
            return json.dumps(
                {
                    "choices": [
                        {
                            "message": {
                                "content": json.dumps(opinion()),
                                "tool_calls": [{"id": "forbidden"}],
                            }
                        }
                    ]
                }
            ).encode("utf-8")

        adapter = OpenAICompatibleAnalyst(
            provider="deepseek",
            role="risk_challenge_analyst",
            api_key="secret",
            base_url="https://api.example.test",
            model="deepseek-test",
            timeout_seconds=1,
            max_output_tokens=200,
            transport=transport,
        )
        with self.assertRaises(InvalidAnalystResponse):
            adapter.analyze({"symbol": "FPT"}, allowed_evidence_ids=("price:1",))

    def test_two_analysts_start_in_parallel_and_report_order_is_stable(self):
        barrier = threading.Barrier(2)
        glm = StaticAdapter(opinion(summary="GLM assessment"), barrier=barrier)
        deepseek = StaticAdapter(
            opinion(
                verdict="reject",
                confidence=0.6,
                summary="DeepSeek challenge",
            ),
            barrier=barrier,
        )
        council = ModelCouncil(enabled_config(), glm_adapter=glm, deepseek_adapter=deepseek)

        report = council.review(
            {"symbol": "FPT", "snapshot_id": "snap-1"},
            evidence_ids=("price:1",),
        )

        self.assertEqual(report.status, "complete")
        self.assertEqual([review.provider for review in report.reviews], ["glm", "deepseek"])
        self.assertEqual([review.status for review in report.reviews], ["ok", "ok"])
        self.assertEqual(glm.calls, 1)
        self.assertEqual(deepseek.calls, 1)

    def test_successful_single_provider_report_is_partial_and_cached(self):
        glm = StaticAdapter()
        deepseek = StaticAdapter()
        config = CouncilConfig(
            enabled=True,
            glm_api_key="glm-key",
            deepseek_api_key="",
            cache_ttl_seconds=30,
        )
        council = ModelCouncil(config, glm_adapter=glm, deepseek_adapter=deepseek)

        first = council.review({"symbol": "FPT"}, evidence_ids=("price:1",))
        second = council.review({"symbol": "FPT"}, evidence_ids=("price:1",))

        self.assertEqual(first.status, "partial")
        self.assertEqual([review.status for review in first.reviews], ["ok", "disabled"])
        self.assertTrue(second.cache_hit)
        self.assertEqual(glm.calls, 1)
        self.assertEqual(deepseek.calls, 0)

    def test_provider_circuit_is_independent_and_reopens_after_cooldown(self):
        now = [100.0]
        glm = StaticAdapter(error=AnalystProviderError("billing"))
        deepseek = StaticAdapter(opinion(verdict="neutral", evidence_ids=[]))
        council = ModelCouncil(
            enabled_config(
                cache_ttl_seconds=0,
                configuration_cooldown_seconds=60,
            ),
            glm_adapter=glm,
            deepseek_adapter=deepseek,
            clock=lambda: now[0],
        )

        first = council.review({"symbol": "FPT", "run": 1}, evidence_ids=("price:1",))
        second = council.review({"symbol": "FPT", "run": 2}, evidence_ids=("price:1",))

        self.assertEqual(first.status, "partial")
        self.assertEqual(first.reviews[0].failure_kind, "billing")
        self.assertEqual(first.reviews[0].retry_after_seconds, 60)
        self.assertEqual(second.reviews[0].failure_kind, "billing")
        self.assertEqual(glm.calls, 1)
        self.assertEqual(deepseek.calls, 2)
        self.assertTrue(council.can_attempt())
        statuses = council.provider_statuses()
        self.assertFalse(statuses["glm"]["available"])
        self.assertTrue(statuses["deepseek"]["available"])
        self.assertEqual(statuses["glm"]["failure_kind"], "billing")

        now[0] = 161.0
        council.review({"symbol": "FPT", "run": 3}, evidence_ids=("price:1",))
        self.assertEqual(glm.calls, 2)
        self.assertEqual(deepseek.calls, 3)

    def test_rate_limit_retry_after_extends_provider_cooldown(self):
        now = [10.0]
        glm = StaticAdapter(
            error=AnalystProviderError("rate_limit", retry_after_seconds=75)
        )
        deepseek = StaticAdapter(opinion(verdict="neutral", evidence_ids=[]))
        council = ModelCouncil(
            enabled_config(
                cache_ttl_seconds=0,
                rate_limit_cooldown_seconds=10,
            ),
            glm_adapter=glm,
            deepseek_adapter=deepseek,
            clock=lambda: now[0],
        )

        report = council.review({"symbol": "FPT"}, evidence_ids=("price:1",))

        self.assertEqual(report.reviews[0].failure_kind, "rate_limit")
        self.assertEqual(report.reviews[0].retry_after_seconds, 75)

    def test_timeout_and_failure_fail_closed_to_abstain(self):
        glm = StaticAdapter(error=TimeoutError("simulated"))
        deepseek = StaticAdapter(opinion(verdict="neutral", evidence_ids=[]))
        council = ModelCouncil(enabled_config(), glm_adapter=glm, deepseek_adapter=deepseek)

        report = council.review({"symbol": "FPT"}, evidence_ids=("price:1",))

        self.assertEqual(report.status, "partial")
        self.assertEqual(report.reviews[0].status, "timeout")
        self.assertEqual(report.reviews[0].opinion.verdict, "abstain")
        self.assertEqual(report.reviews[1].status, "ok")
        self.assertEqual(report.reviews[1].opinion.verdict, "neutral")

    def test_shared_deadline_returns_without_waiting_for_slow_analyst(self):
        glm = StaticAdapter(delay=0.20)
        deepseek = StaticAdapter(opinion(verdict="neutral", evidence_ids=[]))
        council = ModelCouncil(
            enabled_config(overall_timeout_seconds=0.03),
            glm_adapter=glm,
            deepseek_adapter=deepseek,
        )

        started = time.monotonic()
        report = council.review({"symbol": "FPT"}, evidence_ids=("price:1",))
        elapsed = time.monotonic() - started

        self.assertLess(elapsed, 0.15)
        self.assertEqual(report.status, "partial")
        self.assertEqual(report.reviews[0].status, "timeout")
        self.assertEqual(report.reviews[0].opinion.verdict, "abstain")
        self.assertEqual(report.reviews[1].status, "ok")

    def test_invalid_provider_output_becomes_abstain_and_is_not_cached(self):
        invalid = opinion()
        invalid["unexpected"] = "field"
        glm = StaticAdapter(invalid)
        deepseek = StaticAdapter()
        council = ModelCouncil(enabled_config(), glm_adapter=glm, deepseek_adapter=deepseek)

        first = council.review({"symbol": "FPT"}, evidence_ids=("price:1",))
        second = council.review({"symbol": "FPT"}, evidence_ids=("price:1",))

        self.assertEqual(first.status, "partial")
        self.assertEqual(first.reviews[0].status, "invalid")
        self.assertEqual(first.reviews[0].opinion.verdict, "abstain")
        self.assertFalse(second.cache_hit)
        self.assertEqual(glm.calls, 2)
        self.assertEqual(deepseek.calls, 2)

    def test_cache_uses_canonical_evidence_and_non_secret_config_fingerprint(self):
        now = [100.0]
        glm = StaticAdapter()
        deepseek = StaticAdapter()
        config = enabled_config(cache_ttl_seconds=10.0)
        council = ModelCouncil(
            config,
            glm_adapter=glm,
            deepseek_adapter=deepseek,
            clock=lambda: now[0],
        )

        first = council.review(
            {"symbol": "FPT", "facts": {"price": 100, "rsi": 55}},
            evidence_ids=("price:1",),
        )
        second = council.review(
            {"facts": {"rsi": 55, "price": 100}, "symbol": "FPT"},
            evidence_ids=("price:1",),
        )

        self.assertFalse(first.cache_hit)
        self.assertTrue(second.cache_hit)
        self.assertEqual(first.fingerprint, second.fingerprint)
        self.assertEqual(glm.calls, 1)
        self.assertEqual(deepseek.calls, 1)

        now[0] = 111.0
        expired = council.review(
            {"symbol": "FPT", "facts": {"price": 100, "rsi": 55}},
            evidence_ids=("price:1",),
        )
        self.assertFalse(expired.cache_hit)
        self.assertEqual(glm.calls, 2)
        self.assertEqual(deepseek.calls, 2)

        other_model = ModelCouncil(
            CouncilConfig(glm_model="another-model"),
        )
        self.assertNotEqual(
            council.fingerprint({"symbol": "FPT"}, evidence_ids=("price:1",)),
            other_model.fingerprint({"symbol": "FPT"}, evidence_ids=("price:1",)),
        )

    def test_invalid_or_non_finite_evidence_fails_before_provider_calls(self):
        glm = StaticAdapter()
        deepseek = StaticAdapter()
        council = ModelCouncil(enabled_config(), glm_adapter=glm, deepseek_adapter=deepseek)

        with self.assertRaises(ValueError):
            council.review({"price": float("nan")}, evidence_ids=("price:1",))
        self.assertEqual(glm.calls, 0)
        self.assertEqual(deepseek.calls, 0)


if __name__ == "__main__":
    unittest.main()
