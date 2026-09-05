"""Smoke test manual (spec bagian 7 langkah 3): ulangi persis kasus yang
memvalidasi perbaikan chaining & co-occurrence tool sebelum migrasi ini
(MT02, CS10, CP04, SC12, CP01), pakai root_agent + specialist BARU, cetak
tool yang benar-benar terpanggil untuk dibandingkan MANUAL dengan
test_report.jsonl (baseline sebelum migrasi) -- bukan hard assert, karena
perilaku model tetap probabilistik (sama seperti semua smoke test lain di
proyek ini).
Prasyarat: Ollama jalan di localhost:11434.
Jalankan: ./Scripts/python.exe smoke_test_chaining.py
"""

import asyncio
import json

from test_agent_cases import CASES, run_case

TARGET_CASE_IDS = {"MT02", "CS10", "CP04", "SC12", "CP01"}
BASELINE_PATH = "D:/agentic/agentic_dev/rag-env/test_report.jsonl"


def _load_baseline():
    baseline = {}
    try:
        with open(BASELINE_PATH, encoding="utf-8") as f:
            for line in f:
                rec = json.loads(line)
                baseline[rec["case_id"]] = rec
    except FileNotFoundError:
        pass
    return baseline


async def main():
    baseline = _load_baseline()
    targets = [c for c in CASES if c[0] in TARGET_CASE_IDS]
    assert len(targets) == len(TARGET_CASE_IDS), (
        f"FAIL: sebagian case_id tidak ketemu di CASES: "
        f"{TARGET_CASE_IDS - {c[0] for c in targets}}"
    )

    for case_id, tool_area, questions, expected_tools, note in targets:
        turns = await run_case(f"chain_smoke_{case_id}", questions)
        actual_tools = [t for turn in turns for t in turn["tools_called"]]
        before = baseline.get(case_id, {}).get("actual_tools", "(tidak ada baseline)")
        print(f"\n=== {case_id} ({tool_area}) -- {note or 'n/a'} ===")
        for q, turn in zip(questions, turns):
            print(f"  Q: {q}")
            print(f"  A: {turn['answer'][:200]}")
        print(f"  expected_tools (dari CASES)   : {expected_tools}")
        print(f"  actual_tools   (baseline lama) : {before}")
        print(f"  actual_tools   (SESUDAH migrasi): {actual_tools}")
        if sorted(actual_tools) == sorted(expected_tools):
            print("  PASS (cocok dengan expected_tools)")
        else:
            print("  REVIEW MANUAL: actual_tools beda dari expected_tools -- baca jawabannya, bukan otomatis regresi")

    print("\nSELESAI -- baca tiap kasus di atas, pastikan pola chaining (cross-sell dari kategori/produk) masih terjadi.")


if __name__ == "__main__":
    asyncio.run(main())
