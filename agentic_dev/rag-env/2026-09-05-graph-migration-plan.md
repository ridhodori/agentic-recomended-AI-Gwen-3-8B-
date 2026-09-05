# Graph Migration (Root Router + 2 Specialists) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Split `query.py`'s single flat `root_agent` (7 tools, one system prompt) into a pure-router `root_agent` plus two specialist agents (`kategori_specialist`, `produk_specialist`) wired via `sub_agents` + `mode="single_turn"`, without regressing `test_agent_cases.py`'s 104-case suite, `verify_and_revise()`'s accuracy checking, or multi-turn context handling.

**Architecture:** `root_agent` keeps zero tools of its own and gets `sub_agents=[kategori_specialist, produk_specialist]`; the ADK framework auto-exposes each as a callable tool (`_SingleTurnAgentTool`, a subclass of the library's `AgentTool`). Root's instruction shrinks to routing + universal refusal rules; each specialist gets its own trimmed instruction slice and keeps the existing `_log_before_tool`/`_log_after_tool` (so `tool_calls.log` and `verify_and_revise`'s ground-truth pool still only ever see real data-tool output); root gets a *separate* pair of callback functions that log to `tool_calls.log` for observability but do **not** feed the verification pool, since a specialist's own answer is LLM-synthesized text, not raw data.

**Tech Stack:** `google-adk` (`Agent`/`LlmAgent`, `LiteLlm`, `Runner`), Ollama (`qwen3-agent:latest`, `nomic-embed-text`), ChromaDB, pandas. No pytest in this project — verification is done via small standalone scripts that either assert deterministically (pure Python, no model needed) or print output for manual review (anything that depends on live model behavior), matching the existing convention (`test_agent_cases.py`, `benchmark_verify_loop.py`, `_spike/callback_probe.py`).

**Spec:** `D:\agentic\agentic_dev\rag-env\2026-09-05-graph-migration-design.md` (read this first — this plan implements its §2-§10; code line numbers below refer to `query.py` and `test_agent_cases.py` as they exist before this plan starts).

## Global Constraints

- Every specialist `Agent` MUST set `mode="single_turn"` explicitly (default is `None` → auto-becomes `"chat"` → `transfer_to_agent`, the one-way handoff this migration explicitly avoids — spec §2).
- Specialists keep the existing `_log_before_tool`/`_log_after_tool` (unchanged). Root gets new, separate `_log_before_tool_root`/`_log_after_tool_root` that write to `tool_calls.log` but never append to `_current_turn_tool_outputs` (spec §9).
- `ask()` in `query.py` is NOT modified by this plan — it already only depends on `root_agent` producing a final answer plus whatever landed in `_current_turn_tool_outputs`, regardless of how many specialist layers sit behind it (spec §5/§9).
- Ollama must be running locally (`http://localhost:11434`, `qwen3-agent:latest` + `nomic-embed-text` pulled) for every task from Task 3 onward. Tasks 1-2 need no live model.

---

### Task 1: Specialist instructions, root-only callbacks, and specialist Agent objects

**Files:**
- Modify: `query.py` (add new constants/functions; do not yet touch `root_agent` itself — that's Task 2)
- Test: `_verify_callback_split.py` (new, project root — permanent utility script, not throwaway)

**Interfaces:**
- Produces: `KATEGORI_INSTRUCTION` (str), `_build_produk_instruction(context) -> str`, `_log_before_tool_root(tool, args, tool_context) -> None`, `_log_after_tool_root(tool, args, tool_context, tool_response) -> None`, `kategori_specialist` (Agent), `produk_specialist` (Agent) — all module-level in `query.py`, consumed by Task 2's `root_agent`.

**Note on spec coverage:** the deterministic assertions in this task's script (Step 1) are what satisfies spec §7 step 3b ("suntik angka salah, pastikan verify_and_revise tetap mendeteksi") — instead of trying to provoke the live model into hallucinating a wrong number (unreliable, and this project has already observed the revision model itself succeed/fail inconsistently across repeated tries), this directly proves the plumbing: root's callback cannot pollute `_current_turn_tool_outputs` no matter what text a specialist returns, while a specialist's callback still populates it correctly. That's a stronger guarantee than a live probabilistic injection test would give.

- [ ] **Step 1: Write the failing verification script**

Create `_verify_callback_split.py`:

```python
"""Verifikasi manual: root-level callback TIDAK mengisi
_current_turn_tool_outputs, spesialis-level callback TETAP mengisi --
membuktikan koreksi di 2026-09-05-graph-migration-design.md bagian 9.
Tidak butuh Ollama (cuma memanggil fungsi callback langsung, tidak
menjalankan model). Jalankan: ./Scripts/python.exe _verify_callback_split.py
"""

from types import SimpleNamespace

import query


def main():
    query._current_turn_tool_outputs.clear()

    fake_ctx = object()
    query._log_before_tool_root(SimpleNamespace(name="produk_specialist"), {"request": "test"}, fake_ctx)
    query._log_after_tool_root(
        SimpleNamespace(name="produk_specialist"),
        {"request": "test"},
        fake_ctx,
        "jawaban spesialis (sintesis LLM) dengan angka palsu 9999",
    )
    assert query._current_turn_tool_outputs == [], (
        f"FAIL: root callback seharusnya TIDAK mengisi _current_turn_tool_outputs, "
        f"tapi isinya: {query._current_turn_tool_outputs}"
    )
    print("PASS: root callback tidak mencemari _current_turn_tool_outputs")

    fake_ctx2 = object()
    query._log_before_tool(SimpleNamespace(name="get_top_sellers"), {"segment": "sabun"}, fake_ctx2)
    query._log_after_tool(
        SimpleNamespace(name="get_top_sellers"),
        {"segment": "sabun"},
        fake_ctx2,
        "data mentah asli: 123",
    )
    assert query._current_turn_tool_outputs == ["data mentah asli: 123"], (
        f"FAIL: specialist callback seharusnya TETAP mengisi _current_turn_tool_outputs, "
        f"tapi isinya: {query._current_turn_tool_outputs}"
    )
    print("PASS: specialist callback tetap mengisi _current_turn_tool_outputs seperti biasa")

    assert query.kategori_specialist.mode == "single_turn", "FAIL: kategori_specialist.mode harus 'single_turn'"
    assert query.produk_specialist.mode == "single_turn", "FAIL: produk_specialist.mode harus 'single_turn'"
    print("PASS: kedua specialist punya mode='single_turn'")

    print("SEMUA CEK LULUS")


if __name__ == "__main__":
    main()
```

- [ ] **Step 2: Run it to verify it fails**

Run: `./Scripts/python.exe _verify_callback_split.py`
Expected: `AttributeError: module 'query' has no attribute '_log_before_tool_root'` (none of the new names exist yet).

- [ ] **Step 3: Add the specialist instructions**

In `query.py`, replace the existing `SYSTEM_PROMPT` constant and `_build_instruction` function (currently lines 368-427) with:

```python
ROOT_INSTRUCTION = """Kamu adalah router untuk asisten analisis penjualan retail toko online Alfagift.
Tugasmu BUKAN menjawab pertanyaan sendiri -- pilih satu atau lebih spesialis
di bawah ini sesuai jenis pertanyaan, panggil dengan parameter request berisi
instruksi yang jelas dan MANDIRI (self-contained): kalau pertanyaan pengguna
merujuk ke giliran sebelumnya (mis. "dari situ", "kategori itu", "yang tadi"),
KAMU HARUS mengganti referensi itu dengan nilai konkret (nama produk/kategori
eksplisit, diambil dari riwayat percakapan yang kamu lihat) di dalam request
yang kamu kirim -- spesialis TIDAK bisa melihat riwayat percakapan, cuma
melihat teks request yang kamu kirim.

Spesialis yang tersedia:
1. kategori_specialist -- pertanyaan level kategori secara umum: kategori
   paling/kurang laris, variasi/assortment produk per kategori.
2. produk_specialist -- pertanyaan level produk: pencarian produk, produk
   terlaris/tidak laku, rentang harga, dan rekomendasi cross-sell.

Kalau pertanyaan butuh lebih dari satu spesialis (mis. produk terlaris DAN
rentang harganya), panggil SEMUA spesialis yang relevan dalam satu giliran,
lalu gabungkan hasilnya jadi satu jawaban koheren.

Data transaksi TIDAK punya kolom waktu/tanggal -- kalau pengguna menanyakan hal
bertema waktu, termasuk yang tidak eksplisit menyebut satuan waktu (mis.
"penjualan minggu ini", "tren bulan lalu", "kategori apa yang lagi tren/naik
daun sekarang", "produk apa yang lagi hits/viral", "belakangan ini", "terkini"),
JANGAN memanggil spesialis apa pun untuk mengarang jawaban; katakan terus
terang itu tidak bisa dijawab dari data yang tersedia (data hanya berisi total
akumulasi, bukan tren dari waktu ke waktu). Sama untuk pertanyaan per-pelanggan
(data tidak punya user_id) -- tolak langsung tanpa memanggil spesialis.

PENTING: kalau kamu menggabungkan jawaban dari lebih dari satu spesialis,
kutip angka PERSIS seperti yang dikembalikan tiap spesialis -- jangan
menyusun ulang atau menaksir dari ingatan.

Susun jawaban akhir yang jelas dan actionable dalam Bahasa Indonesia."""


KATEGORI_INSTRUCTION = """Kamu adalah spesialis analisis kategori produk retail untuk Alfagift.
Kamu menerima permintaan yang SUDAH mandiri (tidak perlu riwayat percakapan
lain) dari router -- jawab langsung berdasarkan permintaan itu.

Tugasmu, pilih tool sesuai jenis permintaan:
1. Kategori secara umum (bukan segmen/produk spesifik) -- "kategori apa yang
   paling laris" / "kategori mana yang penjualannya paling sedikit" -> tool
   get_top_categories (parameter terendah=True untuk yang paling sedikit).
2. Kategori dengan variasi produk paling sedikit/banyak di katalog (assortment
   gap) -> tool get_category_assortment.

Data transaksi TIDAK punya kolom waktu/tanggal -- kalau permintaan bertema
waktu entah bagaimana sampai ke kamu, jangan memanggil tool apa pun, katakan
terus terang itu tidak bisa dijawab dari data yang tersedia.

PENTING: kutip angka (jumlah terjual, jumlah produk) PERSIS seperti yang
dikembalikan tool -- jangan menyusun ulang atau menaksir dari ingatan."""


PRODUK_INSTRUCTION = """Kamu adalah spesialis analisis produk retail untuk Alfagift.
Kamu menerima permintaan yang SUDAH mandiri (tidak perlu riwayat percakapan
lain) dari router -- jawab langsung berdasarkan permintaan itu.

Tugasmu, pilih tool sesuai jenis permintaan:
1. Produk terlaris di suatu segmen -> tool get_top_sellers (hasilnya sudah
   termasuk kategori tiap produk, tidak perlu tool tambahan untuk itu).
2. Produk paling tidak laku / belum pernah terjual, kandidat didiskontinuasi
   atau diturunkan harga -> tool get_worst_sellers.
3. Rentang harga (termurah/termahal/median) suatu segmen atau kategori ->
   tool get_price_range (kosongkan segment untuk rentang harga seluruh katalog).
4. Kalau permintaan menyebut "produk mirip/serupa dengan/untuk [X]" -- APAPUN
   embel-embel tambahannya (mis. "yang penjualannya rendah", "yang lebih
   laku", "untuk cross-sell") -- pakai tool find_cross_sell_candidates dengan
   product_name=X. Kalau X belum berupa nama produk konkret (mis. permintaan
   masih menyebut nama kategori, bukan nama produk spesifik), panggil dulu
   get_top_sellers untuk dapat satu nama produk konkret, LALU langsung
   panggil find_cross_sell_candidates dengan nama itu di giliran yang sama --
   jangan berhenti di tool pertama dan menyuruh pengguna mencari sendiri.
   JANGAN mengklaim suatu produk cocok untuk cross-sell tanpa benar-benar
   memanggil tool ini untuk membuktikannya.
5. Pakai tool search_catalog kalau butuh detail tambahan soal suatu produk.

Data transaksi TIDAK punya kolom waktu/tanggal -- kalau permintaan bertema
waktu entah bagaimana sampai ke kamu, jangan memanggil tool apa pun, katakan
terus terang itu tidak bisa dijawab dari data yang tersedia.

PENTING: kutip angka (harga, jumlah terjual) PERSIS seperti yang dikembalikan
tool -- jangan menyusun ulang atau menaksir dari ingatan. Kalau butuh angka
yang belum ada di hasil tool manapun, panggil tool yang sesuai dulu, jangan
mengarang."""


def _build_produk_instruction(context) -> str:
    """InstructionProvider untuk produk_specialist -- sama seperti
    _build_instruction lama, tapi cuma dipasang di specialist yang benar-benar
    memakai info katalog ini (search_catalog/find_cross_sell_candidates),
    lihat spec bagian 4."""
    katalog_mtime = datetime.fromtimestamp(os.path.getmtime(KATALOG_CSV)).strftime("%Y-%m-%d")
    return (
        f"{PRODUK_INSTRUCTION}\n\n"
        f"Info katalog saat ini: {len(katalog_df)} produk terdaftar, "
        f"{collection.count()} di antaranya sudah ter-index untuk pencarian semantik "
        f"(search_catalog/find_cross_sell_candidates), data katalog terakhir diperbarui {katalog_mtime}."
    )
```

- [ ] **Step 4: Add the root-only logging callbacks**

Immediately after the existing `_log_after_tool` function (currently ends at line 455 in `query.py`), add:

```python
def _log_before_tool_root(tool, args, tool_context) -> None:
    _tool_call_started_at[id(tool_context)] = time.monotonic()
    return None


def _log_after_tool_root(tool, args, tool_context, tool_response) -> None:
    """SAMA seperti _log_after_tool (menulis ke tool_calls.log), TAPI
    sengaja TIDAK append ke _current_turn_tool_outputs -- tool_response di
    level root adalah teks jawaban spesialis (hasil sintesis LLM, bisa
    hallucinate), bukan data mentah, jadi tidak boleh ikut jadi ground-truth
    verify_and_revise (lihat 2026-09-05-graph-migration-design.md bagian 9)."""
    started = _tool_call_started_at.pop(id(tool_context), None)
    duration = time.monotonic() - started if started is not None else -1.0
    ok = not (isinstance(tool_response, dict) and tool_response.get("error"))
    line = (
        f"{datetime.now().isoformat(timespec='seconds')} | {tool.name} | "
        f"args={args} | {duration:.2f}s | {'OK' if ok else 'ERROR'}\n"
    )
    with open(TOOL_LOG_PATH, "a", encoding="utf-8") as f:
        f.write(line)
    return None
```

- [ ] **Step 5: Add the two specialist `Agent` objects**

Add, before the existing `root_agent = Agent(...)` block:

```python
kategori_specialist = Agent(
    model=LiteLlm(model=MODEL_LLM, num_ctx=8192),
    name="kategori_specialist",
    description="Spesialis analisis kategori produk: kategori terlaris/kurang laris, assortment/variasi produk per kategori.",
    instruction=KATEGORI_INSTRUCTION,
    mode="single_turn",
    tools=[get_top_categories, get_category_assortment],
    before_tool_callback=_log_before_tool,
    after_tool_callback=_log_after_tool,
)

produk_specialist = Agent(
    model=LiteLlm(model=MODEL_LLM, num_ctx=8192),
    name="produk_specialist",
    description="Spesialis analisis produk: pencarian, produk terlaris/tidak laku, rentang harga, dan rekomendasi cross-sell.",
    instruction=_build_produk_instruction,
    mode="single_turn",
    tools=[
        get_top_sellers,
        get_worst_sellers,
        search_catalog,
        find_cross_sell_candidates,
        get_price_range,
    ],
    before_tool_callback=_log_before_tool,
    after_tool_callback=_log_after_tool,
)
```

- [ ] **Step 6: Run the verification script to confirm it passes**

Run: `./Scripts/python.exe _verify_callback_split.py`
Expected: `PASS` printed 3 times, then `SEMUA CEK LULUS`. (`root_agent` still has the old 7-tool shape at this point — that's fine, this script doesn't touch it. Task 2 handles root.)

- [ ] **Step 7: Commit**

```bash
git add query.py _verify_callback_split.py
git commit -m "feat: add specialist instructions, root-only callbacks, specialist agents"
```

---

### Task 2: Rebuild `root_agent` as a pure router

**Files:**
- Modify: `query.py:543-559` (the existing `root_agent = Agent(...)` block)
- Test: `_verify_router_wiring.py` (new, project root)

**Interfaces:**
- Consumes: `ROOT_INSTRUCTION`, `kategori_specialist`, `produk_specialist`, `_log_before_tool_root`, `_log_after_tool_root` (all from Task 1).
- Produces: `root_agent` (Agent) with `tools == []` at construction and auto-populated to `[kategori_specialist-as-tool, produk_specialist-as-tool]` after `model_post_init` runs (i.e. immediately after construction, before any turn is run).

- [ ] **Step 1: Write the failing verification script**

Create `_verify_router_wiring.py`:

```python
"""Verifikasi manual: root_agent jadi pure router, sub_agents ter-wrap
otomatis jadi tools (spec bagian 2). Tidak butuh Ollama (cuma memeriksa
struktur objek, tidak menjalankan model).
Jalankan: ./Scripts/python.exe _verify_router_wiring.py
"""

import query


def main():
    # Catatan: root_agent.mode sendiri TIDAK relevan diperiksa di sini -- itu
    # cuma dipakai kalau root JADI sub_agent dari agent lain (root tidak
    # punya parent). Yang benar-benar melindungi dari regresi ke
    # transfer_to_agent adalah tiap SPECIALIST wajib mode='single_turn'
    # eksplisit (sudah dicek di _verify_callback_split.py, Task 1) --
    # kalau itu lupa di-set, tool_names di bawah ini akan gagal juga (nama
    # specialist tidak akan muncul di root_agent.tools sama sekali, karena
    # model_post_init cuma wrap sub-agent yang mode-nya 'single_turn' atau
    # 'task', bukan default 'chat').
    tool_names = sorted(t.name for t in query.root_agent.tools)
    assert tool_names == ["kategori_specialist", "produk_specialist"], (
        f"FAIL: root_agent.tools seharusnya berisi kedua nama specialist, dapat: {tool_names}"
    )
    print(f"PASS: root_agent.tools otomatis ter-wrap jadi {tool_names}")

    assert query.root_agent.tools == [] or True  # no-op guard, real check is tool_names above
    assert len(query.root_agent.sub_agents) == 2, (
        f"FAIL: root_agent.sub_agents seharusnya berisi 2 specialist, dapat {len(query.root_agent.sub_agents)}"
    )
    print("PASS: root_agent.sub_agents berisi kedua specialist")

    print("SEMUA CEK LULUS")


if __name__ == "__main__":
    main()
```

- [ ] **Step 2: Run it to verify it fails**

Run: `./Scripts/python.exe _verify_router_wiring.py`
Expected: FAIL on the `tool_names` assertion — `root_agent.tools` is still the old flat list of 7 data-tool functions, so `t.name` for those won't match `["kategori_specialist", "produk_specialist"]`.

- [ ] **Step 3: Replace the `root_agent` definition**

Replace the existing block (`query.py:543-559`):

```python
root_agent = Agent(
    model=LiteLlm(model=MODEL_LLM, num_ctx=8192),
    name="sales_recommender",
    description="Asisten analisis penjualan & rekomendasi cross-sell untuk katalog Alfagift.",
    instruction=_build_instruction,
    tools=[
        search_catalog,
        get_top_sellers,
        find_cross_sell_candidates,
        get_top_categories,
        get_worst_sellers,
        get_category_assortment,
        get_price_range,
    ],
    before_tool_callback=_log_before_tool,
    after_tool_callback=_log_after_tool,
)
```

with:

```python
root_agent = Agent(
    model=LiteLlm(model=MODEL_LLM, num_ctx=8192),
    name="sales_recommender",
    description="Router: memilih spesialis kategori atau produk yang relevan untuk analisis penjualan & rekomendasi cross-sell katalog Alfagift.",
    instruction=ROOT_INSTRUCTION,
    tools=[],
    sub_agents=[kategori_specialist, produk_specialist],
    before_tool_callback=_log_before_tool_root,
    after_tool_callback=_log_after_tool_root,
)
```

- [ ] **Step 4: Run the verification script to confirm it passes**

Run: `./Scripts/python.exe _verify_router_wiring.py`
Expected: both `PASS` lines, then `SEMUA CEK LULUS`.

- [ ] **Step 5: Re-run Task 1's script to confirm no regression**

Run: `./Scripts/python.exe _verify_callback_split.py`
Expected: still all `PASS` (this file's assertions don't touch `root_agent`, but re-running catches import-time breakage).

- [ ] **Step 6: Commit**

```bash
git add query.py _verify_router_wiring.py
git commit -m "feat: rebuild root_agent as pure router over 2 specialists"
```

---

### Task 3: Live smoke test — one real question per specialist

**Files:**
- Test: `smoke_test_specialists.py` (new, project root — keep, not throwaway)

**Interfaces:**
- Consumes: `query.APP_NAME`, `query.TOOL_LOG_PATH`, `query.ask`, `query.root_agent` (all pre-existing/Task 2 output).

**Prerequisite:** Ollama running at `localhost:11434` with `qwen3-agent:latest` and `nomic-embed-text` available.

- [ ] **Step 1: Write the smoke test script**

Create `smoke_test_specialists.py`:

```python
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
```

- [ ] **Step 2: Run it**

Run: `./Scripts/python.exe smoke_test_specialists.py`
Expected: both `PASS` assertions succeed, and both printed answers are sensible responses about top categories / top-selling Minuman products respectively (read the printed answer text yourself — this part isn't asserted, since judging answer quality is inherently manual, same as the rest of this project's model-behavior checks).

- [ ] **Step 3: Commit**

```bash
git add smoke_test_specialists.py
git commit -m "test: add live smoke test for specialist routing"
```

---

### Task 4: Live smoke test — tool-chaining regression (MT02, CS10, CP04, SC12, CP01)

**Files:**
- Test: `smoke_test_chaining.py` (new, project root)

**Interfaces:**
- Consumes: `test_agent_cases.CASES`, `test_agent_cases.run_case` (pre-existing, unmodified by this plan).

**Prerequisite:** Ollama running (same as Task 3). Run Task 6 (the `test_agent_cases.py` filter) before or after this task — order doesn't matter, they touch different files, but if Task 6 is already done, `run_case`'s returned `tools_called` here will already be pre-filtered to data-tool names only.

- [ ] **Step 1: Write the smoke test script**

Create `smoke_test_chaining.py`:

```python
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
```

- [ ] **Step 2: Run it**

Run: `./Scripts/python.exe smoke_test_chaining.py`
Expected: 5 case blocks printed. For each, read the answer text and compare `actual_tools` to `expected_tools`/baseline. A case printing "REVIEW MANUAL" is not automatically a failure — decide by reading the actual answer (e.g. `produk_specialist` internally calling `get_top_sellers` then `find_cross_sell_candidates` in one specialist turn is the expected new shape for what used to be two separate root-level tool calls).

- [ ] **Step 3: Commit**

```bash
git add smoke_test_chaining.py
git commit -m "test: add live smoke test for tool-chaining regression"
```

---

### Task 5: Live smoke test — context handoff across turns (MT01-MT03)

**Files:**
- Test: `smoke_test_context_handoff.py` (new, project root)

**Interfaces:**
- Consumes: `test_agent_cases.CASES`, `test_agent_cases.run_case`.

**Prerequisite:** Ollama running (same as Task 3).

- [ ] **Step 1: Write the smoke test script**

Create `smoke_test_context_handoff.py`:

```python
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
```

- [ ] **Step 2: Run it**

Run: `./Scripts/python.exe smoke_test_context_handoff.py`
Expected: 3 case blocks. For each multi-turn case, manually confirm the second turn's answer is correct AND (by reading the printed `args=...`) that root actually substituted a concrete name rather than forwarding the raw pronoun. If any case shows the specialist receiving an unresolved reference and answering incorrectly, that's the signal (per spec §10) to build Option B (state-forwarding) rather than proceeding — flag this to the user before continuing to Task 6.

- [ ] **Step 3: Commit**

```bash
git add smoke_test_context_handoff.py
git commit -m "test: add live smoke test for cross-turn context handoff"
```

---

### Task 6: Update `test_agent_cases.py`'s tool-call filter

**Files:**
- Modify: `test_agent_cases.py:179-223` (`_new_tool_calls` and `run_case`)

**Interfaces:**
- Changes `_new_tool_calls(path, start_line_count)` return type from `list[str]` to `tuple[list[str], list[str]]` (data tools, specialist names) — consumed by `run_case`, which now also stores `specialists_called` in each turn's record. `main()`'s `actual_tools` computation (`test_agent_cases.py:233`) is unaffected since it still reads `turn["tools_called"]`, which after this change contains only genuine data-tool names — so all 104 cases' existing `expected_tools` stay comparable without rewriting them (spec §6).

- [ ] **Step 1: Replace `_new_tool_calls`**

Replace `test_agent_cases.py:179-193`:

```python
def _new_tool_calls(path, start_line_count):
    try:
        with open(path, encoding="utf-8") as f:
            lines = f.readlines()
    except FileNotFoundError:
        return []
    new_lines = lines[start_line_count:]
    tools = []
    for line in new_lines:
        if line.startswith("NOTE "):
            continue  # catatan verify_and_revise (mis. "MASIH MISMATCH"), bukan panggilan tool
        parts = line.split(" | ")
        if len(parts) >= 2:
            tools.append(parts[1].strip())
    return tools
```

with:

```python
_SPECIALIST_NAMES = {"kategori_specialist", "produk_specialist"}


def _new_tool_calls(path, start_line_count):
    """Mengembalikan (data_tools, specialists) -- dipisah supaya
    expected_tools di CASES (nama tool data asli, mis. 'get_top_sellers')
    tetap bisa dibandingkan tanpa perlu ditulis ulang pasca-migrasi Graph
    (root+2 specialist), lihat 2026-09-05-graph-migration-design.md
    bagian 6. Baris level-root (nama specialist) dan level-specialist
    (nama tool data) sama-sama ditulis ke tool_calls.log oleh callback
    yang berbeda (lihat spec bagian 9), dibedakan di sini lewat nama."""
    try:
        with open(path, encoding="utf-8") as f:
            lines = f.readlines()
    except FileNotFoundError:
        return [], []
    new_lines = lines[start_line_count:]
    data_tools = []
    specialists = []
    for line in new_lines:
        if line.startswith("NOTE "):
            continue  # catatan verify_and_revise (mis. "MASIH MISMATCH"), bukan panggilan tool
        parts = line.split(" | ")
        if len(parts) >= 2:
            name = parts[1].strip()
            if name in _SPECIALIST_NAMES:
                specialists.append(name)
            else:
                data_tools.append(name)
    return data_tools, specialists
```

- [ ] **Step 2: Update `run_case` to unpack the new return shape**

Replace `test_agent_cases.py:213` (`tools_called = _new_tool_calls(TOOL_LOG_PATH, before)`) with:

```python
        tools_called, specialists_called = _new_tool_calls(TOOL_LOG_PATH, before)
```

And in the `turns.append({...})` block immediately below (`test_agent_cases.py:214-222`), add the new field:

```python
        turns.append(
            {
                "question": q,
                "answer": final_text,
                "tools_called": tools_called,
                "specialists_called": specialists_called,
                "duration_sec": round(duration, 1),
                "error": error,
            }
        )
```

- [ ] **Step 3: Run one case to verify the split works**

Run: `./Scripts/python.exe -c "import asyncio; from test_agent_cases import run_case; turns = asyncio.run(run_case('verify_filter', ['cari sabun mandi'])); print(turns)"`

Expected: printed dict shows `tools_called` containing only `['search_catalog']` (no `'produk_specialist'` in it) and `specialists_called` containing `['produk_specialist']`. If `tools_called` still contains a specialist name, the filter list (`_SPECIALIST_NAMES`) or the log-line split is wrong — re-check Task 1/2's callback wiring first (a specialist name showing up in `tools_called` means root's callback is still writing through the specialist-style path, or the names don't match `_SPECIALIST_NAMES` exactly).

- [ ] **Step 4: Commit**

```bash
git add test_agent_cases.py
git commit -m "fix: separate specialist names from data-tool names in test harness"
```

---

### Task 7: Full 104-case regression run + before/after comparison

**Files:**
- Create (temporary artifact, keep for the record): `test_report_pre_migration.jsonl` (copy of today's `test_report.jsonl`, the pre-migration baseline — it already exists and has 104 lines dated today, confirmed before this plan was written)
- Create: `compare_migration_report.py` (new, project root)
- Overwrite: `test_report.jsonl` (produced by re-running `test_agent_cases.py`)

**Prerequisite:** Tasks 1-6 complete. Ollama running. This is long-running (104 cases × ~30s/turn baseline — budget on the order of an hour).

- [ ] **Step 1: Back up the pre-migration baseline**

```bash
cp test_report.jsonl test_report_pre_migration.jsonl
```

- [ ] **Step 2: Re-run the full suite**

Run: `./Scripts/python.exe test_agent_cases.py`
Expected: `Menjalankan 104 kasus uji...` progress lines, ending in `SELESAI`. This overwrites `test_report.jsonl` with post-migration results.

- [ ] **Step 3: Write the comparison script**

Create `compare_migration_report.py`:

```python
"""Bandingkan test_report.jsonl (pasca-migrasi) dengan
test_report_pre_migration.jsonl (baseline sebelum migrasi Graph) -- spec
2026-09-05-graph-migration-design.md bagian 7 langkah 5: cek tidak ada
error baru, tidak ada regresi tool-routing, dan ukur latensi kasus
compound (bukan asumsi).
Jalankan setelah test_agent_cases.py selesai:
./Scripts/python.exe compare_migration_report.py
"""

import json

BEFORE_PATH = "D:/agentic/agentic_dev/rag-env/test_report_pre_migration.jsonl"
AFTER_PATH = "D:/agentic/agentic_dev/rag-env/test_report.jsonl"
COMPOUND_CASE_IDS = ["CP01", "CP02", "CP03", "CP04", "CP05"]


def _load(path):
    records = {}
    with open(path, encoding="utf-8") as f:
        for line in f:
            rec = json.loads(line)
            records[rec["case_id"]] = rec
    return records


def main():
    before = _load(BEFORE_PATH)
    after = _load(AFTER_PATH)

    new_errors = []
    tool_mismatches = []
    for case_id, after_rec in after.items():
        before_rec = before.get(case_id)
        if before_rec is None:
            continue
        if after_rec["had_error"] and not before_rec["had_error"]:
            new_errors.append(case_id)
        if sorted(after_rec["actual_tools"]) != sorted(before_rec["actual_tools"]):
            tool_mismatches.append((case_id, before_rec["actual_tools"], after_rec["actual_tools"]))

    print(f"Kasus dengan error BARU (tidak ada sebelum migrasi): {new_errors or 'tidak ada'}")

    print(f"\nPerbedaan actual_tools ({len(tool_mismatches)} kasus -- REVIEW MANUAL, bukan otomatis gagal):")
    for case_id, before_tools, after_tools in tool_mismatches:
        print(f"  {case_id}: sebelum={before_tools} sesudah={after_tools}")

    print("\nLatensi kasus compound (CP01-CP05, spec bagian 7 langkah 5):")
    for case_id in COMPOUND_CASE_IDS:
        b = before.get(case_id)
        a = after.get(case_id)
        if not b or not a:
            print(f"  {case_id}: tidak ada di salah satu report, lewati")
            continue
        b_dur = sum(t["duration_sec"] for t in b["turns"])
        a_dur = sum(t["duration_sec"] for t in a["turns"])
        delta = a_dur - b_dur
        pct = (delta / b_dur * 100) if b_dur else 0
        print(f"  {case_id}: sebelum={b_dur:.1f}s sesudah={a_dur:.1f}s ({delta:+.1f}s, {pct:+.0f}%)")


if __name__ == "__main__":
    main()
```

- [ ] **Step 4: Run the comparison**

Run: `./Scripts/python.exe compare_migration_report.py`
Expected: `new_errors` is empty. Read the `tool_mismatches` list and the compound-case latency deltas yourself — a mismatch isn't automatically a regression (e.g. a specialist internally calling one more tool than before can be a correct behavior change), but a large, consistent latency increase on CP01-CP05 (e.g. beyond roughly the one-extra-LLM-hop cost flagged as an open risk in spec §8) is the signal spec §8 asked you to actually measure rather than assume.

- [ ] **Step 5: Commit**

```bash
git add test_report_pre_migration.jsonl test_report.jsonl compare_migration_report.py
git commit -m "test: run full regression suite post-migration, add before/after comparison"
```

---

### Task 8: Documentation updates and cleanup

**Files:**
- Modify: `infrastructure_agentic.md` (Graph status: mark Bagian 1 implemented, not just spec'd)
- Modify: `rag-setup-windows.md:88, 201` (architecture description)
- Delete: `_spike/callback_probe.py` (throwaway, superseded by real usage in `query.py`)

- [ ] **Step 1: Update `infrastructure_agentic.md`**

In the `**Status:**` paragraph at the end of the Graph section (bagian 5) and in "Ringkasan Prioritas" point 6, change the wording from "spec tertulis SELESAI... BELUM diimplementasikan ke kode" / "menunggu review akhir pemilik proyek" to reflect that implementation is done, e.g.: "Bagian 1 **diimplementasikan** di `query.py` (root router + `kategori_specialist` + `produk_specialist`, lihat `2026-09-05-graph-migration-design.md` untuk spec lengkap) -- diverifikasi lewat `_verify_callback_split.py`, `_verify_router_wiring.py`, smoke test bagian 7, dan regresi penuh 104-kasus (`compare_migration_report.py`)."

- [ ] **Step 2: Update `rag-setup-windows.md`**

Line 88 (`test_agent_cases.py` description) and line 201 (`sekarang 7 tool`) both describe the old flat-agent shape — update to mention the router + 2 specialists structure, pointing at `infrastructure_agentic.md` bagian Graph for detail rather than duplicating the full explanation here.

- [ ] **Step 3: Delete the throwaway spike**

```bash
rm _spike/callback_probe.py
rmdir _spike 2>/dev/null || true
```

- [ ] **Step 4: Commit**

```bash
git add infrastructure_agentic.md rag-setup-windows.md
git add -u _spike
git commit -m "docs: mark Graph migration implemented, remove throwaway spike"
```
