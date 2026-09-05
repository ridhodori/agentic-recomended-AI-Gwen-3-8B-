"""Smoke test manual (spec bagian 7 langkah 2): 1 pertanyaan per specialist
lewat root_agent ASLI (bukan dummy), konfirmasi lewat tool_calls.log bahwa
baris level-root (nama specialist) dan baris level-specialist (nama tool
data asli) sama-sama tercatat dan terpisah dengan benar.
Prasyarat: Ollama jalan di localhost:11434 dengan qwen3-agent:latest +
nomic-embed-text ter-pull.
Jalankan: ./Scripts/python.exe smoke_test_specialists.py
"""

import asyncio

from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService

from query import APP_NAME, TOOL_LOG_PATH, ask, root_agent

_SPECIALIST_NAMES = {"kategori_specialist", "produk_specialist"}


def _count_lines(path):
    try:
        with open(path, encoding="utf-8") as f:
            return sum(1 for _ in f)
    except FileNotFoundError:
        return 0


async def _run_one(session_id, question):
    session_service = InMemorySessionService()
    await session_service.create_session(app_name=APP_NAME, user_id="smoke_user", session_id=session_id)
    runner = Runner(agent=root_agent, app_name=APP_NAME, session_service=session_service)
    before = _count_lines(TOOL_LOG_PATH)
    answer = await ask(runner, question, user_id="smoke_user", session_id=session_id)
    with open(TOOL_LOG_PATH, encoding="utf-8") as f:
        new_lines = f.readlines()[before:]
    return answer, new_lines


async def main():
    cases = [
        ("smoke_kategori", "kategori apa yang paling laris"),
        ("smoke_produk", "produk terlaris kategori Minuman"),
    ]
    for session_id, question in cases:
        answer, new_lines = await _run_one(session_id, question)
        print(f"\n=== {question} ===")
        print("Jawaban:", answer[:300])
        print("Baris tool_calls.log baru:")
        for line in new_lines:
            print(" ", line.rstrip())

        specialist_lines = [l for l in new_lines if any(name in l for name in _SPECIALIST_NAMES)]
        data_tool_lines = [
            l for l in new_lines
            if not l.startswith("NOTE") and not any(name in l for name in _SPECIALIST_NAMES)
        ]
        assert specialist_lines, f"FAIL: tidak ada baris log level-root (nama specialist) untuk: {question}"
        assert data_tool_lines, f"FAIL: tidak ada baris log level-specialist (tool data asli) untuk: {question}"
        print("PASS: log root (routing) dan log specialist (tool data) sama-sama tercatat, terpisah.")

    print("\nSELESAI -- baca output di atas untuk konfirmasi manual jawabannya masuk akal.")


if __name__ == "__main__":
    asyncio.run(main())
