# -*- coding: utf-8 -*-
"""Anthropic <-> OpenAI format cevirisi testleri (saf fonksiyon, ag yok).

Kapsanan gercek regresyonlar:
  - tool_use / tool_result cift yonlu ceviri (cok turlu gecmis ATILMIYOR)
  - image bloklari iki yonde de KORUNUYOR (sessizce dusmuyor)
  - stop_reason / finish_reason eslemeleri ("error" sahte end_turn olmamali)
"""
import json
import unittest

import gateway


def img_b64(data: str = "QUJD", media_type: str = "image/png") -> dict:
    return {"type": "image", "source": {"type": "base64", "media_type": media_type,
                                        "data": data}}


def img_url(url: str) -> dict:
    return {"type": "image", "source": {"type": "url", "url": url}}


def user_msg(*blocks) -> dict:
    return {"role": "user", "content": list(blocks)}


def tool_use(tid: str = "toolu_1", name: str = "get_weather", **inp) -> dict:
    return {"type": "tool_use", "id": tid, "name": name, "input": inp or {"city": "Izmir"}}


class TestAnthropicToOpenAI(unittest.TestCase):
    """Anthropic istegi -> OpenAI payload."""

    def test_system_string_becomes_system_message(self):
        out = gateway._anthropic_to_openai({"messages": [], "system": "kisa sistem"})
        self.assertEqual(out["messages"][0], {"role": "system", "content": "kisa sistem"})

    def test_system_blocks_joined(self):
        body = {"messages": [], "system": [{"type": "text", "text": "bir"},
                                            {"type": "text", "text": "iki"}]}
        out = gateway._anthropic_to_openai(body)
        self.assertEqual(out["messages"][0]["content"], "bir iki")

    def test_plain_string_content_passthrough(self):
        body = {"messages": [{"role": "user", "content": "merhaba"}]}
        out = gateway._anthropic_to_openai(body)
        self.assertEqual(out["messages"], [{"role": "user", "content": "merhaba"}])

    def test_tool_use_becomes_assistant_tool_calls(self):
        body = {"messages": [user_msg({"type": "text", "text": "hava?"}),
                             {"role": "assistant", "content": [tool_use()]}]}
        msgs = gateway._anthropic_to_openai(body)["messages"]
        asst = msgs[1]
        self.assertEqual(asst["role"], "assistant")
        self.assertEqual(len(asst["tool_calls"]), 1)
        tc = asst["tool_calls"][0]
        self.assertEqual(tc["id"], "toolu_1")
        self.assertEqual(tc["type"], "function")
        self.assertEqual(tc["function"]["name"], "get_weather")
        self.assertEqual(json.loads(tc["function"]["arguments"]), {"city": "Izmir"})

    def test_tool_result_becomes_tool_message(self):
        body = {"messages": [{"role": "user",
                              "content": [{"type": "tool_result", "tool_use_id": "toolu_1",
                                           "content": "22 derece"}]}]}
        msgs = gateway._anthropic_to_openai(body)["messages"]
        self.assertEqual(msgs, [{"role": "tool", "tool_call_id": "toolu_1",
                                 "content": "22 derece"}])

    def test_tool_result_block_list_keeps_only_text(self):
        body = {"messages": [{"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "t1",
             "content": [{"type": "text", "text": "satir1"},
                         {"type": "text", "text": "satir2"}]}]}]}
        msgs = gateway._anthropic_to_openai(body)["messages"]
        self.assertEqual(msgs[0]["content"], "satir1\nsatir2")

    def test_multi_turn_tool_history_is_not_dropped(self):
        """Asil regresyon: 3 turlu tool gecmisi OpenAI tarafinda da 3 mesaj."""
        body = {"messages": [
            {"role": "user", "content": [{"type": "text", "text": "hava"}]},
            {"role": "assistant", "content": [tool_use()]},
            {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "toolu_1",
                                          "content": "22"}]},
        ]}
        msgs = gateway._anthropic_to_openai(body)["messages"]
        self.assertEqual([m["role"] for m in msgs], ["user", "assistant", "tool"])
        self.assertEqual(msgs[1]["tool_calls"][0]["id"], "toolu_1")
        self.assertEqual(msgs[2]["tool_call_id"], "toolu_1")

    def test_tool_result_with_image_keeps_text_and_image(self):
        """MCP screenshot: tool_result icindeki gorsel de iletilmeli."""
        body = {"messages": [{"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "t1", "content": [
                {"type": "text", "text": "ss alindi"},
                img_b64(),
            ]}]}]}
        msgs = gateway._anthropic_to_openai(body)["messages"]
        parts = msgs[0]["content"]
        self.assertEqual(msgs[0]["role"], "tool")
        self.assertEqual(parts[0], {"type": "text", "text": "ss alindi"})
        self.assertEqual(parts[1]["type"], "image_url")
        self.assertTrue(parts[1]["image_url"]["url"].startswith("data:image/png;base64,"))

    def test_tools_translated_with_input_schema(self):
        body = {"messages": [], "tools": [
            {"name": "get_weather", "description": "hava",
             "input_schema": {"type": "object", "properties": {"city": {"type": "string"}}}},
            {"description": "isimsiz, atlanmali"},
        ]}
        out = gateway._anthropic_to_openai(body)
        self.assertEqual(len(out["tools"]), 1)
        self.assertEqual(out["tools"][0], {
            "type": "function",
            "function": {"name": "get_weather", "description": "hava",
                         "parameters": {"type": "object",
                                        "properties": {"city": {"type": "string"}}}},
        })

    def test_tool_choice_variants(self):
        cases = [
            ({"type": "any"}, "required"),
            ({"type": "auto"}, "auto"),
            ({"type": "none"}, "none"),
            ({"type": "tool", "name": "get_weather"},
             {"type": "function", "function": {"name": "get_weather"}}),
        ]
        for tc, beklenen in cases:
            with self.subTest(tc=tc):
                out = gateway._anthropic_to_openai({"messages": [], "tool_choice": tc})
                self.assertEqual(out["tool_choice"], beklenen)

    def test_image_base64_becomes_data_uri(self):
        msgs = gateway._anthropic_to_openai(
            {"messages": [user_msg(img_b64("QUJD", "image/jpeg"))]})["messages"]
        self.assertEqual(msgs[0]["content"][0],
                         {"type": "image_url",
                          "image_url": {"url": "data:image/jpeg;base64,QUJD"}})

    def test_image_url_source_passthrough(self):
        msgs = gateway._anthropic_to_openai(
            {"messages": [user_msg(img_url("https://x.test/a.png"))]})["messages"]
        self.assertEqual(msgs[0]["content"][0],
                         {"type": "image_url",
                          "image_url": {"url": "https://x.test/a.png"}})

    def test_text_plus_image_becomes_parts(self):
        msgs = gateway._anthropic_to_openai({"messages": [
            user_msg({"type": "text", "text": "bu ne?"}, img_b64())]})["messages"]
        self.assertEqual(len(msgs), 1)
        self.assertEqual([p["type"] for p in msgs[0]["content"]], ["text", "image_url"])

    def test_image_only_message_sent_once(self):
        """Sadece gorsel iceren tek mesaj TEK kez gonderilmeli (copya olmamali)."""
        msgs = gateway._anthropic_to_openai(
            {"messages": [user_msg(img_b64())]})["messages"]
        self.assertEqual(len(msgs), 1,
                         f"sadece gorsel iceren mesaj {len(msgs)} kez gonderildi: {msgs}")

    def test_max_tokens_clamped_in_payload(self):
        out = gateway._anthropic_to_openai({"messages": [], "max_tokens": 131072})
        self.assertEqual(out["max_tokens"], 32000)

    def test_temperature_only_when_given(self):
        self.assertNotIn("temperature", gateway._anthropic_to_openai({"messages": []}))
        out = gateway._anthropic_to_openai({"messages": [], "temperature": 0.2})
        self.assertEqual(out["temperature"], 0.2)

    def test_thinking_only_turn_kept(self):
        body = {"messages": [{"role": "assistant", "content": [
            {"type": "thinking", "thinking": "dusunuyorum"}]}]}
        msgs = gateway._anthropic_to_openai(body)["messages"]
        self.assertEqual(msgs, [{"role": "assistant", "content": ""}])


class TestOpenAIToAnthropic(unittest.TestCase):
    """OpenAI payload -> Anthropic (Atria yolu)."""

    def test_tool_calls_become_tool_use_blocks(self):
        payload = {"model": "m", "messages": [
            {"role": "user", "content": "hava"},
            {"role": "assistant", "content": None, "tool_calls": [
                {"id": "c1", "type": "function",
                 "function": {"name": "get_weather", "arguments": '{"city": "Izmir"}'}}]},
            {"role": "tool", "tool_call_id": "c1", "content": "22 derece"},
        ]}
        out = gateway._openai_to_anthropic(payload, "m")
        self.assertEqual([m["role"] for m in out["messages"]], ["user", "assistant", "user"])
        asst = out["messages"][1]["content"]
        self.assertEqual(asst[0], {"type": "tool_use", "id": "c1",
                                   "name": "get_weather", "input": {"city": "Izmir"}})
        self.assertEqual(out["messages"][2]["content"][0],
                         {"type": "tool_result", "tool_use_id": "c1", "content": "22 derece"})

    def test_tool_call_broken_arguments_kept_raw(self):
        payload = {"model": "m", "messages": [
            {"role": "user", "content": "x"},
            {"role": "assistant", "content": "y", "tool_calls": [
                {"id": "c1", "function": {"name": "f", "arguments": "{bozuk"}}]},
        ]}
        blocks = gateway._openai_to_anthropic(payload, "m")["messages"][1]["content"]
        self.assertEqual(blocks, [{"type": "text", "text": "y"},
                                  {"type": "tool_use", "id": "c1", "name": "f",
                                   "input": {"_raw": "{bozuk"}}])

    def test_system_and_developer_merged(self):
        payload = {"model": "m", "messages": [
            {"role": "system", "content": "birinci"},
            {"role": "developer", "content": "ikinci"},
            {"role": "user", "content": "merhaba"},
        ]}
        out = gateway._openai_to_anthropic(payload, "m")
        self.assertEqual(out["system"], "birinci\nikinci")
        self.assertEqual(len(out["messages"]), 1)

    def test_data_uri_image_back_to_anthropic_block(self):
        payload = {"model": "m", "messages": [{"role": "user", "content": [
            {"type": "text", "text": "bu ne?"},
            {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64,QUJD"}},
        ]}]}
        blocks = gateway._openai_to_anthropic(payload, "m")["messages"][0]["content"]
        self.assertEqual(blocks[0], {"type": "text", "text": "bu ne?"})
        self.assertEqual(blocks[1], {"type": "image", "source": {
            "type": "base64", "media_type": "image/jpeg", "data": "QUJD"}})

    def test_plain_url_image_back_to_anthropic_block(self):
        payload = {"model": "m", "messages": [{"role": "user", "content": [
            {"type": "image_url", "image_url": {"url": "https://x.test/a.png"}}]}]}
        blocks = gateway._openai_to_anthropic(payload, "m")["messages"][0]["content"]
        self.assertEqual(blocks, [{"type": "image",
                                   "source": {"type": "url", "url": "https://x.test/a.png"}}])

    def test_tool_message_image_kept(self):
        payload = {"model": "m", "messages": [
            {"role": "user", "content": "ss al"},
            {"role": "tool", "tool_call_id": "c1", "content": [
                {"type": "text", "text": "ok"},
                {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAA"}},
            ]},
        ]}
        blocks = gateway._openai_to_anthropic(payload, "m")["messages"][1]["content"]
        self.assertEqual([b["type"] for b in blocks], ["tool_result", "image"])

    def test_tools_to_input_schema(self):
        payload = {"model": "m", "messages": [], "tools": [
            {"type": "function", "function": {"name": "get_weather", "description": "hava",
                                               "parameters": {"type": "object"}}}],
            "tool_choice": "required"}
        out = gateway._openai_to_anthropic(payload, "m")
        self.assertEqual(out["tools"], [{"name": "get_weather", "description": "hava",
                                         "input_schema": {"type": "object"}}])
        self.assertEqual(out["tool_choice"], {"type": "any"})

    def test_tool_choice_named_function(self):
        payload = {"model": "m", "messages": [],
                   "tool_choice": {"type": "function", "function": {"name": "f"}}}
        out = gateway._openai_to_anthropic(payload, "m")
        self.assertEqual(out["tool_choice"], {"type": "tool", "name": "f"})

    def test_max_tokens_default_and_passthrough(self):
        base = {"model": "m", "messages": [{"role": "user", "content": "x"}]}
        self.assertEqual(gateway._openai_to_anthropic(base, "m")["max_tokens"], 1024)
        self.assertEqual(
            gateway._openai_to_anthropic({**base, "max_tokens": 2048}, "m")["max_tokens"], 2048)

    def test_stop_string_becomes_stop_sequences(self):
        base = {"model": "m", "messages": [{"role": "user", "content": "x"}]}
        out = gateway._openai_to_anthropic({**base, "stop": "DUR"}, "m")
        self.assertEqual(out["stop_sequences"], ["DUR"])


class TestAnthropicToAnthropicBlocks(unittest.TestCase):
    def test_empty_message_becomes_placeholder_block(self):
        self.assertEqual(gateway._openai_to_anthropic_blocks({"content": ""}),
                         [{"type": "text", "text": ""}])

    def test_text_and_tool_calls(self):
        blocks = gateway._openai_to_anthropic_blocks({
            "content": "merhaba",
            "tool_calls": [{"id": "c1", "function": {"name": "f", "arguments": '{"a": 1}'}}],
        })
        self.assertEqual(blocks, [
            {"type": "text", "text": "merhaba"},
            {"type": "tool_use", "id": "c1", "name": "f", "input": {"a": 1}},
        ])


class TestAnthropicResponseToOpenAI(unittest.TestCase):
    def test_stop_reason_tool_use(self):
        out = gateway._anthropic_response_to_openai(
            {"id": "msg_1", "content": [], "stop_reason": "tool_use",
             "usage": {"input_tokens": 5, "output_tokens": 7}}, "m")
        self.assertEqual(out["choices"][0]["finish_reason"], "tool_calls")
        self.assertEqual(out["usage"], {"prompt_tokens": 5, "completion_tokens": 7,
                                        "total_tokens": 12})

    def test_stop_reason_max_tokens(self):
        out = gateway._anthropic_response_to_openai(
            {"content": [{"type": "text", "text": "k"}], "stop_reason": "max_tokens"}, "m")
        self.assertEqual(out["choices"][0]["finish_reason"], "length")

    def test_thinking_folded_into_content(self):
        out = gateway._anthropic_response_to_openai({"content": [
            {"type": "thinking", "thinking": "dusundu"}, {"type": "text", "text": "cevap"}],
            "stop_reason": "end_turn"}, "m")
        self.assertEqual(out["choices"][0]["message"]["content"], "dusundu\ncevap")

    def test_tool_use_block_becomes_tool_call(self):
        out = gateway._anthropic_response_to_openai({"content": [
            {"type": "tool_use", "id": "t1", "name": "f", "input": {"a": 1}}]}, "m")
        tc = out["choices"][0]["message"]["tool_calls"][0]
        self.assertEqual(tc["id"], "t1")
        self.assertEqual(json.loads(tc["function"]["arguments"]), {"a": 1})


class TestStopReasonMapping(unittest.TestCase):
    def test_openai_finish_mapping(self):
        cases = {"end_turn": "stop", "stop_sequence": "stop", "max_tokens": "length",
                 "tool_use": "tool_calls", "pause_turn": "stop", "refusal": "content_filter",
                 None: "stop", "": "stop"}
        for src, beklenen in cases.items():
            with self.subTest(src=src):
                self.assertEqual(gateway._openai_finish(src), beklenen)

    def test_anthropic_stop_mapping(self):
        # girdi OpenAI finish_reason'udur (stop / length / tool_calls / content_filter)
        cases = {"stop": "end_turn", "length": "max_tokens", "tool_calls": "tool_use",
                 "stop_sequence": "stop_sequence", "content_filter": "end_turn",
                 "bilinmeyen": "end_turn"}
        for src, beklenen in cases.items():
            with self.subTest(src=src):
                self.assertEqual(gateway._anthropic_stop(src), beklenen)

    def test_anthropic_stop_none_is_end_turn(self):
        self.assertEqual(gateway._anthropic_stop(None), "end_turn")

    def test_anthropic_stop_error_is_not_successful_turn(self):
        """'error' sahte basari OLMAZ: end_turn'a eslenmemeli (bkz. gateway.py:1783)."""
        self.assertNotEqual(gateway._anthropic_stop("error"), "end_turn",
                            "upstream hata bildirdi ama stop_reason=end_turn uretildi; "
                            "istemci hatayi gormuyor")

    def test_anthropic_stop_error_maps_to_error_exactly(self):
        """Sadece 'end_turn' degil OLMAYAN deger uretmeli.

        Saglayici bu stop_reason'i gormeyen istemciler (opencode gibi)
        icin tutarli bir hata isareti sart; yalnizca 'end_turn degil'
        kontrolu bosa cikar.
        """
        self.assertEqual(gateway._anthropic_stop("error"), "error")

    def test_openai_finish_is_error(self):
        self.assertTrue(gateway._openai_finish_is_error("error"))
        for fin in ("stop", "tool_calls", "length", None, ""):
            with self.subTest(fin=fin):
                self.assertFalse(gateway._openai_finish_is_error(fin))

    def test_openai_finish_is_error_matches_only_exact_token(self):
        """Esleme TAM 'error' ile yapilir; 'ERROR' hata sayilmaz."""
        for fin in ("ERROR", "Error", " error", "error ", "errors", "err", "bilinmeyen"):
            with self.subTest(fin=fin):
                self.assertFalse(gateway._openai_finish_is_error(fin))

    def test_error_flag_and_stop_mapping_stay_in_sync(self):
        """Iki kopya ayrilirsa sahte basari geri gelir.

        `_anthropic_stop` hata bildirdiginde 'error' donmeli; bu tam
        olarak `_openai_finish_is_error` ile ayni kosula bagli olmali.
        """
        for fin in ("stop", "length", "tool_calls", "stop_sequence", "content_filter",
                    "pause_turn", "refusal", None, "", "bilinmeyen", "error"):
            with self.subTest(fin=fin):
                self.assertEqual(gateway._anthropic_stop(fin) == "error",
                                 gateway._openai_finish_is_error(fin))


if __name__ == "__main__":
    unittest.main()
