"""Smoke test manual (spec bagian 7 langkah 3c, spec bagian 10): ulangi
persis MT01-MT03, baca tool_calls.log level-root untuk memastikan root
BENAR-BENAR menyusun ulang referensi lintas-giliran ("dari situ",
"kategori itu") jadi nilai konkret di 'request' yang dikirim ke specialist
-- specialist TIDAK melihat riwayat percakapan sama sekali (spec bagian
10), jadi kalau root gagal resolve, giliran ke-2 akan salah jawab/menolak.
Prasyarat: Ollama jalan di localhost:11434.
Jalankan: ./Scripts/python.exe smoke_test_context_handoff.py
"""

import asyncio
import json

from query import TOOL_LOG_PATH
from test_agent_cases import CASES, run_case

TARGET_CASE_IDS = {"MT01", "MT02", "MT03"}
_SPECIALIST_NAMES = {"kategori_specialist", "produk_specialist"}


def _count_lines(path):
    try:
        with open(path, encoding="utf-8") as f:
            return sum(1 for _ in f)
    except FileNotFoundError:
        return 0


async def main():
    targets = [c for c in CASES if c[0] in TARGET_CASE_IDS]
    assert len(targets) == len(TARGET_CASE_IDS), (
        f"FAIL: sebagian case_id tidak ketemu di CASES: "
        f"{TARGET_CASE_IDS - {c[0] for c in targets}}"
    )

    for case_id, tool_area, questions, expected_tools, note in targets:
        before = _count_lines(TOOL_LOG_PATH)
        turns = await run_case(f"context_smoke_{case_id}", questions)
        with open(TOOL_LOG_PATH, encoding="utf-8") as f:
            new_lines = f.readlines()[before:]
        root_lines = [l for l in new_lines if any(name in l for name in _SPECIALIST_NAMES)]

        print(f"\n=== {case_id} -- {note} ===")
        for q, turn in zip(questions, turns):
            print(f"  Q: {q}")
            print(f"  A: {turn['answer'][:250]}")
        print("  Baris tool_calls.log level-root (request yang dikirim ke specialist):")
        for line in root_lines:
            print("   ", line.rstrip())
        print(
            "  REVIEW MANUAL: baca 'args={...}' di atas -- apakah request untuk giliran ke-2 "
            "(kalau ada) sudah berisi nama produk/kategori KONKRET, bukan 'itu'/'situ'/'tadi' mentah?"
        )

    print("\nSELESAI -- kalau ada request yang masih mengandung referensi mentah, itu berarti "
          "opsi A (spec bagian 10) tidak cukup andal untuk kasus itu -- pertimbangkan opsi B "
          "(state-forwarding) yang sudah dicatat di spec.")


if __name__ == "__main__":
    asyncio.run(main())
