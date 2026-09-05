"""
Benchmark (throwaway, not part of the suite) -- mengukur biaya nyata 3 opsi
loop verifikasi akurasi (Graph Bagian 2) di hardware ini, supaya keputusan
programatik vs LoopAgent vs hybrid didasarkan pada angka nyata, bukan
perkiraan generik dari dokumentasi.

Diukur:
  1. Latensi single-pass (baseline, tanpa verifikasi) -- lewat root_agent asli.
  2. Latensi satu panggilan kritik model kedua (simulasi LoopAgent) -- panggilan
     ollama.chat() langsung ke model yang sama, prompt pendek: cocokkan angka
     di draft jawaban dengan output tool.
  3. Biaya cek programatik (regex ekstrak angka + set comparison) -- murni CPU,
     tanpa panggilan model.
  4. VRAM sebelum/sesudah panggilan kritik -- untuk verifikasi tidak ada
     model swap/thrashing (kritik pakai model yang sama, bukan model kedua).
  5. Rasio giliran yang BUTUH kritik (jawaban tanpa angka sama sekali, mis.
     klaim kualitatif "cocok untuk cross-sell") dari 104 kasus nyata di
     test_report.jsonl -- estimasi seberapa sering opsi hybrid harus eskalasi.

Cara pakai:
    ./Scripts/python.exe benchmark_verify_loop.py
"""
import asyncio
import json
import re
import subprocess
import time

import ollama

from query import APP_NAME, MODEL_LLM, TOOL_LOG_PATH, root_agent
from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from google.genai import types

TEST_USER = "bench_user"
REPORT_PATH = "D:/agentic/agentic_dev/rag-env/test_report.jsonl"

# Pertanyaan representatif: single-tool sederhana, multi-tool/compound, dan
# satu yang jawabannya biasanya kualitatif (butuh eskalasi di opsi hybrid).
BENCH_QUESTIONS = [
    ("simple", "produk terlaris kategori Minuman"),
    ("simple", "cari sabun mandi"),
    ("compound", "cari sabun mandi murah lalu kasih tau juga produk cross-sell yang cocok"),
    ("compound", "kategori mana yang paling sedikit terjual, lalu rekomendasi produk mirip yang lebih laku"),
]


def _gpu_snapshot():
    try:
        out = subprocess.run(
            ["nvidia-smi", "--query-gpu=memory.used,utilization.gpu", "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=5,
        )
        used_mb, util_pct = out.stdout.strip().split(",")
        return int(used_mb.strip()), int(util_pct.strip())
    except Exception:
        return None, None


NUM_RE = re.compile(r"\d[\d.,]*")


def _extract_numbers(text: str) -> set[str]:
    return {n.replace(".", "").replace(",", "") for n in NUM_RE.findall(text)}


def _programmatic_check(draft_answer: str, tool_raw_output: str) -> bool:
    draft_nums = _extract_numbers(draft_answer)
    tool_nums = _extract_numbers(tool_raw_output)
    if not draft_nums:
        return True  # tidak ada angka untuk dicek -- lolos programatik (butuh eskalasi kalau hybrid)
    return draft_nums.issubset(tool_nums)


async def run_single_pass(session_service, session_id, question):
    runner = Runner(agent=root_agent, app_name=APP_NAME, session_service=session_service)
    content = types.Content(role="user", parts=[types.Part(text=question)])
    started = time.monotonic()
    final_text = ""
    async for event in runner.run_async(user_id=TEST_USER, session_id=session_id, new_message=content):
        if event.is_final_response() and event.content and event.content.parts:
            texts = [p.text for p in event.content.parts if p.text and not p.thought]
            if texts:
                final_text = "\n".join(texts)
    return final_text, time.monotonic() - started


def run_critique_pass(draft_answer: str, tool_context: str) -> tuple[str, float]:
    prompt = (
        "Kamu adalah pengecek akurasi. Berikut draft jawaban dari asisten penjualan, "
        "dan data mentah dari tool yang dipanggil. Cek apakah SEMUA angka (harga, "
        "jumlah terjual, jumlah produk) di draft jawaban benar-benar ada di data tool. "
        "Jawab singkat: 'OK' kalau semua cocok, atau 'TIDAK COCOK: <penjelasan>' kalau ada angka "
        "yang tidak cocok/dikarang.\n\n"
        f"DATA TOOL:\n{tool_context[:2000]}\n\nDRAFT JAWABAN:\n{draft_answer[:1500]}"
    )
    started = time.monotonic()
    resp = ollama.chat(model=MODEL_LLM.replace("ollama_chat/", ""), messages=[{"role": "user", "content": prompt}])
    duration = time.monotonic() - started
    return resp["message"]["content"], duration


async def main():
    print("=== 1. Baseline single-pass latency (root_agent asli) ===")
    session_service = InMemorySessionService()
    single_pass_durations = []
    samples = []
    for i, (kind, q) in enumerate(BENCH_QUESTIONS):
        sid = f"bench_{i}"
        await session_service.create_session(app_name=APP_NAME, user_id=TEST_USER, session_id=sid)
        before_lines = sum(1 for _ in open(TOOL_LOG_PATH, encoding="utf-8"))
        answer, dur = await run_single_pass(session_service, sid, q)
        with open(TOOL_LOG_PATH, encoding="utf-8") as f:
            new_lines = f.readlines()[before_lines:]
        tool_output_ctx = " | ".join(l.strip() for l in new_lines)
        single_pass_durations.append(dur)
        samples.append((kind, q, answer, tool_output_ctx))
        print(f"  [{kind}] {q!r} -> {dur:.1f}s, {len(new_lines)} tool call(s)")
    avg_single = sum(single_pass_durations) / len(single_pass_durations)
    print(f"  Rata-rata single-pass: {avg_single:.1f}s\n")

    print("=== 2. Simulasi kritik model kedua (LoopAgent-style) ===")
    gpu_before_mb, gpu_before_util = _gpu_snapshot()
    critique_durations = []
    for kind, q, answer, tool_ctx in samples:
        critique_text, dur = run_critique_pass(answer, tool_ctx)
        critique_durations.append(dur)
        print(f"  [{kind}] kritik untuk {q!r} -> {dur:.1f}s | verdict: {critique_text[:80].strip()!r}")
    gpu_after_mb, gpu_after_util = _gpu_snapshot()
    avg_critique = sum(critique_durations) / len(critique_durations)
    print(f"  Rata-rata kritik: {avg_critique:.1f}s")
    print(f"  VRAM sebelum: {gpu_before_mb} MiB ({gpu_before_util}% util) | sesudah: {gpu_after_mb} MiB ({gpu_after_util}% util)\n")

    print("=== 3. Biaya cek programatik (CPU murni, regex + set) ===")
    t0 = time.perf_counter()
    N = 1000
    for _, _, answer, tool_ctx in samples:
        for _ in range(N):
            _programmatic_check(answer, tool_ctx)
    total = time.perf_counter() - t0
    per_call_ms = (total / (N * len(samples))) * 1000
    print(f"  {per_call_ms:.4f} ms/panggilan (rata-rata {N * len(samples)} panggilan)\n")

    print("=== 4. Estimasi rasio eskalasi (dari 104 kasus nyata, test_report.jsonl) ===")
    try:
        with open(REPORT_PATH, encoding="utf-8") as f:
            rows = [json.loads(l) for l in f]
        total_turns = 0
        no_number_turns = 0
        for r in rows:
            for t in r["turns"]:
                total_turns += 1
                if not _extract_numbers(t["answer"]):
                    no_number_turns += 1
        escalation_rate = no_number_turns / total_turns if total_turns else 0
        print(f"  {no_number_turns}/{total_turns} giliran ({escalation_rate*100:.1f}%) tidak punya angka sama "
              f"sekali di jawaban akhir -- ini proxy kasar untuk giliran yang akan butuh eskalasi ke kritik model kedua di opsi hybrid.")
    except FileNotFoundError:
        escalation_rate = None
        print("  test_report.jsonl tidak ditemukan, lewati.")
    print()

    print("=== 5. Proyeksi rata-rata latensi per giliran per opsi ===")
    print(f"  Programatik saja      : {avg_single:.1f}s + ~0s     = ~{avg_single:.1f}s/giliran")
    print(f"  LoopAgent tiap giliran: {avg_single:.1f}s + {avg_critique:.1f}s   = ~{avg_single + avg_critique:.1f}s/giliran (SELALU)")
    if escalation_rate is not None:
        hybrid_avg = avg_single + escalation_rate * avg_critique
        print(f"  Hybrid (eskalasi {escalation_rate*100:.0f}%)  : {avg_single:.1f}s + {escalation_rate*100:.0f}%*{avg_critique:.1f}s = ~{hybrid_avg:.1f}s/giliran rata-rata")


if __name__ == "__main__":
    asyncio.run(main())
