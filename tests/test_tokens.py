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
        # NOT: reasoning_effort varsa o onceliklidir; sayi secimi ancak
        # reasoning_effort bos/None iken devreye girer.
        eski_effort = gateway.CONFIG.get("reasoning_effort")
        eski = gateway.CONFIG.get("reasoning_max_tokens")
        try:
            gateway.CONFIG.pop("reasoning_effort", None)
            gateway.CONFIG["reasoning_max_tokens"] = 1024
            self.assertEqual(gateway._reasoning_config({}), {"max_tokens": 1024})
        finally:
            if eski is None:
                gateway.CONFIG.pop("reasoning_max_tokens", None)
            else:
                gateway.CONFIG["reasoning_max_tokens"] = eski
            if eski_effort is None:
                gateway.CONFIG.pop("reasoning_effort", None)
            else:
                gateway.CONFIG["reasoning_effort"] = eski_effort

    def test_client_thinking_budget_wins(self):
        """Istemci thinking budget_tokens gonderdiyse dikkate alinir.

        ONCEKI DAVRANIS (DUZELTILDI): burada None donuyordu, yani "thinking
        ACIK" diyen istemciye gateway thinking'i tamamen KAPATIYORDU.
        Claude Code bu alani sik gonderiyor; model neredeyse hic dusunmedigi
        icin suphelenilen belirti buydu.

        Artik butce 'effort' seviyesine cevrilir (reasoning_max_tokens
        kilidi kalirsa: eski hali reasoning.max_tokens gonderirdi).
        """
        rc = None
        eski = gateway.CONFIG.get("reasoning_budget_to_effort")
        try:
            gateway.CONFIG["reasoning_budget_to_effort"] = True
            rc = gateway._reasoning_config(
                {"thinking": {"type": "enabled", "budget_tokens": 4096}})
            self.assertEqual(rc, {"effort": "low"})
            # bayrak kapatilirsa eski (max_tokens) davranisa doner
            gateway.CONFIG["reasoning_budget_to_effort"] = False
            self.assertEqual(
                gateway._reasoning_config(
                    {"thinking": {"type": "enabled", "budget_tokens": 4096}}),
                {"max_tokens": 4096})
        finally:
            if eski is None:
                gateway.CONFIG.pop("reasoning_budget_to_effort", None)
            else:
                gateway.CONFIG["reasoning_budget_to_effort"] = eski

    def test_client_thinking_budget_is_clamped_to_leave_answer_room(self):
        """Devasa butce cevap icin yer birakir (effort yolunda da)."""
        gateway.CONFIG["reasoning_budget_to_effort"] = False
        eski = gateway.CONFIG.get("reasoning_budget_to_effort")
        try:
            rc = gateway._reasoning_config(
                {"thinking": {"type": "enabled", "budget_tokens": 60000}}, 32000)
            self.assertEqual(rc, {"max_tokens": 19200})  # 32000 * 0.6
        finally:
            if eski is None:
                gateway.CONFIG.pop("reasoning_budget_to_effort", None)
            else:
                gateway.CONFIG["reasoning_budget_to_effort"] = eski

    def test_thinking_disabled_type_turns_reasoning_off(self):
        self.assertIsNone(
            gateway._reasoning_config({"thinking": {"type": "disabled"}}))

    def test_thinking_zero_disables_gateway_reasoning(self):
        eski_effort = gateway.CONFIG.get("reasoning_effort")
        eski = gateway.CONFIG.get("reasoning_max_tokens")
        try:
            gateway.CONFIG.pop("reasoning_effort", None)
            gateway.CONFIG["reasoning_max_tokens"] = 0
            self.assertIsNone(gateway._reasoning_config({}))
        finally:
            if eski is None:
                gateway.CONFIG.pop("reasoning_max_tokens", None)
            else:
                gateway.CONFIG["reasoning_max_tokens"] = eski
            if eski_effort is None:
                gateway.CONFIG.pop("reasoning_effort", None)
            else:
                gateway.CONFIG["reasoning_effort"] = eski_effort


class TestReasoningEffort(unittest.TestCase):
    """reasoning_effort ayari: effort <-> max_tokens secimi ve gecersiz degerler.

    OpenRouter olcum notu: effort ve max_tokens AYNI ANDA gonderilemez,
    HTTP 400 donuyor ("Only one of reasoning.effort and reasoning.max_tokens
    can be specified"). Bu yuzden ikisi birden ASLA uretilmemeli.
    """

    def setUp(self):
        self._effort = gateway.CONFIG.get("reasoning_effort")
        self._tokens = gateway.CONFIG.get("reasoning_max_tokens")

    def tearDown(self):
        for k, v in (("reasoning_effort", self._effort),
                     ("reasoning_max_tokens", self._tokens)):
            if v is None:
                gateway.CONFIG.pop(k, None)
            else:
                gateway.CONFIG[k] = v

    def _with(self, **cfg):
        for k in ("reasoning_effort", "reasoning_max_tokens"):
            gateway.CONFIG.pop(k, None)
        gateway.CONFIG.update(cfg)

    def test_named_levels_map_to_effort(self):
        for lvl in ("minimal", "low", "medium", "high", "xhigh"):
            with self.subTest(level=lvl):
                self._with(reasoning_effort=lvl)
                self.assertEqual(gateway._reasoning_config({}), {"effort": lvl})

    def test_numeric_maps_to_max_tokens(self):
        self._with(reasoning_effort="4096")
        self.assertEqual(gateway._reasoning_config({}, 32000),
                         {"max_tokens": 4096})

    def test_off_variants_disable(self):
        for v in ("off", "OFF", "none", "0", "false", "kapali", "yok"):
            with self.subTest(value=v):
                self._with(reasoning_effort=v)
                self.assertIsNone(gateway._reasoning_config({}))

    def test_empty_effort_means_unset_and_uses_legacy_tokens(self):
        # "" bos deger "ayar yok" demektir (geriye uyum), "off" degil.
        self._with(reasoning_effort="", reasoning_max_tokens=1024)
        self.assertEqual(gateway._reasoning_config({}), {"max_tokens": 1024})

    def test_never_sends_effort_and_max_tokens_together(self):
        for v in ("low", "medium", "high", "xhigh", "minimal", "2048"):
            with self.subTest(value=v):
                self._with(reasoning_effort=v)
                rc = gateway._reasoning_config({}, 32000) or {}
                self.assertFalse("effort" in rc and "max_tokens" in rc,
                                 "OpenRouter 400 verir")

    def test_missing_effort_falls_back_to_legacy_tokens(self):
        self._with(reasoning_max_tokens=1024)
        self.assertEqual(gateway._reasoning_config({}), {"max_tokens": 1024})

    def test_clamp_leaves_answer_room(self):
        self.assertEqual(gateway._clamp_thinking_budget(60000, 32000), 19200)
        self.assertEqual(gateway._clamp_thinking_budget(100, 32000), 1024)
        self.assertEqual(gateway._clamp_thinking_budget(8000, 32000), 8000)


class TestClientEffortWins(unittest.TestCase):
    """Istemcinin gonderdigi effort artik OKUNUYOR.

    ONCE HIC OKUNMUYORDU: OpenAI tarzi reasoning / reasoning_effort alanlari
    gateway tarafindan sessizce dusuruluyordu. Yani opencode bir effort
    secip gonderiyor, gateway kendi varsayilanini kullaniyordu - kullanici
    "model secerken effortunu secemiyorum" diyordu.
    """

    M = "stealth/space-bunny-alpha"

    def test_openai_reasoning_effort_object(self):
        self.assertEqual(
            gateway._reasoning_config({"reasoning": {"effort": "low"}}, 32000, self.M),
            {"effort": "low"})

    def test_openai_reasoning_effort_flat_field(self):
        self.assertEqual(
            gateway._reasoning_config({"reasoning_effort": "minimal"}, 32000, self.M),
            {"effort": "minimal"})

    def test_openai_reasoning_max_tokens(self):
        """OpenAI tarzi reasoning.max_tokens -> effort seviyesine cevrilir."""
        self.assertEqual(
            gateway._reasoning_config({"reasoning": {"max_tokens": 6000}}, 32000, self.M),
            {"effort": "low"})

    def test_client_effort_is_clamped(self):
        """Butce kirpma + effort'a cevirme.

        ONCEKI DAVRANIS (DUZELTILDI): reasoning.max_tokens olarak
        gonderiliyordu; saglayici effort'a daha cok uyuyor (olcum:
        effort=xhigh 1003 vs max_tokens=24576 870 karakter). Artik butce
        en yakin 'effort' seviyesine cevrilir.
        """
        self.assertEqual(
            gateway._reasoning_config({"reasoning": {"max_tokens": 90000}}, 32000, self.M),
            {"effort": "xhigh"})

    def test_client_off_variants_disable(self):
        for v in ("off", "none", "disabled", "minimalx"):
            with self.subTest(value=v):
                self.assertIsNone(
                    gateway._reasoning_config({"reasoning": {"effort": v}}, 32000, self.M))

    def test_client_reasoning_overrides_model_and_global(self):
        eski = gateway.CONFIG.get("reasoning_effort")
        per = gateway.CONFIG.get("reasoning_effort_by_model")
        try:
            gateway.CONFIG["reasoning_effort"] = "off"
            gateway.CONFIG["reasoning_effort_by_model"] = {self.M: "off"}
            # model ve genel 'off' olsa bile istemci acikca 'high' istedi
            self.assertEqual(
                gateway._reasoning_config({"reasoning": {"effort": "high"}}, 32000, self.M),
                {"effort": "high"})
        finally:
            if eski is None:
                gateway.CONFIG.pop("reasoning_effort", None)
            else:
                gateway.CONFIG["reasoning_effort"] = eski
            if per is None:
                gateway.CONFIG.pop("reasoning_effort_by_model", None)
            else:
                gateway.CONFIG["reasoning_effort_by_model"] = per


class TestBudgetToEffortMapping(unittest.TestCase):
    """Istemci thinking BUDGET'i -> OpenRouter 'effort' seviyesi.

    Neden: OpenRouter'da effort ve max_tokens birlikte gonderilemez ve
    ikisi ayni isi yapmiyor. Olcum (space-bunny, n=3):
        reasoning: {effort: "xhigh"}   -> 1003 dusunme karakteri
        reasoning: {max_tokens: 24576} ->  870
    Yani saglayici effort'a daha cok uyuyor.
    """

    def setUp(self):
        self._flag = gateway.CONFIG.get("reasoning_budget_to_effort")
        self._effort = gateway.CONFIG.get("reasoning_effort")
        gateway.CONFIG["reasoning_budget_to_effort"] = True

    def tearDown(self):
        for k, v in (("reasoning_budget_to_effort", self._flag),
                     ("reasoning_effort", self._effort)):
            if v is None:
                gateway.CONFIG.pop(k, None)
            else:
                gateway.CONFIG[k] = v

    def test_opencode_variants_map_one_to_one(self):
        import sync_opencode
        v = sync_opencode.thinking_variants()
        eslenme = {"low": "low", "medium": "medium", "high": "high",
                   "max": "xhigh"}
        for seviye, beklenen in eslenme.items():
            with self.subTest(variant=seviye):
                rc = gateway._reasoning_config(
                    {"thinking": v[seviye]["thinking"]}, 32000)
                self.assertEqual(rc, {"effort": beklenen})

    def test_off_variant_still_sends_nothing(self):
        self.assertIsNone(gateway._reasoning_config(
            {"thinking": {"type": "disabled"}}, 32000))

    def test_arbitrary_budget_rounds_up_to_nearest_level(self):
        self.assertEqual(gateway._budget_to_effort(1), "low")
        self.assertEqual(gateway._budget_to_effort(4095), "low")
        self.assertEqual(gateway._budget_to_effort(4096), "low")
        self.assertEqual(gateway._budget_to_effort(10239), "low")
        self.assertEqual(gateway._budget_to_effort(10240), "medium")
        self.assertEqual(gateway._budget_to_effort(16384), "high")
        self.assertEqual(gateway._budget_to_effort(24576), "xhigh")
        self.assertIsNone(gateway._budget_to_effort(0))

    def test_flag_off_keeps_old_max_tokens_behaviour(self):
        gateway.CONFIG["reasoning_budget_to_effort"] = False
        rc = gateway._reasoning_config(
            {"thinking": {"type": "enabled", "budget_tokens": 24576}}, 32000)
        self.assertEqual(rc, {"max_tokens": 19200})

    def test_explicit_effort_never_converted(self):
        """Istemci dogrudan effort gonderiyorsa butceye cevrilmez."""
        self.assertEqual(
            gateway._reasoning_config({"reasoning": {"effort": "medium"}}, 32000),
            {"effort": "medium"})


class TestDefaultModelAlias(unittest.TestCase):
    """"default" takma adi -> anlik aktif model.

    Kullanici modeli dashboard'dan secmek istiyor; opencode'da her
    oturumda model secmek zorunda kalmamali.
    """

    def setUp(self):
        self._pm = gateway.CONFIG.get("provider_models")
        gateway.CONFIG.setdefault("provider_models", {})
        gateway.CONFIG["provider_models"]["2"] = "vendor/aktif-model"

    def tearDown(self):
        if self._pm is None:
            gateway.CONFIG.pop("provider_models", None)
        else:
            gateway.CONFIG["provider_models"] = self._pm

    def test_aliases_resolve_to_active_model(self):
        for ad in ("default", "ox/default", "auto", "ox/auto", "DEFAULT", ""):
            with self.subTest(alias=ad):
                self.assertEqual(gateway._resolve_model_alias(ad),
                                 "vendor/aktif-model")

    def test_none_resolves_to_active_model(self):
        self.assertEqual(gateway._resolve_model_alias(None),
                         "vendor/aktif-model")

    def test_real_model_passes_through_untouched(self):
        for m in ("stealth/space-bunny-alpha", "Atria-Dawn-Preview",
                  "openai/gpt-6-astra"):
            with self.subTest(model=m):
                self.assertEqual(gateway._resolve_model_alias(m), m)

    def test_default_variants_present(self):
        """opencode her modelde variants bekliyor; yoksa thinking arayuzu acilmaz."""
        v = gateway._default_variants()
        self.assertEqual(v["off"], {"thinking": {"type": "disabled"}})
        for lvl in ("low", "medium", "high", "max"):
            self.assertEqual(v[lvl]["thinking"]["type"], "enabled")

    def test_default_alias_is_not_routed_to_atria_in_mode2(self):
        gateway.CONFIG["active_mode"] = "2"
        eski = gateway.CONFIG.get("active_mode")
        try:
            self.assertFalse(gateway._should_route_atria("default"))
        finally:
            if eski is None:
                gateway.CONFIG.pop("active_mode", None)
            else:
                gateway.CONFIG["active_mode"] = eski


class TestPaidFallbackGuard(unittest.IsolatedAsyncioTestCase):
    """Yedek zincire ucretli model GIREMEZ (kullaniciyi borclandirmamak icin).

    Konu: bir model config'e yanlislikla yazilirsa gateway onu 429 halinde
    OTOMATIK olarak cagirir ve kullanici istemeden fatura olusur.
    """

    def setUp(self):
        self._fb = gateway.CONFIG.get("fallback_models")
        self._allow = gateway.CONFIG.get("allow_paid_fallbacks")
        self._auto = gateway.CONFIG.get("auto_model_fallback")
        self._mode = gateway.CONFIG.get("active_mode")

    def tearDown(self):
        for k, v in (("fallback_models", self._fb),
                     ("allow_paid_fallbacks", self._allow),
                     ("auto_model_fallback", self._auto),
                     ("active_mode", self._mode)):
            if v is None:
                gateway.CONFIG.pop(k, None)
            else:
                gateway.CONFIG[k] = v

    def test_free_suffix_is_not_paid(self):
        for m in ("dots-studio/dots-3-note-preview:free", "openrouter/free"):
            with self.subTest(model=m):
                self.assertFalse(gateway._is_paid_model(m))

    def test_name_rule_flags_unknown_as_paid(self):
        """Katalog YOKKEN temkinli davranis: isimde :free yoksa ucretli.

        Bu bir yedek yontem; asil kural fiyattir (asagida).
        """
        for m in ("deepseek/deepseek-v4-flash", "openai/gpt-6-astra", ""):
            with self.subTest(model=m):
                self.assertTrue(gateway._looks_paid_by_name(m))

    def test_real_price_beats_name(self):
        """':free' etiketi OLMAYAN bedava model gercekten ucretsiz olmali.

        Olculdu: stealth/space-bunny-alpha -> pricing.prompt=0,
        pricing.completion=0. Onceki kod isme baktigi icin bunu ucretli
        sayiyordu.
        """
        cat = {"stealth/space-bunny-alpha": {
            "id": "stealth/space-bunny-alpha",
            "pricing": {"prompt": "0", "completion": "0"}}}
        self.assertFalse(gateway._is_paid_model(
            "stealth/space-bunny-alpha", cat),
            "fiyati 0 olan model ucretli sayilmamali")
        self.assertTrue(gateway._looks_paid_by_name("stealth/space-bunny-alpha"),
                        "isim kurali bunu yanlis bulurdu - katalog duzeltir")

    def test_paid_model_in_catalog_still_blocked(self):
        cat = {"deepseek/deepseek-v4-flash": {
            "id": "deepseek/deepseek-v4-flash",
            "pricing": {"prompt": "0.0000002", "completion": "0.000000056"}}}
        self.assertTrue(gateway._is_paid_model("deepseek/deepseek-v4-flash", cat))

    def test_model_absent_from_catalog_falls_back_to_name(self):
        self.assertFalse(gateway._is_paid_model("vendor/x:free", {}))
        self.assertTrue(gateway._is_paid_model("vendor/x", {}))

    def test_paid_fallback_listed_as_blocked(self):
        gateway.CONFIG["allow_paid_fallbacks"] = False
        gateway.CONFIG["fallback_models"] = ["deepseek/deepseek-v4-flash",
                                             "qwen/qwen3.8-27b:free"]
        self.assertEqual(gateway._paid_fallbacks_blocked(),
                         ["deepseek/deepseek-v4-flash"])

    def test_explicit_opt_in_allows_paid(self):
        gateway.CONFIG["allow_paid_fallbacks"] = True
        gateway.CONFIG["fallback_models"] = ["deepseek/deepseek-v4-flash"]
        self.assertEqual(gateway._paid_fallbacks_blocked(), [])

    async def test_chain_excludes_paid_fallback(self):
        gateway.CONFIG["active_mode"] = "2"
        gateway.CONFIG["allow_paid_fallbacks"] = False
        gateway.CONFIG["auto_model_fallback"] = True
        gateway.CONFIG["max_model_fallbacks"] = 4
        gateway.CONFIG["fallback_models"] = ["deepseek/deepseek-v4-flash",
                                             "qwen/qwen3.8-27b:free"]
        chain = await gateway._candidate_models("stealth/space-bunny-alpha")
        self.assertNotIn("deepseek/deepseek-v4-flash", chain)
        self.assertIn("qwen/qwen3.8-27b:free", chain)

    async def test_primary_model_is_never_blocked(self):
        """Kullanicinin kendi sectigi aktif model engellenmez."""
        gateway.CONFIG["active_mode"] = "2"
        gateway.CONFIG["allow_paid_fallbacks"] = False
        gateway.CONFIG["auto_model_fallback"] = False
        chain = await gateway._candidate_models("stealth/space-bunny-alpha")
        self.assertEqual(chain, ["stealth/space-bunny-alpha"])


class TestIncludeReasoningFlag(unittest.TestCase):
    """reasoning acikken include_reasoning gonderilir.

    Olcum (space-bunny, 2 istek):
      effort=high                     -> dusunme 339 / metin 102
      effort=high + include_reasoning -> dusunme 492 / metin 448
    Saglayicinin resmi bayragi; model desteklemiyorsa zarari yok.
    """

    def test_flag_sent_when_reasoning_configured(self):
        import inspect
        src = inspect.getsource(gateway.anthropic_messages)
        self.assertIn("include_reasoning", src)

    def test_free_suffix_check_matches_gateway_candidates(self):
        # yedek zinciri yalnizca _is_free() olanlari dinamik ekler
        self.assertTrue(gateway._is_free({"id": "qwen/qwen3.8-27b:free",
                                         "pricing": {"prompt": "0", "completion": "0"}}))
        self.assertFalse(gateway._is_free({"id": "deepseek/deepseek-v4-flash",
                                           "pricing": {"prompt": "0.0000002",
                                                       "completion": "0.0000008"}}))


class TestOpencodeModelSchema(unittest.TestCase):
    """sync_opencode'in yazdigi model nesnesi opencode semasina uymali.

    Semayi opencode.exe binary'sinden cikardik (ConfigProviderV1.Model):
      id, name, family, release_date, attachment, reasoning, temperature,
      tool_call, interleaved, cost, limit, modalities, experimental, status,
      provider, options, headers, variants
    """

    def setUp(self):
        import sync_opencode
        self.caps = sync_opencode.model_capabilities()

    def test_model_marked_as_reasoning(self):
        """opencode'da reasoning varsayilan FALSE; isaretlenmezse model
        'dusunen model' sayilmaz ve thinking arayuzu acilmaz."""
        self.assertTrue(self.caps["reasoning"])

    def test_limit_declared(self):
        """limit.output semada zorunlu; opencode variantlari bu degerden
        hesapliyor (thinking butcesi)."""
        self.assertIn("limit", self.caps)
        self.assertIn("output", self.caps["limit"])
        self.assertIn("context", self.caps["limit"])

    def test_output_limit_matches_gateway_ceiling(self):
        import gateway
        self.assertEqual(self.caps["limit"]["output"], gateway._MAX_OUTPUT_TOKENS)

    def test_vision_modalities_declared(self):
        self.assertIn("image", self.caps["modalities"]["input"])

    def test_max_variant_fits_inside_clamp(self):
        """En buyuk butce cevap icin yer birakmali (gateway %60 kirpar)."""
        import sync_opencode
        import gateway
        biggest = sync_opencode.thinking_variants()["max"]["thinking"]["budgetTokens"]
        self.assertEqual(
            gateway._clamp_thinking_budget(biggest, gateway._MAX_OUTPUT_TOKENS), 19200)
        self.assertLess(biggest, gateway._MAX_OUTPUT_TOKENS)


class TestThinkingVariantsShape(unittest.TestCase):
    """sync_opencode'in opencode.json'a yazdigi thinking varyantlari.

    OpenCode'un kendi kodundan cikarildi (binary'de dogrulandi):
        case "@ai-sdk/anthropic":
          return { thinking: { type: "enabled", budgetTokens: Z } }

    Dolayisiyla varyant degeri `thinking.budgetTokens` icermeli; SDK bunu
    govdeye `thinking: {type:"enabled", budget_tokens:N}` olarak cevirir ve
    gateway o alani okur.
    """

    def setUp(self):
        import sync_opencode
        self.v = sync_opencode.thinking_variants()

    def test_off_variant_disables_thinking(self):
        self.assertEqual(self.v["off"], {"thinking": {"type": "disabled"}})

    def test_enabled_variants_use_budget_tokens(self):
        for lvl in ("low", "medium", "high"):
            with self.subTest(level=lvl):
                th = self.v[lvl]["thinking"]
                self.assertEqual(th["type"], "enabled")
                self.assertIsInstance(th["budgetTokens"], int)
                self.assertGreater(th["budgetTokens"], 0)

    def test_budgets_increase_with_level(self):
        self.assertLess(self.v["low"]["thinking"]["budgetTokens"],
                        self.v["medium"]["thinking"]["budgetTokens"])
        self.assertLess(self.v["medium"]["thinking"]["budgetTokens"],
                        self.v["high"]["thinking"]["budgetTokens"])

    def test_every_variant_is_honoured_by_gateway(self):
        """Varyant degerleri gateway'de beklenen 'effort' seviyesine donusmeli.

        Artik budget -> effort cevrisi var (saglayici effort'a daha cok
        uyuyor: effort=xhigh 1003 vs max_tokens=24576 870 karakter).
        """
        M = "stealth/space-bunny-alpha"
        self.assertIsNone(gateway._reasoning_config(
            {"thinking": self.v["off"]["thinking"]}, 32000, M))
        beklenen = {"low": "low", "medium": "medium",
                    "high": "high", "max": "xhigh"}
        for lvl, eff in beklenen.items():
            with self.subTest(level=lvl):
                self.assertEqual(
                    gateway._reasoning_config(
                        {"thinking": self.v[lvl]["thinking"]}, 32000, M),
                    {"effort": eff})

    def test_camelcase_budget_also_accepted(self):
        """Bazi SDK'lar budgetTokens (camelCase) gonderiyor."""
        self.assertEqual(
            gateway._reasoning_config(
                {"thinking": {"type": "enabled", "budgetTokens": 8192}}, 32000),
            {"effort": "low"})


class TestPerModelEffort(unittest.TestCase):
    """Model basina thinking ayari: opencode'da model secerken effort secmek."""

    M = "stealth/space-bunny-alpha"
    F = "liquid/lfm-2.5-2.6b:free"

    def setUp(self):
        self._effort = gateway.CONFIG.get("reasoning_effort")
        self._per = gateway.CONFIG.get("reasoning_effort_by_model")

    def tearDown(self):
        for k, v in (("reasoning_effort", self._effort),
                     ("reasoning_effort_by_model", self._per)):
            if v is None:
                gateway.CONFIG.pop(k, None)
            else:
                gateway.CONFIG[k] = v

    def test_model_specific_setting_beats_global(self):
        gateway.CONFIG["reasoning_effort"] = "low"
        gateway.CONFIG["reasoning_effort_by_model"] = {self.M: "high", self.F: "off"}
        self.assertEqual(gateway._reasoning_config({}, 32000, self.M), {"effort": "high"})
        self.assertIsNone(gateway._reasoning_config({}, 32000, self.F))

    def test_model_without_entry_uses_global(self):
        gateway.CONFIG["reasoning_effort"] = "medium"
        gateway.CONFIG["reasoning_effort_by_model"] = {self.F: "off"}
        self.assertEqual(gateway._reasoning_config({}, 32000, self.M), {"effort": "medium"})

    def test_per_model_off_is_respected(self):
        gateway.CONFIG["reasoning_effort"] = "high"
        gateway.CONFIG["reasoning_effort_by_model"] = {self.F: "off"}
        self.assertIsNone(gateway._reasoning_config({}, 32000, self.F))

    def test_per_model_numeric_uses_max_tokens(self):
        gateway.CONFIG.pop("reasoning_effort", None)
        gateway.CONFIG["reasoning_effort_by_model"] = {self.M: "8192"}
        self.assertEqual(gateway._reasoning_config({}, 32000, self.M),
                         {"max_tokens": 8192})

    def test_normalize_effort_aliases(self):
        self.assertEqual(gateway._normalize_effort("off"), "off")
        self.assertEqual(gateway._normalize_effort(""), "off")
        self.assertEqual(gateway._normalize_effort("max"), "high")
        self.assertEqual(gateway._normalize_effort("min"), "minimal")
        self.assertEqual(gateway._normalize_effort("med"), "medium")
        self.assertEqual(gateway._normalize_effort("tokens:8192"), "8192")
        self.assertEqual(gateway._normalize_effort("HIGH"), "high")

    def test_normalize_effort_rejects_garbage(self):
        import unittest as _u
        for bad in ("turberk", "tokens:abc", "tokens:0", "-5"):
            with self.subTest(value=bad):
                with self.assertRaises(Exception):
                    gateway._normalize_effort(bad)

    def test_model_list_is_capped(self):
        lst = gateway._model_list()
        self.assertLessEqual(len(lst), 40)
        self.assertIn(gateway.get_active_model(), lst)

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
