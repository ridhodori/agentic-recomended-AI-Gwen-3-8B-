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
  - root_agent menggunakan ROOT_INSTRUCTION (string statis) sebagai router,
    dan sub-agents (kategori_specialist, produk_specialist) di-wrap otomatis
    jadi tools oleh ADK model_post_init. produk_specialist menggunakan
    _build_produk_instruction (callable InstructionProvider) untuk menyisipkan
    info katalog terkini tiap giliran (lihat infrastructure_agentic.md bagian Prompt).
  - Tiap giliran diakhiri verify_and_revise(): cek murah (regex angka) draft
    jawaban vs data tool MENTAH yang benar-benar dipanggil giliran itu
    (_current_turn_tool_outputs, diisi after_tool_callback) DITAMBAH angka
    dari function_response tool data mentah di SELURUH riwayat sesi
    (_extract_session_tool_numbers) -- root sah menjawab dari data yang
    sudah diambil giliran sebelumnya tanpa panggil tool lagi, jadi tanpa
    riwayat ini pengulangan data nyata keliru ditandai karangan (lihat
    kasus nyata di infrastructure_agentic.md bagian Graph #4). Cuma
    eskalasi ke satu panggilan model tambahan kalau masih ada mismatch atau
    klaim tanpa angka verifiable. Opsi ini (hybrid) dipilih lewat benchmark
    nyata di benchmark_verify_loop.py, bukan LoopAgent tiap giliran (terlalu
    mahal di VRAM 8GB) -- lihat infrastructure_agentic.md bagian Graph
    #Bagian 2.
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
   paling/kurang laris, variasi/assortment produk per kategori. TIDAK
   PUNYA tool harga sama sekali.
2. produk_specialist -- pertanyaan level produk: pencarian produk, produk
   terlaris/tidak laku, rentang harga, dan rekomendasi cross-sell.

PENTING soal kata "kategori": kata ini muncul di DUA konteks berbeda --
(a) pertanyaan level-kategori MURNI (kategori_specialist), mis. "kategori
apa yang paling laris", TIDAK menyebut harga/produk spesifik sama sekali,
vs (b) kata "kategori" cuma dipakai sebagai PENUNJUK SEGMEN untuk
pertanyaan HARGA atau PRODUK, mis. "harga median kategori Keripik &
Kerupuk", "produk termahal di kategori Minuman" -- ini WAJIB ke
produk_specialist, karena cuma dia yang punya tool harga (get_price_range)
dan produk. Aturannya: kalau pertanyaan tentang HARGA, PRODUK, atau
CROSS-SELL, itu SELALU produk_specialist -- terlepas dari kata "kategori"
ikut disebut sebagai penunjuk segmen atau tidak.

Kalau pertanyaan butuh lebih dari satu spesialis (mis. produk terlaris DAN
rentang harganya), panggil SEMUA spesialis yang relevan dalam satu giliran,
lalu gabungkan hasilnya jadi satu jawaban koheren.

Beberapa pertanyaan dibungkus sebagai permintaan kreatif/strategis (mis.
"buatkan ide promo dari 5 produk terlaris", "rekomendasi strategi jualan
kategori X") padahal tetap BERGANTUNG pada fakta konkret (produk mana yang
terlaris, harga berapa, kategori mana). Framing "ide/rekomendasi/strategi"
BUKAN alasan untuk melewati spesialis -- kalau jawabanmu akan menyebut nama
produk/kategori/angka penjualan/harga tertentu, kamu WAJIB memanggil
spesialis dulu untuk data itu, baru menyusun ide di atasnya. Bagian yang
murni saranmu sendiri (mis. persentase diskon promo, kalimat marketing)
boleh kamu tambahkan, tapi tandai jelas sebagai saran -- jangan sampai
pembaca mengira itu berasal dari data penjualan asli.

Kalau kamu berpikir data TAMBAHAN (mis. kandidat cross-sell) akan
memperkuat jawaban, PANGGIL spesialis yang sesuai SEKARANG di giliran yang
sama -- JANGAN cuma menyebut nama tool/spesialis sebagai "langkah
selanjutnya" di jawaban akhir. Pengguna tidak bisa memanggil tool itu
sendiri, jadi kalimat seperti "gunakan find_cross_sell_candidates..." tidak
berguna baginya dan membocorkan detail implementasi internal -- kalau kamu
tidak memanggilnya sekarang, jangan sebut nama tool/spesialis itu sama
sekali di jawaban akhir.

Data transaksi TIDAK punya kolom waktu/tanggal -- kalau pengguna menanyakan hal
bertema waktu, termasuk yang tidak eksplisit menyebut satuan waktu (mis.
"penjualan minggu ini", "tren bulan lalu", "kategori apa yang lagi tren/naik
daun sekarang", "produk apa yang lagi hits/viral", "belakangan ini", "terkini"),
JANGAN memanggil spesialis apa pun untuk mengarang jawaban; katakan terus terang
itu tidak bisa dijawab dari data yang tersedia (data hanya berisi total
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
terus terang itu tidak bisa dijawab dari data yang tersedia. Sama untuk
permintaan per-pelanggan (data tidak punya user_id) -- tolak langsung tanpa
memanggil tool apa pun.

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
   memanggil tool ini untuk membuktikannya. Kandidat cross-sell HARUS
   produk LAIN yang penjualannya lebih rendah dari produk acuan (persis
   yang dikembalikan tool ini) -- JANGAN menyarankan produk acuan itu
   sendiri sebagai kandidat cross-sell-nya sendiri. Kalau tool ini
   benar-benar tidak mengembalikan kandidat, katakan itu terus terang
   ("tidak ditemukan kandidat cross-sell yang cocok"), jangan mengarang
   kandidat atau mengganti dengan produk acuannya sendiri.
5. Pakai tool search_catalog kalau butuh detail tambahan soal suatu produk.

Kalau permintaan meminta BEBERAPA hal sekaligus (mis. produk terlaris DAN
rentang harganya DAN rekomendasi cross-sell), pastikan jawaban akhir
BENAR-BENAR memuat hasil dari SETIAP tool yang kamu panggil -- jangan
diam-diam menghilangkan salah satu bagian yang diminta.

Data transaksi TIDAK punya kolom waktu/tanggal -- kalau permintaan bertema
waktu entah bagaimana sampai ke kamu, jangan memanggil tool apa pun, katakan
terus terang itu tidak bisa dijawab dari data yang tersedia. Sama untuk
permintaan per-pelanggan (data tidak punya user_id) -- tolak langsung tanpa
memanggil tool apa pun.

Kalau kamu berpikir data TAMBAHAN (mis. kandidat cross-sell) akan
memperkuat jawaban, PANGGIL tool yang sesuai SEKARANG di giliran yang sama
-- JANGAN cuma menyebut nama tool sebagai "langkah selanjutnya" di jawaban
akhir. Router yang meneruskan jawabanmu ke pengguna tidak bisa memanggil
tool itu sendiri, jadi kalimat seperti "gunakan find_cross_sell_candidates
..." tidak berguna dan membocorkan detail implementasi internal.

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


def _write_tool_log_line(tool, args, tool_context, tool_response) -> None:
    """Helper: Tulis baris log tool call ke tool_calls.log (durasi, status).
    Dipakai oleh _log_after_tool dan _log_after_tool_root."""
    started = _tool_call_started_at.pop(id(tool_context), None)
    duration = time.monotonic() - started if started is not None else -1.0
    ok = not (isinstance(tool_response, dict) and tool_response.get("error"))
    line = (
        f"{datetime.now().isoformat(timespec='seconds')} | {tool.name} | "
        f"args={args} | {duration:.2f}s | {'OK' if ok else 'ERROR'}\n"
    )
    with open(TOOL_LOG_PATH, "a", encoding="utf-8") as f:
        f.write(line)


def _log_after_tool(tool, args, tool_context, tool_response) -> None:
    _write_tool_log_line(tool, args, tool_context, tool_response)
    _current_turn_tool_outputs.append(str(tool_response))
    return None


def _log_before_tool_root(tool, args, tool_context) -> None:
    _tool_call_started_at[id(tool_context)] = time.monotonic()
    return None


def _log_after_tool_root(tool, args, tool_context, tool_response) -> None:
    """SAMA seperti _log_after_tool (menulis ke tool_calls.log), TAPI
    sengaja TIDAK append ke _current_turn_tool_outputs -- tool_response di
    level root adalah teks jawaban spesialis (hasil sintesis LLM, bisa
    hallucinate), bukan data mentah, jadi tidak boleh ikut jadi ground-truth
    verify_and_revise (lihat 2026-09-05-graph-migration-design.md bagian 9)."""
    _write_tool_log_line(tool, args, tool_context, tool_response)
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


# Ambang jumlah angka konkret "asing" (bukan dari pertanyaan pengguna) yang
# dianggap mencurigakan kalau NOL tool dipanggil giliran ini -- penolakan sah
# (tema waktu/per-pelanggan) tidak pernah menyebut angka spesifik sama
# sekali, sedangkan draft yang dikarang (lihat kasus nyata di
# infrastructure_agentic.md bagian Graph) menyebut belasan-puluhan angka
# (harga, persentase, jumlah terjual). 3 dipilih sebagai batas aman yang
# jelas di atas nol tapi jelas di bawah pola fabrikasi nyata yang teramati.
_SUSPICIOUS_NUM_THRESHOLD = 3

# Nama tool DATA MENTAH (bukan produk_specialist/kategori_specialist itu
# sendiri, yang jawabannya sintesis LLM dan bisa hallucinate -- lihat
# _log_after_tool_root) -- dipakai _extract_session_tool_numbers() untuk
# memfilter function_response mana di riwayat sesi yang boleh dianggap
# ground-truth.
_DATA_TOOL_NAMES = {
    "search_catalog",
    "get_top_sellers",
    "find_cross_sell_candidates",
    "get_top_categories",
    "get_worst_sellers",
    "get_category_assortment",
    "get_price_range",
}

# Ditemukan lewat laporan pengguna: root/spesialis kadang menutup jawaban
# dengan bagian "Tindakan Selanjutnya: Gunakan find_cross_sell_candidates
# ..." alih-alih benar-benar memanggilnya -- bocoran nama tool internal yang
# TIDAK BISA ditindaklanjuti pengguna (mereka tidak bisa memanggil tool
# sendiri), jadi murni pointless. ROOT_INSTRUCTION/PRODUK_INSTRUCTION sudah
# diminta untuk tidak melakukan ini, TAPI pola "instruksi teks tidak 100%
# diikuti model 8B lokal" sudah terbukti berulang di proyek ini (lihat root
# delegation gap, infrastructure_agentic.md bagian Graph #4) -- jaring
# pengaman deterministik di sini (buang seluruh bagian, bukan cuma baris
# yang menyebut nama tool) jauh lebih diandalkan daripada instruksi saja.
_TRAILING_METASECTION_RE = re.compile(
    r"\n{1,2}\**\s*(Tindakan Selanjutnya|Next Steps|Langkah Selanjutnya)\s*:?\s*\**\s*\n[\s\S]*\Z",
    re.IGNORECASE,
)


def _strip_trailing_meta_section(text: str) -> str:
    """Buang bagian akhir "Tindakan Selanjutnya"/"Next Steps" dari jawaban
    akhir kalau ada -- lihat catatan di _TRAILING_METASECTION_RE."""
    return _TRAILING_METASECTION_RE.sub("", text).rstrip()


# Untuk draft "kreatif" (mis. ide promo) yang MENAMBAHKAN angka baru secara
# sengaja (diskon %, harga bundel -- ROOT_INSTRUCTION mengizinkan ini) di
# atas data nyata, draft TETAP dianggap cukup berbasis data kalau sebagian
# angkanya match ke history_nums. Sempat dicoba deteksi lewat overlap KATA
# nama produk (bukan angka) supaya lebih tegas -- GAGAL lewat stress-test
# adversarial: kata generik penanda kategori (mis. "sabun", "mandi", "cair",
# "anti bakteri") muncul di HAMPIR SEMUA nama produk segmen yang sama, jadi
# draft yang brand & angkanya karangan TOTAL pun lolos cuma karena menyebut
# istilah kategori umum. Rasio angka NYATA (jauh lebih spesifik/acak
# daripada kata kategori, kecil kemungkinan cocok karena kebetulan) jauh
# lebih tahan terhadap celah itu -- lihat infrastructure_agentic.md.
_MIN_GROUNDED_NUM_MATCHES = 2
_MIN_GROUNDED_NUM_RATIO = 0.25


def _extract_session_tool_numbers(session) -> set[str]:
    """Angka dari SEMUA function_response tool data mentah di SELURUH riwayat
    sesi (bukan cuma giliran ini) -- root punya akses penuh ke riwayat sesi
    tiap giliran (ADK include_contents="default"), jadi WAJAR dan BENAR kalau
    root menjawab dari data yang sudah diambil giliran-giliran sebelumnya
    tanpa memanggil tool lagi. Ditemukan lewat investigasi laporan pengguna
    (giliran nol-tool-call yang ditolak _verify_and_revise_impl ternyata
    mengutip PERSIS angka dari get_top_sellers yang dipanggil ~24 jam
    sebelumnya di sesi abadi yang sama, dikonfirmasi lewat
    agent_sessions.db) -- tanpa ini, verify_and_revise salah menandai
    pengulangan data NYATA sebagai karangan, cuma karena tool-nya tidak
    dipanggil ULANG di giliran yang sama. Cuma function_response dari
    _DATA_TOOL_NAMES yang dihitung -- jawaban sintesis produk_specialist/
    kategori_specialist sendiri (dicatat di level root sebagai tool_response
    juga, lihat _log_after_tool_root) sengaja tidak ikut, itu tetap bisa
    hallucinate."""
    nums: set[str] = set()
    for event in session.events:
        content = getattr(event, "content", None)
        if not content or not content.parts:
            continue
        for part in content.parts:
            fr = getattr(part, "function_response", None)
            if fr is not None and getattr(fr, "name", None) in _DATA_TOOL_NAMES:
                nums |= _extract_numbers(str(fr.response))
    return nums


def _verify_and_revise_impl(
    draft_answer: str,
    tool_outputs: str,
    question: str = "",
    history_nums: frozenset[str] = frozenset(),
) -> str:
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
        # Nol tool dipanggil giliran ini -- BISA berarti penolakan sah (tema
        # waktu/per-pelanggan, root ATAU spesialis menolak tanpa data, lihat
        # ROOT_INSTRUCTION/KATEGORI_INSTRUCTION/PRODUK_INSTRUCTION), yang
        # TIDAK menyebut angka konkret apa pun. TAPI ditemukan lewat laporan
        # pengguna langsung: kadang root malah menjawab kreatif dari ingatan
        # sendiri (mis. "buatkan 5 ide promo") dan menyebut banyak angka
        # (harga, persentase diskon, jumlah terjual) yang jelas bukan dari
        # data nyata karena tidak ada satu tool pun dipanggil -- pola
        # fabrikasi yang beda dari penolakan sah lewat jumlah angka
        # konkretnya (penolakan sah ~0 angka, fabrikasi puluhan). Cek
        # tool_choice="required" di level API TERBUKTI tidak bisa dipakai di
        # sini (LiteLLM membuang tool_choice untuk provider ollama_chat,
        # lihat litellm/llms/ollama/chat/transformation.py:186, komentarnya
        # sendiri "causes ollama requests to hang") -- jadi ini satu-satunya
        # jaring pengaman yang bisa dipasang tanpa akses tool asli di sini.
        draft_nums_all = _extract_numbers(draft_answer)
        suspicious_nums = draft_nums_all - _extract_numbers(question) - history_nums
        grounded_nums = draft_nums_all & history_nums
        grounded_ratio = len(grounded_nums) / len(draft_nums_all) if draft_nums_all else 0.0
        creative_but_grounded = (
            len(grounded_nums) >= _MIN_GROUNDED_NUM_MATCHES and grounded_ratio >= _MIN_GROUNDED_NUM_RATIO
        )
        if len(suspicious_nums) < _SUSPICIOUS_NUM_THRESHOLD or creative_but_grounded:
            return draft_answer  # penolakan sah / angka asing sedikit / cukup angka nyata dari riwayat utk jawaban kreatif
        with open(TOOL_LOG_PATH, "a", encoding="utf-8") as f:
            f.write(
                f"NOTE {datetime.now().isoformat(timespec='seconds')} | verify_and_revise | "
                f"NOL TOOL DIPANGGIL tapi draft menyebut {len(suspicious_nums)} angka konkret asing "
                f"dan cuma {len(grounded_nums)}/{len(draft_nums_all)} angka match riwayat (rasio {grounded_ratio:.2f}) -- "
                f"kemungkinan dikarang, draft ditolak: {suspicious_nums}\n"
            )
        return (
            "Saya belum benar-benar mengambil data penjualan untuk pertanyaan ini "
            "(tidak ada tool yang terpanggil), jadi saya tidak mau memberi angka atau "
            "nama produk spesifik yang berisiko dikarang. Coba tanya lebih eksplisit, "
            "misalnya \"ambil dulu 5 produk terlaris, baru buatkan ide promo untuk "
            "masing-masing\", supaya data aslinya benar-benar diambil dulu."
        )

    draft_nums = _extract_numbers(draft_answer)
    tool_nums = _extract_numbers(tool_outputs)
    # allowed_nums termasuk angka dari pertanyaan pengguna sendiri (mis. "5
    # produk terlaris", "harga di bawah 10000") -- ditemukan lewat pengamatan
    # langsung: jawaban WAJAR mengulang angka permintaan pengguna ("Berikut 5
    # produk...") yang bukan data dari tool, dan tanpa ini verify_and_revise
    # eskalasi di ~50% giliran (jauh di atas ~8% hasil benchmark), bukan
    # karena jawabannya salah, tapi karena pengecekannya terlalu ketat.
    allowed_nums = tool_nums | _extract_numbers(question) | history_nums
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


async def verify_and_revise(
    draft_answer: str,
    tool_outputs: str,
    question: str = "",
    history_nums: frozenset[str] = frozenset(),
) -> str:
    return await asyncio.to_thread(_verify_and_revise_impl, draft_answer, tool_outputs, question, history_nums)


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
    # Riwayat sesi (bukan cuma giliran ini) dibaca ulang di sini supaya
    # verify_and_revise bisa membedakan "mengulang/berbasis data nyata dari
    # giliran sebelumnya" (sah) dari "mengarang" (ditolak) -- lihat
    # _extract_session_tool_numbers().
    session = await runner.session_service.get_session(
        app_name=APP_NAME, user_id=user_id, session_id=session_id
    )
    history_nums = frozenset(_extract_session_tool_numbers(session)) if session is not None else frozenset()
    revised = await verify_and_revise(final_text, tool_outputs, query, history_nums)
    return _strip_trailing_meta_section(revised)


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
