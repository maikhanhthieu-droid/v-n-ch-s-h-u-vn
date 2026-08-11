import json
import sys
import threading
import time
import unittest
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from model_council import (  # noqa: E402
    AnalystOpinion,
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


class ModelCouncilTests(unittest.TestCase):
    def test_build_from_env_is_a_disabled_safe_default(self):
        council = build_from_env({})
        self.assertIsInstance(council, ModelCouncil)
        self.assertFalse(council.enabled)

    def test_config_is_opt_in_and_missing_either_key_disables_whole_council(self):
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

        self.assertFalse(council.enabled)
        self.assertEqual(report.status, "disabled")
        self.assertEqual([review.opinion.verdict for review in report.reviews], ["abstain", "abstain"])
        self.assertEqual(glm.calls, 0)
        self.assertEqual(deepseek.calls, 0)

        keys_are_the_opt_in = CouncilConfig(
            glm_api_key="glm-key",
            deepseek_api_key="deepseek-key",
        )
        self.assertTrue(keys_are_the_opt_in.effective_enabled)

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
