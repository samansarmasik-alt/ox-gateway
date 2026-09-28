# -*- coding: utf-8 -*-
"""max_tokens kelepcesi ve token sayimi testleri (saf fonksiyon)."""
import json
import unittest
from unittest import mock

import gateway


class TestClampMaxTokens(unittest.TestCase):
    """Istemcinin kocaman max_tokens'i saglayiciya OLDUGU GIBI gonderilmez."""

    def test_ceiling_is_32000(self):
        self.assertEqual(gateway._MAX_OUTPUT_TOKENS, 32000)

    def test_oversized_values_clamped(self):
        for value in (131072, 128000, 64000, 32001, 1_000_000):
            with self.subTest(value=value):
                self.assertEqual(gateway._clamp_max_tokens(value), 32000)

    def test_values_under_ceiling_untouched(self):
        for value in (1, 512, 4096, 32000):
            with self.subTest(value=value):
                self.assertEqual(gateway._clamp_max_tokens(value), value)

    def test_zero_none_garbage_fall_back_to_1024(self):
        for value in (0, None, "", "abc", "12x", [], {}, object()):
            with self.subTest(value=repr(value)):
                self.assertEqual(gateway._clamp_max_tokens(value), 1024)

    def test_negative_falls_back_to_1024(self):
        self.assertEqual(gateway._clamp_max_tokens(-1), 1024)
        self.assertEqual(gateway._clamp_max_tokens(-99999), 1024)

    def test_numeric_string_parsed(self):
        self.assertEqual(gateway._clamp_max_tokens("4096"), 4096)
        self.assertEqual(gateway._clamp_max_tokens("131072"), 32000)

    def test_float_truncated(self):
        self.assertEqual(gateway._clamp_max_tokens(4096.9), 4096)
        self.assertEqual(gateway._clamp_max_tokens(1.5), 1)

    def test_never_returns_zero(self):
        for value in (0, None, "abc", -5, 0.4):
            with self.subTest(value=value):
                self.assertGreater(gateway._clamp_max_tokens(value), 0)


class TestEstimateTokens(unittest.TestCase):
    def test_empty_is_zero(self):
        for text in ("", None):
            with self.subTest(text=repr(text)):
                self.assertEqual(gateway._estimate_tokens(text), 0)

    def test_ascii_about_four_chars_per_token(self):
        self.assertEqual(gateway._estimate_tokens("a" * 400), 100)

    def test_short_text_at_least_one(self):
        self.assertEqual(gateway._estimate_tokens("x"), 1)

    def test_unicode_costs_more_than_ascii(self):
        ascii_t = gateway._estimate_tokens("a" * 200)
        uni_t = gateway._estimate_tokens("ş" * 200)
        self.assertGreater(uni_t, ascii_t)

    def test_never_negative(self):
        self.assertGreaterEqual(gateway._estimate_tokens("merhaba dünya"), 1)


class TestCountRequestTokens(unittest.TestCase):
    def test_empty_body_is_overhead_only(self):
        self.assertEqual(gateway._count_request_tokens({}), 4)
        self.assertEqual(gateway._count_request_tokens({"messages": []}), 4)

    def test_plain_text_message_counted(self):
        n = gateway._count_request_tokens({"messages": [{"role": "user", "content": "a" * 400}]})
        self.assertGreaterEqual(n, 100)

    def test_system_string_and_blocks_counted(self):
        a = gateway._count_request_tokens({"system": "x" * 400, "messages": []})
        b = gateway._count_request_tokens({"system": [{"type": "text", "text": "x" * 400}],
                                            "messages": []})
        self.assertEqual(a, b)
        self.assertGreaterEqual(a, 100)

    def test_image_costs_about_1600(self):
        n = gateway._count_request_tokens({"messages": [{"role": "user", "content": [
            {"type": "image", "source": {"type": "base64", "media_type": "image/png",
                                          "data": "A" * 100000}}]}]})
        self.assertGreaterEqual(n, 1600)
        self.assertLess(n, 1700)  # gorsel agir ama 100k base64 karakteri kadar degil

    def test_two_images_double_the_cost(self):
        one = gateway._count_request_tokens({"messages": [{"role": "user", "content": [
            {"type": "image", "source": {"type": "base64", "data": "A"}}]}]})
        two = gateway._count_request_tokens({"messages": [{"role": "user", "content": [
            {"type": "image", "source": {"type": "base64", "data": "A"}},
            {"type": "image", "source": {"type": "base64", "data": "A"}}]}]})
        self.assertEqual(two - one, 1600)

    def test_tool_definitions_counted(self):
        without = gateway._count_request_tokens({"messages": [{"role": "user", "content": "x"}]})
        with_tools = gateway._count_request_tokens({
            "messages": [{"role": "user", "content": "x"}],
            "tools": [{"name": "get_weather", "description": "hava durumu",
                       "input_schema": {"type": "object",
                                        "properties": {"city": {"type": "string"}}}}]})
        self.assertGreater(with_tools, without + 10)

    def test_tool_use_and_result_blocks_counted(self):
        n = gateway._count_request_tokens({"messages": [{"role": "user", "content": [
            {"type": "tool_use", "id": "t1", "name": "get_weather",
             "input": {"city": "Izmir"}},
            {"type": "tool_result", "tool_use_id": "t1", "content": "22 derece"}]}]})
        self.assertGreater(n, 10)

    def test_openai_style_tool_calls_counted(self):
        n = gateway._count_request_tokens({"messages": [{"role": "assistant", "content": "",
                                                         "tool_calls": [
            {"id": "c1", "function": {"name": "get_weather",
                                      "arguments": '{"city": "Izmir"}'}}]}]})
        self.assertGreater(n, 8)

    def test_thinking_block_counted(self):
        n = gateway._count_request_tokens({"messages": [{"role": "assistant", "content": [
            {"type": "thinking", "thinking": "dusunuyorum " * 20}]}]})
        self.assertGreater(n, 20)

    def test_unicode_text_estimated(self):
        n = gateway._count_request_tokens({"messages": [{"role": "user",
                                                         "content": "şğüöç ħ€llo dünya"}]})
        self.assertGreater(n, 4)

    def test_garbage_entries_tolerated(self):
        n = gateway._count_request_tokens({
            "system": "x",
            "messages": [None, {"role": "user", "content": ["metin", 5,
                                                             {"type": "bilinmeyen"}]},
                        {"no_content": True}],
            "tools": [None, {"name": "t"}]})
        self.assertGreater(n, 0)

    def test_monotonic_with_length(self):
        kisa = gateway._count_request_tokens({"messages": [{"role": "user", "content": "a" * 40}]})
        uzun = gateway._count_request_tokens({"messages": [{"role": "user", "content": "a" * 4000}]})
        self.assertGreater(uzun, kisa)

    def test_result_is_int(self):
        n = gateway._count_request_tokens({"messages": [{"role": "user", "content": "selam"}]})
        self.assertIsInstance(n, int)

    def test_no_upstream_call_needed(self):
        """Tahmin yerel: ag cagrisi yapilmaz (test ortaminda calistigi kanitlar)."""
        with mock.patch.object(gateway, "call_openrouter") as mock_call:
            gateway._count_request_tokens({"messages": [{"role": "user", "content": "x"}]})
        mock_call.assert_not_called()


class TestTokenBudgetPlumbing(unittest.TestCase):
    def test_payload_max_tokens_is_clamped(self):
        out = gateway._anthropic_to_openai({"messages": [], "max_tokens": 131072})
        self.assertEqual(out["max_tokens"], gateway._MAX_OUTPUT_TOKENS)

    def test_missing_max_tokens_gets_1024(self):
        out = gateway._anthropic_to_openai({"messages": []})
        self.assertEqual(out["max_tokens"], 1024)

    def test_reasoning_budget_comes_from_config(self):
        eski = gateway.CONFIG.get("reasoning_max_tokens")
        try:
            gateway.CONFIG["reasoning_max_tokens"] = 1024
            self.assertEqual(gateway._reasoning_config({}), {"max_tokens": 1024})
        finally:
            if eski is None:
                gateway.CONFIG.pop("reasoning_max_tokens", None)
            else:
                gateway.CONFIG["reasoning_max_tokens"] = eski

    def test_client_thinking_budget_wins(self):
        self.assertIsNone(gateway._reasoning_config(
            {"thinking": {"type": "enabled", "budget_tokens": 4096}}))

    def test_thinking_zero_disables_gateway_reasoning(self):
        eski = gateway.CONFIG.get("reasoning_max_tokens")
        try:
            gateway.CONFIG["reasoning_max_tokens"] = 0
            self.assertIsNone(gateway._reasoning_config({}))
        finally:
            if eski is None:
                gateway.CONFIG.pop("reasoning_max_tokens", None)
            else:
                gateway.CONFIG["reasoning_max_tokens"] = eski

    def test_payload_variants_strip_reasoning_then_tools(self):
        v = gateway._payload_variants({"messages": [], "reasoning": {"max_tokens": 8},
                                       "tools": [1]})
        self.assertEqual(len(v), 3)
        self.assertNotIn("reasoning", v[1])
        self.assertNotIn("tools", v[2])

    def test_count_tokens_matches_manual_text_estimate(self):
        body = {"messages": [{"role": "user", "content": "selam"}]}
        beklenen = gateway._estimate_tokens("selam") + 8  # 4 * (1 mesaj + 1)
        self.assertEqual(gateway._count_request_tokens(body), beklenen)

    def test_json_import_available_for_tool_args(self):
        self.assertEqual(json.loads('{"a": 1}'), {"a": 1})


if __name__ == "__main__":
    unittest.main()
