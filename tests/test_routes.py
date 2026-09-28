# -*- coding: utf-8 -*-
"""HTTP yuzeyi testleri (httpx ASGITransport).

NOT: fastapi.testclient.TestClient bu ortamda calismiyor
(starlette/httpx surum uyumsuzlugu: Client.__init__() got an unexpected keyword
argument 'app'), bu yuzden ASGI uygulamasi dogrudan surulur.

Guvenlik: gateway anahtari sadece BU SURECIN BELLEGINDE okunur, ne ekrana basilir
ne de diske yazilir. Dis yuzeye (127.0.0.1:8756) giden canli testler gateway
kapaliysa kendini atlar (skip). config.json'a YAZILMAZ: kalici ayar degistiren
endpoint'lerde save_config mocklanir.
"""
import asyncio
import copy
import json
import unittest
from unittest import mock

import httpx

import gateway

CANLI_URL = "http://127.0.0.1:8756"


def run(coro):
    return asyncio.run(coro)


class RouteTest(unittest.IsolatedAsyncioTestCase):
    """ASGI dogrudan surulur; upstreame hicbir cagri gitmez."""

    async def req(self, method, url, **kw):
        transport = httpx.ASGITransport(app=gateway.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test",
                                     timeout=10) as ac:
            return await ac.request(method, url, **kw)

    def key(self):
        return {"x-api-key": gateway.GATEWAY_KEY}


class TestHelloRoute(RouteTest):
    """Oncelik 3: /api/hello 404 donerse istemci gateway'i ulasilamaz saniyor."""

    async def test_get_ok(self):
        r = await self.req("GET", "/api/hello")
        self.assertEqual(r.status_code, 200)
        j = r.json()
        self.assertTrue(j["ok"])
        self.assertEqual(j["service"], "ox-gateway")
        self.assertIn(j["mode"], ("1", "2"))

    async def test_head_ok(self):
        r = await self.req("HEAD", "/api/hello")
        self.assertEqual(r.status_code, 200)

    async def test_head_has_no_body(self):
        r = await self.req("HEAD", "/api/hello")
        self.assertEqual(r.content, b"")

    async def test_post_not_allowed(self):
        r = await self.req("POST", "/api/hello", json={})
        self.assertEqual(r.status_code, 405)


class TestDashboardRoute(RouteTest):
    async def test_root_served(self):
        r = await self.req("GET", "/")
        self.assertEqual(r.status_code, 200)


class TestConnRoute(RouteTest):
    async def test_conn_shape(self):
        r = await self.req("GET", "/api/conn")
        self.assertEqual(r.status_code, 200)
        j = r.json()
        self.assertIn("gateway_api_key", j)      # varligi; DEGERI ASLA basilmaz
        self.assertIn("model", j)
        self.assertIn("active_mode", j)
        self.assertEqual(j["active_mode"], gateway.get_active_mode())
        self.assertIsInstance(j["provider_models"], dict)

    async def test_conn_key_matches_process_key(self):
        r = await self.req("GET", "/api/conn")
        self.assertEqual(r.json()["gateway_api_key"], gateway.GATEWAY_KEY)

    async def test_conn_base_urls(self):
        j = (await self.req("GET", "/api/conn")).json()
        self.assertEqual(j["base_url"], "http://127.0.0.1:8756/v1")
        self.assertEqual(j["anthropic_base_url"], "http://127.0.0.1:8756")

    async def test_providers_route(self):
        r = await self.req("GET", "/api/providers")
        self.assertEqual(r.status_code, 200)
        j = r.json()
        self.assertIn(j["active_mode"], ("1", "2"))
        self.assertEqual(set(j["modes"]), {"1", "2"})


class TestStatsRoute(RouteTest):
    async def test_stats_shape(self):
        r = await self.req("GET", "/api/stats")
        self.assertEqual(r.status_code, 200)
        j = r.json()
        for alan in ("model", "active_mode", "provider", "uptime_s", "total_keys",
                     "active_keys", "keys", "modes"):
            self.assertIn(alan, j)
        self.assertIsInstance(j["total_keys"], int)
        self.assertIn(j["active_mode"], ("1", "2"))

    async def test_stats_per_mode(self):
        j = (await self.req("GET", "/api/stats?mode=2")).json()
        self.assertEqual(j["mode"], "2")
        self.assertEqual(set(j["modes"]), {"1", "2"})

    async def test_stats_bad_mode_falls_back(self):
        j = (await self.req("GET", "/api/stats?mode=99")).json()
        self.assertEqual(j["mode"], gateway.get_active_mode())

    async def test_keys_route(self):
        r = await self.req("GET", "/keys")
        self.assertEqual(r.status_code, 200)
        self.assertIn("keys", r.json())


class TestDiagRoute(RouteTest):
    async def test_diag_shape(self):
        r = await self.req("GET", "/api/diag")
        self.assertEqual(r.status_code, 200)
        j = r.json()
        for alan in ("turns", "empty_turns", "truncated_turns", "last", "model"):
            self.assertIn(alan, j)

    async def test_diag_reset_restores_state(self):
        eski = dict(gateway.DIAG)
        try:
            gateway.DIAG["turns"] = 7
            gateway.DIAG["empty_turns"] = 3
            gateway.DIAG["truncated_turns"] = 2
            r = await self.req("POST", "/api/diag/reset")
            self.assertEqual(r.status_code, 200)
            self.assertTrue(r.json()["reset"])
            self.assertEqual(gateway.DIAG["turns"], 0)
            self.assertEqual(gateway.DIAG["empty_turns"], 0)
            self.assertIsNone(gateway.DIAG["last"])
        finally:
            gateway.DIAG.clear()
            gateway.DIAG.update(eski)


class TestCountTokensRoute(RouteTest):
    """Oncelik 4: count_tokens auth'su; endpoint 404 donerse SDK hic cevap almaz."""

    async def test_wrong_key_401(self):
        r = await self.req("POST", "/v1/messages/count_tokens",
                           json={"model": "test", "messages": [
                               {"role": "user", "content": "selam"}]},
                           headers={"x-api-key": "yanlis-anahtar"})
        self.assertEqual(r.status_code, 401)

    async def test_missing_key_401(self):
        r = await self.req("POST", "/v1/messages/count_tokens",
                           json={"model": "test", "messages": [
                               {"role": "user", "content": "selam"}]})
        self.assertEqual(r.status_code, 401)

    async def test_query_string_key_rejected(self):
        r = await self.req("POST", "/v1/messages/count_tokens?api_key=gizli",
                           json={"messages": []})
        self.assertEqual(r.status_code, 401)

    async def test_bearer_token_accepted(self):
        r = await self.req("POST", "/v1/messages/count_tokens", json={"messages": []},
                           headers={"authorization": f"Bearer {gateway.GATEWAY_KEY}"})
        self.assertEqual(r.status_code, 200)

    async def test_valid_key_counts_text(self):
        r = await self.req("POST", "/v1/messages/count_tokens",
                           json={"model": "test", "messages": [
                               {"role": "user", "content": "selam"}]},
                           headers=self.key())
        self.assertEqual(r.status_code, 200)
        j = r.json()
        self.assertIn("input_tokens", j)
        self.assertGreater(j["input_tokens"], 0)
        self.assertEqual(j["input_tokens"], gateway._count_request_tokens(
            {"model": "test", "messages": [{"role": "user", "content": "selam"}]}))

    async def test_image_costs_about_1600(self):
        r = await self.req("POST", "/v1/messages/count_tokens",
                           json={"messages": [{"role": "user", "content": [
                               {"type": "image", "source": {"type": "base64",
                                                             "data": "A" * 500}}]}]},
                           headers=self.key())
        self.assertGreaterEqual(r.json()["input_tokens"], 1600)

    async def test_tools_counted(self):
        az = await self.req("POST", "/v1/messages/count_tokens",
                            json={"messages": [{"role": "user", "content": "x"}]},
                            headers=self.key())
        cok = await self.req("POST", "/v1/messages/count_tokens",
                             json={"messages": [{"role": "user", "content": "x"}],
                                   "tools": [{"name": "get_weather", "description": "hava",
                                              "input_schema": {"type": "object"}}]},
                             headers=self.key())
        self.assertGreater(cok.json()["input_tokens"], az.json()["input_tokens"])

    async def test_invalid_json_400(self):
        r = await self.req("POST", "/v1/messages/count_tokens", content=b"bozuk",
                           headers={**self.key(), "content-type": "application/json"})
        self.assertEqual(r.status_code, 400)

    async def test_root_alias_also_works(self):
        r = await self.req("POST", "/messages/count_tokens", json={"messages": []},
                           headers=self.key())
        self.assertEqual(r.status_code, 200)


class TestProxyAuth(RouteTest):
    async def test_messages_requires_key(self):
        r = await self.req("POST", "/v1/messages", json={"model": "test", "messages": [
            {"role": "user", "content": "selam"}]})
        self.assertEqual(r.status_code, 401)

    async def test_messages_wrong_key_401(self):
        r = await self.req("POST", "/v1/messages", json={"model": "test", "messages": []},
                           headers={"x-api-key": "yanlis"})
        self.assertEqual(r.status_code, 401)

    async def test_messages_alias_requires_key(self):
        r = await self.req("POST", "/messages", json={"messages": []})
        self.assertEqual(r.status_code, 401)

    async def test_chat_completions_requires_key(self):
        r = await self.req("POST", "/v1/chat/completions",
                           json={"model": "test", "messages": [
                               {"role": "user", "content": "selam"}]})
        self.assertEqual(r.status_code, 401)

    async def test_chat_completions_wrong_key_401(self):
        r = await self.req("POST", "/v1/chat/completions",
                           json={"model": "test", "messages": [
                               {"role": "user", "content": "selam"}]},
                           headers={"authorization": "Bearer yanlis"})
        self.assertEqual(r.status_code, 401)

    async def test_chat_completions_body_validated_before_upstream(self):
        """Gecersiz govde 422: upstream'e cikmadan hata."""
        r = await self.req("POST", "/v1/chat/completions", json={"mesajlar": []},
                           headers=self.key())
        self.assertEqual(r.status_code, 422)

    async def test_models_requires_key(self):
        r = await self.req("GET", "/v1/models")
        self.assertEqual(r.status_code, 401)

    async def test_management_endpoints_open(self):
        for yol in ("/api/hello", "/api/conn", "/api/stats", "/api/diag", "/keys"):
            with self.subTest(yol=yol):
                self.assertEqual((await self.req("GET", yol)).status_code, 200)


class ConfigGuardMixin:
    """Kalici ayar degistiren endpoint'lerde config.json'a DOKUNULMAZ."""

    def setUp(self):
        self._cfg = copy.deepcopy(gateway.CONFIG)
        self._pool = gateway.POOL
        self.addCleanup(self._restore)

    def _restore(self):
        gateway.CONFIG.clear()
        gateway.CONFIG.update(copy.deepcopy(self._cfg))
        gateway.POOL = self._pool


class TestModeRoutes(ConfigGuardMixin, RouteTest):
    async def test_invalid_mode_400(self):
        for yol in ("/mode/set", "/provider/set"):
            with self.subTest(yol=yol):
                r = await self.req("POST", yol, json={"mode": "3"})
                self.assertEqual(r.status_code, 400)

    async def test_missing_field_422(self):
        r = await self.req("POST", "/mode/set", json={})
        self.assertEqual(r.status_code, 422)

    async def test_set_mode_updates_config_without_writing_disk(self):
        hedef = "2" if gateway.get_active_mode() == "1" else "1"
        kaydedilen: list = []
        with mock.patch.object(gateway, "load_config", return_value=dict(gateway.CONFIG)), \
             mock.patch.object(gateway, "save_config", side_effect=kaydedilen.append):
            r = await self.req("POST", "/mode/set", json={"mode": hedef})
        self.assertEqual(r.status_code, 200)
        j = r.json()
        self.assertTrue(j["set"])
        self.assertEqual(j["active_mode"], hedef)
        self.assertEqual(gateway.get_active_mode(), hedef)
        self.assertEqual(len(kaydedilen), 1, "config bir kez kaydedilmeli")
        self.assertEqual(kaydedilen[0]["active_mode"], hedef)
        self.assertIn("model", j)
        self.assertEqual(gateway.POOL, gateway.POOLS[hedef])

    async def test_provider_set_alias_same_behaviour(self):
        hedef = "2" if gateway.get_active_mode() == "1" else "1"
        with mock.patch.object(gateway, "load_config", return_value=dict(gateway.CONFIG)), \
             mock.patch.object(gateway, "save_config") as sc:
            r = await self.req("POST", "/provider/set", json={"mode": hedef})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["active_mode"], hedef)
        sc.assert_called_once()

    async def test_mode_set_syncs_model(self):
        hedef = "2" if gateway.get_active_mode() == "1" else "1"
        beklenen = (gateway.CONFIG.get("provider_models") or {}).get(hedef)
        if not beklenen:
            self.skipTest("config'de provider_models eksik")
        with mock.patch.object(gateway, "load_config", return_value=dict(gateway.CONFIG)), \
             mock.patch.object(gateway, "save_config"):
            r = await self.req("POST", "/mode/set", json={"mode": hedef})
        self.assertEqual(r.json()["model"], beklenen)

    async def test_protocol_set_validation(self):
        r = await self.req("POST", "/protocol/set", json={"protocol": "ftp"})
        self.assertEqual(r.status_code, 400)


class TestMiscRoutes(RouteTest):
    async def test_unknown_path_404(self):
        self.assertEqual((await self.req("GET", "/api/boyle-bir-yol-yok")).status_code, 404)

    async def test_model_set_validation(self):
        self.assertEqual((await self.req("POST", "/model/set", json={"model": " "})).status_code,
                         400)
        self.assertEqual((await self.req("POST", "/model/set", json={"mode": "9", "model": "m"})
                          ).status_code, 400)

    async def test_rotate_requires_explicit_call(self):
        """Anahtar sadece /api/rotate ile degisir; testlerde cagrilmiyor."""
        r = await self.req("GET", "/api/rotate")
        self.assertEqual(r.status_code, 405)


class TestLiveGateway(unittest.TestCase):
    """CANLI testler: yalnizca calisan gateway'e karsi, upstream CAGRI YOK.
    Gateway kapaliysa skip edilir."""

    @classmethod
    def setUpClass(cls):
        try:
            r = httpx.get(f"{CANLI_URL}/api/conn", timeout=2)
            r.raise_for_status()
            cls.conn = r.json()
        except Exception as e:  # gateway ayakta degil
            raise unittest.SkipTest(f"gateway calismiyor ({CANLI_URL}): {type(e).__name__}")

    def headers(self):
        # anahtar sadece bellekte; ne basilir ne yazilir
        return {"x-api-key": self.conn["gateway_api_key"]}

    def test_hello_live(self):
        r = httpx.get(f"{CANLI_URL}/api/hello", timeout=5)
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.json()["ok"])

    def test_conn_live(self):
        r = httpx.get(f"{CANLI_URL}/api/conn", timeout=5)
        self.assertEqual(r.status_code, 200)
        self.assertIn("model", r.json())

    def test_stats_live(self):
        j = httpx.get(f"{CANLI_URL}/api/stats", timeout=5).json()
        self.assertIn(j["active_mode"], ("1", "2"))

    def test_count_tokens_live(self):
        r = httpx.post(f"{CANLI_URL}/v1/messages/count_tokens",
                       json={"model": "test", "messages": [
                           {"role": "user", "content": "selam"}]},
                       headers=self.headers(), timeout=10)
        self.assertEqual(r.status_code, 200)
        self.assertGreater(r.json()["input_tokens"], 0)

    def test_count_tokens_wrong_key_live(self):
        r = httpx.post(f"{CANLI_URL}/v1/messages/count_tokens",
                       json={"messages": []},
                       headers={"x-api-key": "yanlis-anahtar"}, timeout=10)
        self.assertEqual(r.status_code, 401)


if __name__ == "__main__":
    unittest.main()
