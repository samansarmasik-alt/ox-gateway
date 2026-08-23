# -*- coding: utf-8 -*-
"""
ox-gateway client: Sub-agent'leri tek satirda acmak icin kolay arayuz.

Ornek:
    from agents import SubAgent, run_parallel

    agent = SubAgent(system="Sen bir arastirmaci agentsin.")
    print(agent.run("Kuantum bilgisayari tek cumlede anlat"))

    results = run_parallel([
        ("Sen bir sairsin.", "Yapay zeka hakkinda 2 satir siir yaz"),
        ("Sen bir matematikci.", "17*23 kac, adim adim"),
        ("Sen bir cevirisin.", "hello world -> Turkce"),
    ])
    for r in results:
        print(r["index"], r["output"])
"""
import httpx

GATEWAY = "http://127.0.0.1:8756"


class SubAgent:
    def __init__(self, system: str = "You are a helpful sub-agent.",
                 temperature: float | None = None, max_tokens: int | None = None):
        self.system = system
        self.temperature = temperature
        self.max_tokens = max_tokens

    def run(self, task: str) -> str:
        body = {"system": self.system, "task": task}
        if self.temperature is not None:
            body["temperature"] = self.temperature
        if self.max_tokens is not None:
            body["max_tokens"] = self.max_tokens
        r = httpx.post(f"{GATEWAY}/agent/run", json=body, timeout=180)
        r.raise_for_status()
        return r.json()["output"]


def run_parallel(agent_specs: list[tuple[str, str]], max_concurrency: int = 8) -> list[dict]:
    """[(system, task), ...] listesini paralel calistirir."""
    body = {
        "agents": [
            {"system": s, "task": t} for s, t in agent_specs
        ],
        "max_concurrency": max_concurrency,
    }
    r = httpx.post(f"{GATEWAY}/agent/parallel", json=body, timeout=300)
    r.raise_for_status()
    return r.json()["results"]


def chat(messages: list[dict], temperature: float | None = None) -> str:
    """OpenAI-uyumlu chat: [{'role': 'user', 'content': '...'}]"""
    body: dict = {"messages": messages}
    if temperature is not None:
        body["temperature"] = temperature
    r = httpx.post(f"{GATEWAY}/v1/chat/completions", json=body, timeout=180)
    r.raise_for_status()
    return r.json()["choices"][0]["message"]["content"]


def add_key(key: str) -> dict:
    return httpx.post(f"{GATEWAY}/keys/add", json={"key": key}).json()


def remove_key(key: str) -> dict:
    return httpx.post(f"{GATEWAY}/keys/remove", json={"key": key}).json()


def keys_status() -> dict:
    return httpx.get(f"{GATEWAY}/keys").json()


if __name__ == "__main__":
    print("Key durumu:", keys_status())
    agent = SubAgent(system="Kisa ve ozlu cevap ver.")
    print("\n[tek agent]", agent.run("Merhaba! Kimsin?"))

    print("\n[paralel 3 agent]")
    for r in run_parallel([
        ("Sen bir sairsin.", "Yapay zeka hakkinda 2 satir siir yaz"),
        ("Sen bir matematikci.", "17*23 kac?"),
        ("Sen bir cevirisin.", "'hello world' cumlesini Turkceye cevir"),
    ]):
        mark = "OK" if r["ok"] else "HATA"
        print(f"  agent-{r['index']} [{mark}]: {r.get('output', r.get('error'))[:200]}")
