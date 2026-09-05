"""
Agen rekomendasi & analisis penjualan berbasis google-adk + tool-calling
(qwen3-agent:latest via Ollama, dibungkus LiteLlm). Sesi multi-turn: pertanyaan
lanjutan dalam satu run yang sama tetap ingat konteks sebelumnya (mis. "dari
rekomendasi tadi, mana yang paling murah?").

Dua sumber data punya peran berbeda:
  - katalog_produk.csv (+ koleksi ChromaDB "products"): pencarian semantik
    produk dan metadata terstruktur (harga, kategori, terjual).
  - transaction_data.csv: log transaksi mentah, dipakai live untuk hitung
    produk terlaris per segmen. Tidak ada kolom timestamp di data ini, jadi
    tidak bisa memfilter berdasarkan waktu (mis. "minggu ini").

Catatan implementasi:
  - qwen3-agent:latest punya mode "thinking" -- reasoning trace balik sebagai
    Part terpisah dengan thought=True, bukan tercampur di jawaban akhir. Kalau
    ini tidak difilter, hasil akhir akan berisi monolog internal model, bukan
    jawaban yang siap dibaca pengguna.
  - Tool yang butuh embedding (search_catalog, find_cross_sell_candidates)
    divalidasi dulu argumennya tidak kosong -- ollama.embeddings(prompt="")
    diam-diam mengembalikan vektor kosong dan bikin ChromaDB.query() crash.
  - ADK memanggil tool sinkron langsung di event loop asyncio kalau tidak
    dibungkus async (lihat google.adk.tools.function_tool._invoke_callable) --
    tool ini semua melakukan panggilan HTTP (Ollama) dan scan pandas atas
    transaction_data.csv (~1.5 juta baris), jadi kalau dipanggil langsung
    event loop-nya beku selama itu. Tiap tool karena itu dibungkus jadi
    `async def` tipis yang menjalankan implementasi sinkronnya lewat
    `asyncio.to_thread`, supaya panggilan tool lain / housekeeping ADK &
    LiteLLM (mis. logging worker-nya) tidak ikut macet menunggu.
  - Sesi disimpan lewat SqliteSessionService ke agent_sessions.db (bukan
    InMemorySessionService) supaya histori percakapan bertahan lintas restart
    query.py -- lihat infrastructure_agentic.md bagian Context.
  - before_tool_callback/after_tool_callback mencatat nama tool, argumen,
    durasi, dan status ke tool_calls.log -- observability minimal supaya
    tool yang lambat/gagal kelihatan tanpa harus baca traceback mentah
    (lihat infrastructure_agentic.md bagian Harness).
  - instruction Agent adalah callable (_build_instruction), bukan string
    statis -- menyisipkan jumlah produk katalog/index dan tanggal update
    ke system prompt tiap giliran (InstructionProvider, lihat
    infrastructure_agentic.md bagian Prompt).
  - Tiap giliran diakhiri verify_and_revise(): cek murah (regex angka) draft
    jawaban vs data tool MENTAH yang benar-benar dipanggil giliran itu
    (_current_turn_tool_outputs, diisi after_tool_callback) -- cuma eskalasi
    ke satu panggilan model tambahan kalau ada mismatch atau klaim tanpa
    angka verifiable. Opsi ini (hybrid) dipilih lewat benchmark nyata di
    benchmark_verify_loop.py, bukan LoopAgent tiap giliran (terlalu mahal di
    VRAM 8GB) -- lihat infrastructure_agentic.md bagian Graph #Bagian 2.
  - _warn_if_session_growing() mencatat peringatan sekali per ambang batas
    kalau riwayat sesi (SESSION_ID tetap sama selamanya) mulai mendekati
    batas num_ctx=8192 -- kanari murah, bukan solusi penuh (lihat
    infrastructure_agentic.md bagian Context).
"""

import asyncio
import os
import re
import time
from datetime import datetime

import chromadb
import ollama
import pandas as pd
from google.adk.agents import Agent
from google.adk.models.lite_llm import LiteLlm
from google.adk.runners import Runner
from google.genai import types

# SqliteSessionService belum di-re-export lewat google.adk.sessions.__init__
# (cuma InMemorySessionService, DatabaseSessionService, VertexAiSessionService yang
# publik) -- jadi diimpor langsung dari submodule-nya. Dipilih daripada
# DatabaseSessionService karena cukup pakai aiosqlite (sudah ter-install sebagai
# dependency google-adk), tidak perlu extra `sqlalchemy` tambahan.
from google.adk.sessions.sqlite_session_service import SqliteSessionService

os.environ.setdefault("OLLAMA_API_BASE", "http://localhost:11434")

MODEL_LLM = "ollama_chat/qwen3-agent:latest"
MODEL_EMBED = "nomic-embed-text"
KATALOG_CSV = "D:/agentic/chroma_db/katalog_produk.csv"
TRANSACTION_CSV = "D:/agentic/transaction_data/transaction_data.csv"
SESSION_DB_PATH = "D:/agentic/chroma_db/agent_sessions.db"
TOOL_LOG_PATH = "D:/agentic/agentic_dev/rag-env/tool_calls.log"

client = chromadb.PersistentClient(path="D:/agentic/chroma_db")
collection = client.get_collection(name="products")

katalog_df = pd.read_csv(KATALOG_CSV, dtype={"id": str})
katalog_df["terjual"] = pd.to_numeric(katalog_df["terjual"], errors="coerce").fillna(0)

_transactions_cache = None


def _load_transactions():
    global _transactions_cache
    if _transactions_cache is None:
        _transactions_cache = pd.read_csv(TRANSACTION_CSV)
    return _transactions_cache


def _embed(text: str) -> list[float]:
    try:
        return ollama.embeddings(model=MODEL_EMBED, prompt=text)["embedding"]
    except Exception:
        # Retry sekali -- Ollama lokal kadang lambat/timeout sesaat (mis. model baru
        # dimuat ke VRAM), bukan kegagalan permanen. Gagal lagi setelah retry berarti
        # masalah nyata (Ollama down dll), biarkan exception naik ke caller.
        time.sleep(1)
        return ollama.embeddings(model=MODEL_EMBED, prompt=text)["embedding"]


def _search_catalog_impl(query: str, category: str, max_price: float) -> str:
    if not query.strip():
        return "Query pencarian kosong, tidak bisa mencari produk."

    q_emb = _embed(query)
    res = collection.query(query_embeddings=[q_emb], n_results=15)
    ids = res["ids"][0]

    matches = katalog_df[katalog_df["id"].isin(ids)].copy()
    if max_price:
        matches = matches[matches["harga"] <= max_price]

    note = ""
    if category:
        by_category = matches[matches["kategori"].str.contains(category, case=False, na=False)]
        if by_category.empty and not matches.empty:
            # katalog_produk.csv["kategori"] (mis. "Sabun Mandi") dan
            # transaction_data.csv["product_category_name_lvl_0"] (mis. "Personal
            # Care") pakai vokabuler kategori yang berbeda -- model kadang minta
            # filter kategori dari vokabuler yang satunya (lihat
            # rag-setup-windows.md Known Issues). Daripada memaksakan 0 hasil
            # padahal pencarian semantiknya sendiri dapat kandidat relevan,
            # turunkan jadi catatan dan tetap tampilkan hasil tanpa filter kategori.
            note = f"(Kategori '{category}' tidak ketemu persis di antara hasil pencarian -- berikut hasil tanpa filter kategori)\n"
        else:
            matches = by_category

    if matches.empty:
        return "Tidak ada produk yang cocok."

    return note + "\n".join(
        f"- {r.nama} | kategori: {r.kategori} | harga: {r.harga:.0f} | terjual: {r.terjual:.0f}x"
        for r in matches.itertuples()
    )


async def search_catalog(query: str, category: str = "", max_price: float = 0) -> str:
    """
    Cari produk di katalog berdasarkan kemiripan makna dengan query.

    Args:
      query: Kata kunci atau deskripsi produk yang dicari.
      category: Filter nama kategori produk, kosongkan ("") jika tidak perlu difilter.
      max_price: Filter harga maksimum dalam rupiah, 0 jika tidak perlu difilter.

    Returns:
      str: Daftar produk yang cocok (nama, kategori, harga, jumlah terjual), satu produk per baris.
    """
    return await asyncio.to_thread(_search_catalog_impl, query, category, max_price)


def _get_top_sellers_impl(segment: str, top_n: int) -> str:
    if not segment.strip():
        return "Segmen kosong, tidak bisa mencari produk terlaris."

    df = _load_transactions()
    mask = df["product_name"].str.contains(segment, case=False, na=False) | df[
        "product_category_name_lvl_0"
    ].str.contains(segment, case=False, na=False)
    filtered = df[mask]

    if filtered.empty:
        return f"Tidak ada data penjualan untuk segmen '{segment}'."

    top = (
        filtered.groupby("product_name")
        .agg(
            terjual=("product_name", "count"),
            harga=("product_price", "first"),
            kategori=("product_category_name_lvl_0", "first"),
        )
        .sort_values("terjual", ascending=False)
        .head(top_n)
    )
    return "\n".join(
        f"- {name} | kategori: {row.kategori} | terjual: {row.terjual}x | harga: {row.harga:.0f}"
        for name, row in top.iterrows()
    )


async def get_top_sellers(segment: str, top_n: int = 5) -> str:
    """
    Cari produk terlaris berdasarkan data transaksi mentah, difilter menurut segmen.

    Args:
      segment: Kata kunci segmen/kategori produk, misalnya "sabun mandi" atau "minuman".
      top_n: Jumlah produk terlaris yang ingin ditampilkan.

    Returns:
      str: Daftar produk terlaris beserta kategori, jumlah kali terjual, dan harga, satu produk per baris.
    """
    return await asyncio.to_thread(_get_top_sellers_impl, segment, top_n)


def _find_cross_sell_candidates_impl(product_name: str, top_k: int) -> str:
    if not product_name.strip():
        return "Nama produk kosong, tidak bisa mencari produk mirip."

    q_emb = _embed(product_name)
    res = collection.query(query_embeddings=[q_emb], n_results=20)
    ids = res["ids"][0]

    candidates = katalog_df[katalog_df["id"].isin(ids)].copy()
    candidates = candidates[~candidates["nama"].str.lower().eq(product_name.lower())]
    candidates = candidates.sort_values("terjual", ascending=True).head(top_k)

    if candidates.empty:
        return "Tidak ditemukan produk mirip."

    return "\n".join(
        f"- {r.nama} | kategori: {r.kategori} | harga: {r.harga:.0f} | terjual: {r.terjual:.0f}x"
        for r in candidates.itertuples()
    )


async def find_cross_sell_candidates(product_name: str, top_k: int = 5) -> str:
    """
    Cari produk lain yang mirip dengan sebuah produk terlaris, diprioritaskan yang
    penjualannya masih rendah -- kandidat untuk dipromosikan bersama produk terlaris itu.

    Args:
      product_name: Nama produk terlaris sebagai acuan pencarian produk mirip.
      top_k: Jumlah kandidat produk mirip yang ingin ditampilkan.

    Returns:
      str: Daftar produk mirip berpenjualan rendah, beserta kategori, harga, dan jumlah terjual.
    """
    return await asyncio.to_thread(_find_cross_sell_candidates_impl, product_name, top_k)


def _get_top_categories_impl(top_n: int, terendah: bool) -> str:
    df = _load_transactions()
    counts = df["product_category_name_lvl_0"].value_counts()
    top = counts.sort_values(ascending=True).head(top_n) if terendah else counts.head(top_n)

    if top.empty:
        return "Tidak ada data kategori."

    return "\n".join(f"- {cat} | terjual: {count}x" for cat, count in top.items())


async def get_top_categories(top_n: int = 5, terendah: bool = False) -> str:
    """
    Cari kategori produk dengan jumlah penjualan tertinggi (atau terendah) dari seluruh
    data transaksi mentah. Pakai tool ini kalau pertanyaannya soal kategori secara umum
    (bukan segmen atau produk spesifik), misalnya "kategori apa yang paling laris" atau
    "kategori mana yang penjualannya paling sedikit".

    Args:
      top_n: Jumlah kategori yang ingin ditampilkan.
      terendah: True untuk mengurutkan dari kategori berpenjualan paling sedikit,
        False (default) untuk kategori terlaris.

    Returns:
      str: Daftar kategori beserta jumlah transaksi, satu kategori per baris.
    """
    return await asyncio.to_thread(_get_top_categories_impl, top_n, terendah)


def _get_worst_sellers_impl(segment: str, top_n: int) -> str:
    df = katalog_df
    if segment.strip():
        mask = df["nama"].str.contains(segment, case=False, na=False) | df["kategori"].str.contains(
            segment, case=False, na=False
        )
        df = df[mask]
        if df.empty:
            return f"Tidak ada produk untuk segmen '{segment}'."

    bottom = df.sort_values("terjual", ascending=True).head(top_n)
    return "\n".join(
        f"- {r.nama} | kategori: {r.kategori} | harga: {r.harga:.0f} | terjual: {r.terjual:.0f}x"
        for r in bottom.itertuples()
    )


async def get_worst_sellers(segment: str = "", top_n: int = 5) -> str:
    """
    Cari produk dari katalog dengan penjualan paling rendah, termasuk yang belum
    pernah terjual sama sekali (terjual=0) -- kandidat untuk didiskontinuasi,
    diturunkan harganya, atau ditinjau ulang penempatannya.

    Args:
      segment: Filter kata kunci nama produk atau kategori, kosongkan ("") untuk
        mencakup seluruh katalog.
      top_n: Jumlah produk berpenjualan terendah yang ingin ditampilkan.

    Returns:
      str: Daftar produk berpenjualan rendah beserta kategori, harga, dan jumlah terjual.
    """
    return await asyncio.to_thread(_get_worst_sellers_impl, segment, top_n)


def _get_category_assortment_impl(top_n: int, terendah: bool) -> str:
    counts = katalog_df.groupby("kategori").size().sort_values(ascending=terendah)
    top = counts.head(top_n)

    if top.empty:
        return "Tidak ada data kategori di katalog."

    return "\n".join(f"- {cat} | jumlah produk: {count}" for cat, count in top.items())


async def get_category_assortment(top_n: int = 5, terendah: bool = True) -> str:
    """
    Hitung jumlah produk per kategori di katalog -- berguna untuk melihat kategori
    dengan variasi produk paling sedikit (assortment gap, kandidat ekspansi katalog)
    atau paling banyak.

    Args:
      top_n: Jumlah kategori yang ingin ditampilkan.
      terendah: True (default) untuk kategori dengan jumlah produk paling sedikit,
        False untuk kategori dengan jumlah produk paling banyak.

    Returns:
      str: Daftar kategori beserta jumlah produknya, satu kategori per baris.
    """
    return await asyncio.to_thread(_get_category_assortment_impl, top_n, terendah)


def _get_price_range_impl(segment: str) -> str:
    if segment.strip():
        mask = katalog_df["nama"].str.contains(segment, case=False, na=False) | katalog_df[
            "kategori"
        ].str.contains(segment, case=False, na=False)
        matches = katalog_df[mask]
        label = f"Segmen '{segment}'"
    else:
        matches = katalog_df
        label = "Seluruh katalog"

    if matches.empty:
        return f"Tidak ada produk untuk segmen '{segment}'."

    return (
        f"{label}: {len(matches)} produk | "
        f"harga termurah: {matches['harga'].min():.0f} | "
        f"harga termahal: {matches['harga'].max():.0f} | "
        f"harga median: {matches['harga'].median():.0f}"
    )


async def get_price_range(segment: str = "") -> str:
    """
    Hitung rentang harga (termurah, termahal, median) produk pada suatu segmen atau
    kategori di katalog -- berguna untuk analisis positioning harga.

    Args:
      segment: Kata kunci nama produk atau kategori yang ingin dicek rentang harganya,
        kosongkan ("") untuk rentang harga seluruh katalog.

    Returns:
      str: Ringkasan jumlah produk dan rentang harganya untuk segmen tersebut.
    """
    return await asyncio.to_thread(_get_price_range_impl, segment)


SYSTEM_PROMPT = """Kamu adalah asisten analisis penjualan retail untuk toko online Alfagift.
Pengguna akan memberikan sebuah segmen produk (kategori atau kata kunci), atau
pertanyaan lanjutan yang merujuk ke percakapan sebelumnya.

Tugasmu, pilih tool sesuai jenis pertanyaan:
1. Kategori secara umum (bukan segmen/produk spesifik) -- "kategori apa yang
   paling laris" / "kategori mana yang penjualannya paling sedikit" -> tool
   get_top_categories (parameter terendah=True untuk yang paling sedikit).
   JANGAN pakai get_top_sellers untuk ini.
2. Produk terlaris di suatu segmen -> tool get_top_sellers (hasilnya sudah
   termasuk kategori tiap produk, tidak perlu tool tambahan untuk itu).
3. Produk paling tidak laku / belum pernah terjual, kandidat didiskontinuasi
   atau diturunkan harga -> tool get_worst_sellers.
4. Kategori dengan variasi produk paling sedikit/banyak di katalog (assortment
   gap) -> tool get_category_assortment.
5. Rentang harga (termurah/termahal/median) suatu segmen atau kategori ->
   tool get_price_range (kosongkan segment untuk rentang harga seluruh katalog).
6. Kalau pertanyaan menyebut "produk mirip/serupa dengan/untuk [X]" -- APAPUN
   embel-embel tambahannya (mis. "yang penjualannya rendah", "yang lebih
   laku", "untuk cross-sell") -- pakai tool find_cross_sell_candidates dengan
   product_name=X. Kalau X belum berupa nama produk konkret (mis. masih berupa
   nama kategori dari giliran sebelumnya), panggil dulu get_top_sellers atau
   get_top_categories untuk dapat satu nama produk konkret, LALU langsung
   panggil find_cross_sell_candidates dengan nama itu di giliran yang sama --
   jangan berhenti di tool pertama dan menyuruh pengguna mencari sendiri.
   JANGAN mengklaim suatu produk cocok untuk cross-sell tanpa benar-benar
   memanggil tool ini untuk membuktikannya.
7. Pakai tool search_catalog kalau butuh detail tambahan soal suatu produk.

Data transaksi TIDAK punya kolom waktu/tanggal -- kalau pengguna menanyakan hal
bertema waktu, termasuk yang tidak eksplisit menyebut satuan waktu (mis.
"penjualan minggu ini", "tren bulan lalu", "kategori apa yang lagi tren/naik
daun sekarang", "produk apa yang lagi hits/viral", "belakangan ini", "terkini"),
jangan memanggil tool apa pun untuk mengarang jawaban; katakan terus terang itu
tidak bisa dijawab dari data yang tersedia (data hanya berisi total akumulasi,
bukan tren dari waktu ke waktu). Boleh tawarkan alternatif yang benar-benar
bisa dijawab, mis. "kategori dengan penjualan tertinggi secara keseluruhan
(bukan tren terkini)", tapi jangan sajikan angka total sebagai kalau itu tren.

PENTING: kutip angka (harga, jumlah terjual, jumlah produk) PERSIS seperti yang
dikembalikan tool -- jangan menyusun ulang atau menaksir dari ingatan. Kalau butuh
angka yang belum ada di hasil tool manapun, panggil tool yang sesuai dulu, jangan
mengarang.

Susun rekomendasi akhir yang jelas dan actionable dalam Bahasa Indonesia: sebutkan
produk/kategori yang relevan dan alasannya singkat."""


def _build_instruction(context) -> str:
    """InstructionProvider: menyisipkan info katalog terkini ke system prompt
    tiap giliran, supaya angka jumlah produk/index tidak perlu ditulis ulang
    manual tiap kali katalog di-refresh (lihat infrastructure_agentic.md
    bagian Prompt)."""
    katalog_mtime = datetime.fromtimestamp(os.path.getmtime(KATALOG_CSV)).strftime("%Y-%m-%d")
    return (
        f"{SYSTEM_PROMPT}\n\n"
        f"Info katalog saat ini: {len(katalog_df)} produk terdaftar, "
        f"{collection.count()} di antaranya sudah ter-index untuk pencarian semantik "
        f"(search_catalog/find_cross_sell_candidates), data katalog terakhir diperbarui {katalog_mtime}."
    )


_tool_call_started_at: dict[int, float] = {}
# Dikosongkan di awal tiap ask(), diisi _log_after_tool dengan tool_response
# MENTAH (bukan cuma metadata di tool_calls.log) -- dipakai verify_and_revise()
# untuk mengecek jawaban akhir terhadap data tool yang SUNGGUHAN dipanggil
# giliran itu. Aman tanpa lock karena satu proses ini selalu memproses satu
# giliran sekaligus (tidak ada turn paralel).
_current_turn_tool_outputs: list[str] = []


def _log_before_tool(tool, args, tool_context) -> None:
    _tool_call_started_at[id(tool_context)] = time.monotonic()
    return None


def _log_after_tool(tool, args, tool_context, tool_response) -> None:
    started = _tool_call_started_at.pop(id(tool_context), None)
    duration = time.monotonic() - started if started is not None else -1.0
    ok = not (isinstance(tool_response, dict) and tool_response.get("error"))
    line = (
        f"{datetime.now().isoformat(timespec='seconds')} | {tool.name} | "
        f"args={args} | {duration:.2f}s | {'OK' if ok else 'ERROR'}\n"
    )
    with open(TOOL_LOG_PATH, "a", encoding="utf-8") as f:
        f.write(line)
    _current_turn_tool_outputs.append(str(tool_response))
    return None


_NUM_RE = re.compile(r"\d[\d.,]*")
# Marker list bernomor markdown ("1. **Produk**", "10) ...") di awal baris --
# HARUS dibuang dulu sebelum ekstraksi angka, kalau tidak "1", "2", "3" dari
# penomoran ikut dianggap "angka data" dan hampir tidak pernah cocok dengan
# tool_nums (yang formatnya bullet "-", bukan list bernomor) -- ditemukan
# lewat pengamatan langsung: baris "1."/"2." bikin verify_and_revise eskalasi
# di HAMPIR SETIAP giliran berformat list, bukan cuma ~8% seperti diproyeksikan
# benchmark, karena "1"/"2" dianggap mismatch terhadap data tool.
_LIST_MARKER_RE = re.compile(r"^\s*\d+[.)]\s+", re.MULTILINE)


def _extract_numbers(text: str) -> set[str]:
    text = _LIST_MARKER_RE.sub("", text)
    return {n.replace(".", "").replace(",", "") for n in _NUM_RE.findall(text)}


def _verify_and_revise_impl(draft_answer: str, tool_outputs: str, question: str = "") -> str:
    """Loop verifikasi akurasi (Graph Bagian 2, hybrid) -- lihat
    infrastructure_agentic.md untuk benchmark & alasan pemilihan opsi ini.

    Cek programatik (murah, ~0.02ms) dulu: kalau semua angka di draft ADA di
    data tool giliran ini ATAU di pertanyaan pengguna sendiri (lihat catatan
    "allowed_nums" di bawah), tidak perlu panggilan model kedua sama sekali.
    Cuma eskalasi ke satu panggilan model tambahan kalau draft punya angka
    yang TIDAK ketemu di data tool ATAU pertanyaan (mismatch nyata), atau
    draft tidak punya angka verifiable sama sekali (klaim kualitatif, mis.
    "cocok untuk cross-sell", yang tidak bisa dicek programatik).
    """
    if not tool_outputs.strip():
        return draft_answer  # tidak ada tool dipanggil giliran ini -- tidak ada yang bisa diverifikasi

    draft_nums = _extract_numbers(draft_answer)
    tool_nums = _extract_numbers(tool_outputs)
    # allowed_nums termasuk angka dari pertanyaan pengguna sendiri (mis. "5
    # produk terlaris", "harga di bawah 10000") -- ditemukan lewat pengamatan
    # langsung: jawaban WAJAR mengulang angka permintaan pengguna ("Berikut 5
    # produk...") yang bukan data dari tool, dan tanpa ini verify_and_revise
    # eskalasi di ~50% giliran (jauh di atas ~8% hasil benchmark), bukan
    # karena jawabannya salah, tapi karena pengecekannya terlalu ketat.
    allowed_nums = tool_nums | _extract_numbers(question)
    if draft_nums and draft_nums.issubset(allowed_nums):
        return draft_answer  # semua angka cocok -- selesai tanpa panggilan model kedua

    prompt = (
        "Kamu mengecek draft jawaban asisten penjualan terhadap data mentah dari tool "
        "yang benar-benar dipanggil di giliran ini. Kalau draft SUDAH akurat (semua angka "
        "dan klaim didukung data tool), ulangi draft itu PERSIS apa adanya, jangan diubah "
        "sedikit pun. Kalau ADA angka yang tidak cocok dengan data tool, atau klaim yang "
        "tidak didukung data tool (mis. menyebut suatu produk cocok untuk cross-sell "
        "padahal tool cross-sell tidak dipanggil/tidak mengembalikan produk itu), revisi "
        "jawabannya supaya akurat, HANYA berdasarkan data tool ini. Jangan tambahkan "
        "penjelasan soal proses pengecekan ini ke jawaban akhir -- keluarkan LANGSUNG "
        "jawaban akhirnya saja (yang asli atau yang sudah direvisi).\n\n"
        f"DATA TOOL (giliran ini):\n{tool_outputs[:3000]}\n\nDRAFT JAWABAN:\n{draft_answer[:2000]}"
    )
    try:
        resp = ollama.chat(model=MODEL_LLM.removeprefix("ollama_chat/"), messages=[{"role": "user", "content": prompt}])
        revised = resp["message"]["content"].strip() or draft_answer
    except Exception:
        return draft_answer  # verifikasi gagal -> tetap kirim draft asli, jangan bikin giliran gagal total

    # Model revisi juga probabilistik -- kadang gagal menangkap mismatch dalam
    # satu percobaan (diamati langsung: 1 dari beberapa percobaan hanya
    # mengulang draft yang salah apa adanya). Tidak retry lagi di sini (itu
    # menghilangkan penghematan biaya hybrid) -- cukup catat kalau masih
    # mismatch setelah revisi, supaya kegagalan ini kelihatan di tool_calls.log
    # alih-alih diam-diam terkirim ke pengguna.
    revised_nums = _extract_numbers(revised)
    if revised_nums and not revised_nums.issubset(allowed_nums):
        # Prefix "NOTE" (bukan format tool_calls.log biasa) supaya harness uji
        # (_new_tool_calls di test_agent_cases.py) tidak salah menganggap ini
        # sebagai tool baru bernama "verify_and_revise" yang ikut terpanggil.
        with open(TOOL_LOG_PATH, "a", encoding="utf-8") as f:
            f.write(
                f"NOTE {datetime.now().isoformat(timespec='seconds')} | verify_and_revise | "
                f"MASIH MISMATCH setelah revisi -- draft_nums={draft_nums} revised_nums={revised_nums} "
                f"allowed_nums={allowed_nums}\n"
            )
    return revised


async def verify_and_revise(draft_answer: str, tool_outputs: str, question: str = "") -> str:
    return await asyncio.to_thread(_verify_and_revise_impl, draft_answer, tool_outputs, question)


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

APP_NAME = "alfagift_sales_agent"
USER_ID = "cli_user"
SESSION_ID = "cli_session"

# SESSION_ID tetap sama selamanya (satu sesi abadi, bukan multi-sesi) -- ADK
# defaultnya mengirim SELURUH riwayat sesi ke model tiap giliran
# (google/adk/flows/llm_flows/contents.py, include_contents="default"), tidak
# ada windowing/summarization bawaan. Kanari murah ini cuma memperingatkan
# kalau riwayat mulai besar; solusi sesungguhnya (windowing/summarization atau
# desain multi-sesi) sengaja ditunda sampai ini benar-benar jadi masalah nyata
# -- lihat infrastructure_agentic.md bagian Context.
_SESSION_SIZE_WARN_THRESHOLDS = [60, 120, 240, 480, 960]
_warned_session_size_thresholds: set[int] = set()


def _warn_if_session_growing(session) -> None:
    n = len(session.events)
    for threshold in _SESSION_SIZE_WARN_THRESHOLDS:
        if n >= threshold and threshold not in _warned_session_size_thresholds:
            _warned_session_size_thresholds.add(threshold)
            print(
                f"[Catatan: sesi ini sudah berisi {n} event percakapan. SESSION_ID "
                f"tetap sama selamanya, jadi riwayat penuh ini dikirim ke model "
                f"tiap giliran (num_ctx=8192) -- kalau model mulai terasa \"lupa\" "
                f"konteks awal atau muncul error soal context length, ini "
                f"penyebabnya. Lihat infrastructure_agentic.md bagian Context.]\n"
            )


async def ask(runner, query, user_id=USER_ID, session_id=SESSION_ID):
    _current_turn_tool_outputs.clear()
    content = types.Content(role="user", parts=[types.Part(text=query)])
    final_text = "(tidak ada respons)"
    async for event in runner.run_async(user_id=user_id, session_id=session_id, new_message=content):
        if event.is_final_response() and event.content and event.content.parts:
            texts = [p.text for p in event.content.parts if p.text and not p.thought]
            if texts:
                final_text = "\n".join(texts)
    tool_outputs = "\n".join(_current_turn_tool_outputs)
    return await verify_and_revise(final_text, tool_outputs, query)


async def main():
    session_service = SqliteSessionService(db_path=SESSION_DB_PATH)
    existing_session = await session_service.get_session(
        app_name=APP_NAME, user_id=USER_ID, session_id=SESSION_ID
    )
    if existing_session is None:
        await session_service.create_session(app_name=APP_NAME, user_id=USER_ID, session_id=SESSION_ID)
        print("Agen rekomendasi siap (sesi baru). Ketik 'keluar' untuk berhenti.")
    else:
        print("Agen rekomendasi siap -- melanjutkan sesi sebelumnya. Ketik 'keluar' untuk berhenti.")
    runner = Runner(agent=root_agent, app_name=APP_NAME, session_service=session_service)

    print("Percakapan ini multi-turn -- pertanyaan lanjutan tetap ingat konteks sebelumnya,")
    print("dan sekarang tersimpan di agent_sessions.db, jadi tetap ada walau program direstart.\n")

    while True:
        user_input = input("Anda: ").strip()
        if not user_input or user_input.lower() in ("keluar", "exit", "quit"):
            break
        try:
            response = await ask(runner, user_input)
        except asyncio.CancelledError:
            # Ctrl+C sekali membatalkan task ini (lihat asyncio.runners.Runner._on_sigint)
            # tanpa langsung mematikan proses -- tangkap di sini supaya cuma giliran ini
            # yang gugur, bukan seluruh sesi. uncancel() mengembalikan status task supaya
            # cancel-scope internal (mis. asyncio.timeout() di litellm/ADK) tidak keliru
            # menganggap task ini masih dalam proses dibatalkan di giliran berikutnya.
            task = asyncio.current_task()
            if task is not None:
                task.uncancel()
            print("\n[Dibatalkan (Ctrl+C) -- sesi tetap lanjut. Tekan Ctrl+C dua kali cepat atau ketik 'keluar' untuk benar-benar berhenti.]\n")
            continue
        except Exception as e:
            print(f"\n[Turn ini gagal: {e!r} -- sesi tetap lanjut, coba lagi atau tanya hal lain.]\n")
            continue
        print(f"\nAgen: {response}\n")
        current_session = await session_service.get_session(app_name=APP_NAME, user_id=USER_ID, session_id=SESSION_ID)
        if current_session is not None:
            _warn_if_session_growing(current_session)


if __name__ == "__main__":
    asyncio.run(main())
