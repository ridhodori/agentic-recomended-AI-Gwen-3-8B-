"""
Agen rekomendasi & analisis penjualan berbasis google-adk + tool-calling
(qwen3-agent:latest via Ollama, dibungkus LiteLlm). Sesi multi-turn: pertanyaan
lanjutan dalam satu run yang sama tetap ingat konteks sebelumnya (mis. "dari
rekomendasi tadi, mana yang paling murah?").

Dua sumber data punya peran berbeda:
  - katalog_produk.csv (+ koleksi ChromaDB "products"): pencarian semantik
    produk dan metadata terstruktur (harga, kategori, terjual).
  - transaction_data/transaction_day*.csv: log transaksi mentah, terpecah jadi
    banyak file harian dan dimuat gabungan lewat glob (lihat
    TRANSACTION_CSV_GLOB, _load_transactions) -- otomatis ikut kalau file
    harian baru ditambahkan, tanpa perlu ubah kode. Tiap baris punya kolom
    transaction_time (timestamp) dan item_qty (bisa negatif untuk retur);
    "terjual"/"net qty" di semua tool dihitung sebagai jumlah item_qty
    (retur mengurangi), bukan jumlah baris mentah. Karena ada transaction_time,
    query bertema waktu (filter tanggal, tren, jam ramai) SEKARANG didukung,
    dibatasi rentang tanggal yang benar-benar termuat saat ini (lihat
    _available_date_range_note).

Bahasa instruksi vs jawaban: instruksi yang dibaca LLM (ROOT_INSTRUCTION dkk.)
dan docstring tool sekarang ditulis dalam Bahasa Inggris (model ini mengikuti
instruksi Inggris lebih konsisten), tapi jawaban akhir ke pengguna tetap WAJIB
Bahasa Indonesia -- lihat aturan eksplisit di tiap instruksi.

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
  - root_agent menggunakan _build_root_instruction sebagai router, dan
    sub-agents (kategori_specialist, produk_specialist) di-wrap otomatis jadi
    tools oleh ADK model_post_init. Ketiganya (_build_root_instruction,
    _build_kategori_instruction, _build_produk_instruction) adalah callable
    InstructionProvider, bukan string statis -- root/kategori menyisipkan
    rentang tanggal data transaksi yang tersedia tiap giliran, produk
    menyisipkan itu DITAMBAH info katalog terkini (lihat
    infrastructure_agentic.md bagian Prompt).
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
import glob
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
TRANSACTION_CSV_GLOB = "D:/agentic/transaction_data/transaction_day*.csv"
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
        paths = sorted(glob.glob(TRANSACTION_CSV_GLOB))
        df = pd.concat((pd.read_csv(p) for p in paths), ignore_index=True)
        df["transaction_time"] = pd.to_datetime(df["transaction_time"], format="%Y-%m-%d%H:%M:%S")
        _transactions_cache = df
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


def _filter_by_date(df: pd.DataFrame, start_date: str, end_date: str) -> tuple[pd.DataFrame, str]:
    """Filter df berdasarkan rentang tanggal transaction_time (inklusif di
    kedua ujung). start_date/end_date kosong berarti tanpa batas di sisi
    itu -- default tanpa filter sama sekali kalau keduanya kosong (lihat
    2026-09-06-transaction-data-v2-design.md bagian 3a). Mengembalikan
    (df_terfilter, pesan_error) -- pesan_error non-kosong kalau formatnya
    salah atau rentangnya di luar data yang tersedia, supaya tool bisa
    menolak jujur alih-alih mengarang dari df kosong."""
    if not start_date and not end_date:
        return df, ""
    available_min = df["transaction_time"].dt.date.min()
    available_max = df["transaction_time"].dt.date.max()
    try:
        start = pd.Timestamp(start_date).date() if start_date else available_min
        end = pd.Timestamp(end_date).date() if end_date else available_max
    except ValueError:
        return df.iloc[0:0], (
            f"Format tanggal tidak valid (pakai YYYY-MM-DD): start_date='{start_date}' end_date='{end_date}'."
        )
    if start > available_max or end < available_min:
        return df.iloc[0:0], (
            f"Tidak ada data untuk rentang {start_date or available_min} s.d. {end_date or available_max} -- "
            f"data yang tersedia cuma {available_min} s.d. {available_max}."
        )
    mask = (df["transaction_time"].dt.date >= start) & (df["transaction_time"].dt.date <= end)
    return df[mask], ""


def _get_top_sellers_impl(segment: str, top_n: int, start_date: str, end_date: str) -> str:
    if not segment.strip():
        return "Segmen kosong, tidak bisa mencari produk terlaris."

    df = _load_transactions()
    df, date_error = _filter_by_date(df, start_date, end_date)
    if date_error:
        return date_error

    mask = df["product_name"].str.contains(segment, case=False, na=False) | df[
        "product_category_name_lvl_0"
    ].str.contains(segment, case=False, na=False)
    filtered = df[mask]

    if filtered.empty:
        return f"Tidak ada data penjualan untuk segmen '{segment}'."

    top = (
        filtered.groupby("product_name")
        .agg(
            terjual=("item_qty", "sum"),
            harga=("product_price", "first"),
            kategori=("product_category_name_lvl_0", "first"),
        )
        .sort_values("terjual", ascending=False)
        .head(top_n)
    )
    return "\n".join(
        f"- {name} | kategori: {row.kategori} | terjual: {row.terjual:.0f}x | harga: {row.harga:.0f}"
        for name, row in top.iterrows()
    )


async def get_top_sellers(
    segment: str, top_n: int = 5, start_date: str = "", end_date: str = ""
) -> str:
    """
    Find best-selling products from raw transaction data, filtered by segment.

    Args:
      segment: Product/category keyword to filter by, e.g. "sabun mandi" or "minuman".
      top_n: Number of top-selling products to return.
      start_date: Optional start date (YYYY-MM-DD), inclusive. Empty ("") means no
        lower bound -- use every available date.
      end_date: Optional end date (YYYY-MM-DD), inclusive. Empty ("") means no upper
        bound -- use every available date.

    Returns:
      str: List of best-selling products with category, net units sold (returns
        subtracted), and price, one per line.
    """
    return await asyncio.to_thread(_get_top_sellers_impl, segment, top_n, start_date, end_date)


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


def _get_top_categories_impl(top_n: int, terendah: bool, start_date: str, end_date: str) -> str:
    df = _load_transactions()
    df, date_error = _filter_by_date(df, start_date, end_date)
    if date_error:
        return date_error

    counts = df.groupby("product_category_name_lvl_0")["item_qty"].sum()
    top = counts.sort_values(ascending=terendah).head(top_n)

    if top.empty:
        return "Tidak ada data kategori."

    return "\n".join(f"- {cat} | terjual: {count:.0f}x" for cat, count in top.items())


async def get_top_categories(
    top_n: int = 5, terendah: bool = False, start_date: str = "", end_date: str = ""
) -> str:
    """
    Find product categories with the highest (or lowest) net units sold from raw
    transaction data.

    Args:
      top_n: Number of categories to return.
      terendah: True to sort by lowest net units sold first, False (default) for
        highest first.
      start_date: Optional start date (YYYY-MM-DD), inclusive. Empty ("") means no
        lower bound -- use every available date.
      end_date: Optional end date (YYYY-MM-DD), inclusive. Empty ("") means no upper
        bound -- use every available date.

    Returns:
      str: List of categories with net units sold, one per line.
    """
    return await asyncio.to_thread(_get_top_categories_impl, top_n, terendah, start_date, end_date)


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


def _get_peak_hours_impl(segment: str, top_n: int) -> str:
    df = _load_transactions()
    if segment.strip():
        mask = df["product_name"].str.contains(segment, case=False, na=False) | df[
            "product_category_name_lvl_0"
        ].str.contains(segment, case=False, na=False)
        df = df[mask]
        if df.empty:
            return f"Tidak ada data penjualan untuk segmen '{segment}'."

    by_hour = df.groupby(df["transaction_time"].dt.hour)["item_qty"].sum()
    top = by_hour.sort_values(ascending=False).head(top_n)

    if top.empty:
        return "Tidak ada data jam penjualan."

    return "\n".join(f"- jam {hour:02d}:00-{hour:02d}:59 | terjual: {qty:.0f}x" for hour, qty in top.items())


async def get_peak_hours(segment: str = "", top_n: int = 5) -> str:
    """
    Find the busiest hours of the day (by net units sold) from raw transaction data,
    optionally filtered by segment. Useful for staffing or promo-timing decisions.

    Args:
      segment: Optional product/category keyword to filter by, e.g. "sabun mandi".
        Empty ("") means use every available transaction, not just one segment.
      top_n: Number of top hours to return.

    Returns:
      str: List of the busiest hours (0-23, as recorded in the data) with net units
        sold, one per line, sorted busiest first.
    """
    return await asyncio.to_thread(_get_peak_hours_impl, segment, top_n)


def _latest_two_dates(df: pd.DataFrame):
    """(tanggal_sebelumnya, tanggal_terbaru) yang BENAR-BENAR ada di df,
    bukan hardcode day2/day3 -- otomatis mengikuti data terbaru kalau
    transaction_dayN.csv baru ditambahkan nanti (lihat
    2026-09-06-transaction-data-v2-design.md bagian 3b). None kalau df
    punya kurang dari 2 tanggal berbeda."""
    dates = sorted(df["transaction_time"].dt.date.unique())
    if len(dates) < 2:
        return None
    return dates[-2], dates[-1]


def _trending_impl(df: pd.DataFrame, group_col: str, top_n: int) -> str:
    """Logika bersama get_trending_products/get_trending_categories --
    beda cuma kolom yang di-groupby (product_name vs
    product_category_name_lvl_0)."""
    dates = _latest_two_dates(df)
    if dates is None:
        return "Tidak cukup data (butuh minimal 2 tanggal berbeda) untuk menghitung tren."
    prev_date, latest_date = dates

    prev_qty = df[df["transaction_time"].dt.date == prev_date].groupby(group_col)["item_qty"].sum()
    latest_qty = df[df["transaction_time"].dt.date == latest_date].groupby(group_col)["item_qty"].sum()
    growth = latest_qty.subtract(prev_qty, fill_value=0).sort_values(ascending=False).head(top_n)

    if growth.empty:
        return "Tidak ada data untuk menghitung tren."

    lines = [f"Dibandingkan {prev_date} vs {latest_date}:"]
    for name, delta in growth.items():
        lines.append(
            f"- {name} | {prev_date}: {prev_qty.get(name, 0):.0f}x -> "
            f"{latest_date}: {latest_qty.get(name, 0):.0f}x | perubahan: {delta:+.0f}"
        )
    return "\n".join(lines)


def _get_trending_products_impl(segment: str, top_n: int) -> str:
    df = _load_transactions()
    if segment.strip():
        mask = df["product_name"].str.contains(segment, case=False, na=False) | df[
            "product_category_name_lvl_0"
        ].str.contains(segment, case=False, na=False)
        df = df[mask]
        if df.empty:
            return f"Tidak ada data penjualan untuk segmen '{segment}'."
    return _trending_impl(df, "product_name", top_n)


async def get_trending_products(segment: str = "", top_n: int = 5) -> str:
    """
    Find products with the biggest growth in net units sold between the two most
    recent dates present in the transaction data (self-scaling: as more daily
    files are added, "trending" automatically follows the newest data).

    Args:
      segment: Optional product/category keyword to filter by, e.g. "sabun mandi".
        Empty ("") means consider every product, not just one segment.
      top_n: Number of top-growing products to return.

    Returns:
      str: The two dates being compared, followed by the top-growing products with
        their net units sold on each date and the change, sorted by biggest growth
        first. A product present on only one of the two dates is treated as having
        0 units on the missing date (so a brand-new hit or a vanished product both
        show up as a large change, not skipped).
    """
    return await asyncio.to_thread(_get_trending_products_impl, segment, top_n)


def _get_trending_categories_impl(top_n: int) -> str:
    df = _load_transactions()
    return _trending_impl(df, "product_category_name_lvl_0", top_n)


async def get_trending_categories(top_n: int = 5) -> str:
    """
    Find product categories with the biggest growth in net units sold between the
    two most recent dates present in the transaction data (self-scaling, see
    get_trending_products).

    Args:
      top_n: Number of top-growing categories to return.

    Returns:
      str: The two dates being compared, followed by the top-growing categories
        with net units sold on each date and the change, sorted by biggest growth
        first.
    """
    return await asyncio.to_thread(_get_trending_categories_impl, top_n)


def _build_root_instruction(context) -> str:
    """InstructionProvider untuk root_agent -- isinya sama seperti
    ROOT_INSTRUCTION lama (string statis), tapi sekarang menyisipkan
    rentang tanggal data transaksi yang BENAR-BENAR tersedia secara
    dinamis, pola yang sama seperti _build_produk_instruction menyisipkan
    info katalog -- perlu dinamis karena larangan blanket "tidak ada data
    waktu" yang lama sudah tidak akurat, dan rentangnya harus tetap benar
    kalau transaction_dayN.csv baru ditambahkan nanti tanpa perlu ubah
    prompt (lihat 2026-09-06-transaction-data-v2-design.md bagian 3d)."""
    date_range = _available_date_range_note()
    return f"""You are the router for Alfagift's retail sales-analysis assistant.
Your job is NOT to answer questions yourself -- pick one or more specialists
below based on the question type, and call them with a `request` parameter
containing a clear, SELF-CONTAINED instruction: if the user's question refers
back to a previous turn (e.g. "from that", "that category", "the one just
mentioned"), YOU MUST replace that reference with the concrete value (explicit
product/category name, taken from the conversation history you can see) inside
the request you send -- specialists CANNOT see the conversation history, they
only see the request text you send them.

Available specialists:
1. kategori_specialist -- category-level questions in general: most/least
   sold categories, product variety/assortment per category, category-level
   trending. Has NO price tool at all.
2. produk_specialist -- product-level questions: product search, best/worst
   sellers, price ranges, cross-sell recommendations, product-level trending,
   and peak selling hours.

IMPORTANT about the word "kategori" (category): this word appears in TWO
different contexts -- (a) a PURE category-level question (kategori_specialist),
e.g. "which category sells the most", not mentioning any specific price or
product at all, vs (b) "kategori" used only as a SEGMENT QUALIFIER for a
PRICE or PRODUCT question, e.g. "harga median kategori Keripik & Kerupuk"
("median price for the Keripik & Kerupuk category"), "produk termahal di
kategori Minuman" ("most expensive product in the Minuman category") -- these
MUST go to produk_specialist, because only it has the price tool
(get_price_range) and product tools. Rule: if the question is about PRICE,
PRODUCT, or CROSS-SELL, it ALWAYS goes to produk_specialist -- regardless of
whether the word "kategori" is also used as a segment qualifier.

If a question needs more than one specialist (e.g. best-selling product AND
its price range), call ALL relevant specialists in the same turn, then merge
their results into one coherent answer.

Some questions are framed as creative/strategic requests (e.g. "make me promo
ideas from the 5 best-selling products", "suggest a sales strategy for
category X") but still DEPEND on concrete facts (which product is the best
seller, what price, which category). Framing it as "idea/recommendation/
strategy" is NOT a reason to skip the specialists -- if your answer will
mention a specific product/category name, sales number, or price, you MUST
call the relevant specialist first for that data, then build your idea on top
of it. Parts that are purely your own suggestion (e.g. a promo discount
percentage, marketing copy) may be added, but mark them clearly as a
suggestion -- never let the reader think they came from real sales data.

If you think ADDITIONAL data (e.g. cross-sell candidates) would strengthen the
answer, CALL the relevant specialist NOW in the same turn -- do NOT just
mention the tool/specialist name as a "next step" in your final answer. The
user cannot call tools themselves, so a sentence like "use
find_cross_sell_candidates..." is useless to them and leaks an internal
implementation detail -- if you are not calling it now, do not mention that
tool/specialist name in the final answer at all.

Data time coverage: the transaction data currently available covers
{date_range} (this range is computed live from the loaded data, so it always
reflects whatever transaction_dayN.csv files are currently loaded -- if more
daily files are added later, this range updates automatically). Time-themed
questions (e.g. "sales this week", "trending now", "recently", "what's hot
lately") CAN be answered now, through get_trending_products/
get_trending_categories (compares the two most recent dates in the data) or
the optional start_date/end_date filters on get_top_sellers/get_top_categories
-- call the relevant specialist for these instead of refusing. BUT if the
question's date/range falls OUTSIDE {date_range}, or uses a relative phrase
whose coverage is unclear given how little data exists (e.g. "last month" when
only a few days of data exist), do NOT call any specialist to make up an
answer -- say plainly that it can't be answered from the available data, and
state the actual available range. Per-customer questions (e.g. "which
customer buys X the most") are still always refused outright without calling
any specialist -- the data has no user_id column at all, regardless of the
time range.

CRITICAL: when extracting a product/category/segment value from the user's
Indonesian-language question to pass to a specialist, copy it EXACTLY as
written (or exactly as it appears in prior tool results) -- NEVER translate
it into English, even though these instructions are written in English. Tool
arguments are matched against Indonesian catalog/category text via literal
substring search; an English translation of the value will silently match
nothing.

IMPORTANT: when you combine answers from more than one specialist, quote
numbers EXACTLY as each specialist returned them -- do not recompute or
estimate from memory.

Always respond in Bahasa Indonesia (Indonesian), regardless of the language
of these instructions."""


def _build_kategori_instruction(context) -> str:
    """InstructionProvider untuk kategori_specialist -- alasan sama seperti
    _build_root_instruction (rentang tanggal dinamis)."""
    date_range = _available_date_range_note()
    return f"""You are a retail category-analysis specialist for Alfagift.
You receive a request that is ALREADY self-contained (no other conversation
history needed) from the router -- answer directly based on that request.

Your job, pick the tool that matches the request type:
1. Categories in general (not a specific segment/product) -- "which category
   sells the most" / "which category sells the least" -> get_top_categories
   (terendah=True for the least-selling).
2. A specific date or date range -> pass start_date/end_date (YYYY-MM-DD) to
   get_top_categories; leave both empty ("") to use every available date
   (the default -- not a special case).
3. Categories with the least/most product variety in the catalog (assortment
   gap) -> get_category_assortment.
4. Trending categories (biggest growth between the two most recent dates in
   the data, e.g. "which category is trending/picking up now") ->
   get_trending_categories.

Data time coverage: {date_range} (computed live from the loaded data, updates
automatically as more daily files are added). If a time-themed request's
date/range falls OUTSIDE this range, or uses a relative phrase whose coverage
is unclear given how little data exists, do NOT call any tool to make up an
answer -- say plainly it can't be answered from the available data, and state
the actual available range. Per-customer requests are still always refused
outright without calling any tool -- the data has no user_id column at all.

CRITICAL: when extracting a category/segment value from the request to pass
as a tool argument, copy it EXACTLY as written -- NEVER translate it into
English, even though these instructions are written in English. Tool
arguments are matched against Indonesian catalog/category text via literal
substring search; an English translation of the value will silently match
nothing.

IMPORTANT: quote numbers (units sold, product counts) EXACTLY as returned by
the tool -- do not recompute or estimate from memory.

Always respond in Bahasa Indonesia (Indonesian), regardless of the language
of these instructions."""


PRODUK_INSTRUCTION = """You are a retail product-analysis specialist for Alfagift.
You receive a request that is ALREADY self-contained (no other conversation
history needed) from the router -- answer directly based on that request.

Your job, pick the tool that matches the request type:
1. Best-selling products in a segment -> get_top_sellers (the result already
   includes each product's category, no extra tool needed for that). Pass
   start_date/end_date (YYYY-MM-DD) if the request names a specific date or
   date range; leave both empty ("") to use every available date (the
   default -- not a special case).
2. Least-selling / never-sold products, candidates for discontinuation or a
   price cut -> get_worst_sellers.
3. Price range (cheapest/most expensive/median) for a segment or category ->
   get_price_range (leave segment empty for the whole catalog's price range).
4. Trending products (biggest growth between the two most recent dates in the
   data, e.g. "which product is trending/picking up now") ->
   get_trending_products.
5. Busiest selling hours (for staffing/promo-timing questions, e.g. "what
   time of day sells the most") -> get_peak_hours.
6. If the request mentions "products similar/comparable to [X]" -- WHATEVER
   qualifier is attached (e.g. "with low sales", "that sell better", "for
   cross-selling") -- use find_cross_sell_candidates with product_name=X. If
   X is not yet a concrete product name (e.g. the request still names a
   category, not a specific product), call get_top_sellers first to get one
   concrete product name, THEN immediately call find_cross_sell_candidates
   with that name in the same turn -- do not stop at the first tool and tell
   the user to look it up themselves. NEVER claim a product is suitable for
   cross-selling without actually calling this tool to prove it. Cross-sell
   candidates MUST be OTHER products with lower sales than the reference
   product (exactly what this tool returns) -- NEVER suggest the reference
   product itself as its own cross-sell candidate. If this tool genuinely
   returns no candidates, say so plainly ("no suitable cross-sell candidates
   found") -- never make one up or substitute the reference product itself.
7. Use search_catalog when you need extra detail about a specific product.

If a request asks for SEVERAL things at once (e.g. best-seller AND its price
range AND a cross-sell recommendation), make sure your final answer actually
includes the result of EVERY tool you called -- never silently drop one of
the requested parts.

If a time-themed request's date/range falls OUTSIDE the available range (see
the current data time coverage noted below), or uses a relative phrase whose
coverage is unclear given how little data exists, do NOT call any tool to
make up an answer -- say plainly it can't be answered from the available
data, and state the actual available range. Per-customer requests are still
always refused outright without calling any tool -- the data has no user_id
column at all.

If you think ADDITIONAL data (e.g. cross-sell candidates) would strengthen
the answer, CALL the relevant tool NOW in the same turn -- do NOT just
mention the tool name as a "next step" in the final answer. The router that
forwards your answer to the user cannot call tools itself, so a sentence
like "use find_cross_sell_candidates..." is useless and leaks an internal
implementation detail.

CRITICAL: when extracting a product/category/segment value from the request
to pass as a tool argument, copy it EXACTLY as written -- NEVER translate it
into English, even though these instructions are written in English. Tool
arguments are matched against Indonesian catalog/category text via literal
substring search; an English translation of the value will silently match
nothing.

IMPORTANT: quote numbers (prices, units sold) EXACTLY as returned by the
tool -- do not recompute or estimate from memory. If you need a number that
isn't in any tool result yet, call the appropriate tool first -- never make
one up.

Always respond in Bahasa Indonesia (Indonesian), regardless of the language
of these instructions."""


def _available_date_range_note() -> str:
    df = _load_transactions()
    dmin = df["transaction_time"].dt.date.min()
    dmax = df["transaction_time"].dt.date.max()
    return f"{dmin} to {dmax}"


def _build_produk_instruction(context) -> str:
    """InstructionProvider untuk produk_specialist -- sama seperti sebelumnya
    (menyisipkan info katalog terkini), sekarang DITAMBAH rentang tanggal
    data transaksi yang tersedia (lihat _build_root_instruction untuk
    alasan lengkap kenapa ini perlu dinamis)."""
    katalog_mtime = datetime.fromtimestamp(os.path.getmtime(KATALOG_CSV)).strftime("%Y-%m-%d")
    date_range = _available_date_range_note()
    return (
        f"{PRODUK_INSTRUCTION}\n\n"
        f"Current catalog info: {len(katalog_df)} products registered, "
        f"{collection.count()} of them indexed for semantic search "
        f"(search_catalog/find_cross_sell_candidates), catalog data last updated {katalog_mtime}. "
        f"Current transaction data time coverage: {date_range} (computed live, updates "
        f"automatically as more daily files are added)."
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
    instruction=_build_kategori_instruction,
    mode="single_turn",
    tools=[get_top_categories, get_category_assortment, get_trending_categories],
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
        get_peak_hours,
        get_trending_products,
    ],
    before_tool_callback=_log_before_tool,
    after_tool_callback=_log_after_tool,
)

root_agent = Agent(
    model=LiteLlm(model=MODEL_LLM, num_ctx=8192),
    name="sales_recommender",
    description="Router: memilih spesialis kategori atau produk yang relevan untuk analisis penjualan & rekomendasi cross-sell katalog Alfagift.",
    instruction=_build_root_instruction,
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
