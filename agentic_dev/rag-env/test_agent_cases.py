"""
Kasus uji untuk root_agent di query.py -- mencakup kesepuluh tool
(search_catalog, get_top_sellers, find_cross_sell_candidates, get_top_categories,
get_worst_sellers, get_category_assortment, get_price_range, get_trending_products,
get_trending_categories, get_peak_hours) plus kasus edge:
pertanyaan bertema waktu/per-pelanggan yang HARUS ditolak (lihat PANDUAN_PENGGUNAAN.md
dan rag-setup-windows.md), segmen di luar katalog, multi-turn, dan pertanyaan
gabungan yang butuh beberapa tool sekaligus dalam satu giliran.

Sengaja pakai InMemorySessionService per kasus (bukan SqliteSessionService produksi
di agent_sessions.db) supaya riwayat uji coba tidak tercampur dengan percakapan asli,
dan supaya tiap kasus mulai dari konteks bersih (kecuali kasus multi-turn yang memang
sengaja berbagi satu session_id).

Cara pakai:
    ./Scripts/python.exe test_agent_cases.py

Hasil ditulis progresif ke test_report.jsonl (satu JSON per kasus, di-flush tiap
kasus selesai) supaya bisa dipantau sambil proses masih jalan.
"""

import asyncio
import json
import time
import traceback

from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService

from query import APP_NAME, TOOL_LOG_PATH, ask, root_agent

REPORT_PATH = "D:/agentic/agentic_dev/rag-env/test_report.jsonl"
TEST_USER = "test_user"

# (case_id, tool_area, [pertanyaan...], expected_tools, catatan)
# expected_tools: list nama tool yang seharusnya dipanggil di SELURUH kasus ini
# (gabungan semua giliran). [] berarti seharusnya TIDAK ADA tool dipanggil sama
# sekali (mis. kasus refusal bertema waktu/per-pelanggan).
CASES = [
    # --- search_catalog (12) ---
    ("SC01", "search_catalog", ["cari sabun mandi"], ["search_catalog"], ""),
    ("SC02", "search_catalog", ["cari sabun mandi kategori Sabun Mandi"], ["search_catalog"], ""),
    ("SC03", "search_catalog", ["cari mie instan harga di bawah 5000"], ["search_catalog"], ""),
    ("SC04", "search_catalog", ["cari sampo anti ketombe kategori Sampo harga maksimal 30000"], ["search_catalog"], ""),
    ("SC05", "search_catalog", ["ada parfum pria tidak?"], ["search_catalog"], ""),
    ("SC06", "search_catalog", ["cari keripik pedas"], ["search_catalog"], ""),
    ("SC07", "search_catalog", ["cari popok bayi ukuran celana"], ["search_catalog"], ""),
    ("SC08", "search_catalog", ["cari susu cair rendah lemak"], ["search_catalog"], ""),
    ("SC09", "search_catalog", ["cari produk pembersih wajah untuk kulit berminyak"], ["search_catalog"], ""),
    ("SC10", "search_catalog", ["cari teh siap minum"], ["search_catalog"], ""),
    ("SC11", "search_catalog", ["cari es krim coklat"], ["search_catalog"], ""),
    ("SC12", "search_catalog", ["carikan sabun MANDI yang murah"], ["search_catalog"], "case-insensitivity"),

    # --- get_top_sellers (14) ---
    ("TS01", "get_top_sellers", ["produk terlaris kategori Makanan"], ["get_top_sellers"], ""),
    ("TS02", "get_top_sellers", ["produk terlaris kategori Minuman"], ["get_top_sellers"], ""),
    ("TS03", "get_top_sellers", ["produk terlaris kategori Kebutuhan Dapur"], ["get_top_sellers"], ""),
    ("TS04", "get_top_sellers", ["produk terlaris kategori Personal Care"], ["get_top_sellers"], ""),
    ("TS05", "get_top_sellers", ["produk terlaris kategori Lifestyle"], ["get_top_sellers"], ""),
    ("TS06", "get_top_sellers", ["5 produk terlaris untuk sabun mandi"], ["get_top_sellers"], ""),
    ("TS07", "get_top_sellers", ["10 produk terlaris kategori Kebutuhan Rumah"], ["get_top_sellers"], ""),
    ("TS08", "get_top_sellers", ["produk apa yang paling laris di kategori Kebutuhan Kesehatan"], ["get_top_sellers"], ""),
    ("TS09", "get_top_sellers", ["top 3 produk terlaris minuman"], ["get_top_sellers"], ""),
    ("TS10", "get_top_sellers", ["produk terlaris untuk kategori Kebutuhan Ibu & Anak"], ["get_top_sellers"], ""),
    ("TS11", "get_top_sellers", ["apa produk terlaris di kategori Produk Segar & Beku"], ["get_top_sellers"], ""),
    ("TS12", "get_top_sellers", ["top seller kategori Pet Foods"], ["get_top_sellers"], "kategori volume kecil"),
    ("TS13", "get_top_sellers", ["produk terlaris untuk sampo"], ["get_top_sellers"], ""),
    ("TS14", "get_top_sellers", ["produk terlaris kategori minuman"], ["get_top_sellers"], "lowercase, case-insensitivity"),

    # --- find_cross_sell_candidates (10) ---
    ("CS01", "find_cross_sell_candidates", ["cari produk mirip dengan Lifebuoy Sabun Mandi Cair Anti Bakteri Lemon Fresh 380 g yang penjualannya rendah"], ["find_cross_sell_candidates"], ""),
    ("CS02", "find_cross_sell_candidates", ["produk apa yang cocok dipromosikan bareng Sari Roti Sandwich Isi Cokelat 40 g"], ["find_cross_sell_candidates"], ""),
    ("CS03", "find_cross_sell_candidates", ["cari kandidat cross-sell untuk Indomie Goreng"], ["find_cross_sell_candidates"], ""),
    ("CS04", "find_cross_sell_candidates", ["produk mirip apa yang bisa dipromosikan dengan Aqua Botol 600ml"], ["find_cross_sell_candidates"], ""),
    ("CS05", "find_cross_sell_candidates", ["cari produk serupa dengan Pepsodent Pasta Gigi yang belum laku"], ["find_cross_sell_candidates"], ""),
    ("CS06", "find_cross_sell_candidates", ["kandidat promosi untuk Lifebuoy Body Wash Sabun Mandi Cair Refill Total 10 450Ml"], ["find_cross_sell_candidates"], ""),
    ("CS07", "find_cross_sell_candidates", ["produk apa yang mirip Teh Pucuk Harum dan masih kurang laku"], ["find_cross_sell_candidates"], ""),
    ("CS08", "find_cross_sell_candidates", ["cari 3 produk mirip dengan Chitato Rasa Sapi Panggang"], ["find_cross_sell_candidates"], ""),
    ("CS09", "find_cross_sell_candidates", ["produk cross-sell untuk Downy Pewangi Pakaian"], ["find_cross_sell_candidates"], ""),
    ("CS10", "find_cross_sell_candidates", ["cari produk serupa Nescafe Kopi Instan yang penjualannya rendah"], ["find_cross_sell_candidates"], ""),

    # --- get_top_categories (12) ---
    ("TC01", "get_top_categories", ["kategori apa yang paling laris"], ["get_top_categories"], ""),
    ("TC02", "get_top_categories", ["kategori mana yang penjualannya paling sedikit"], ["get_top_categories"], "terendah=True"),
    ("TC03", "get_top_categories", ["top 5 kategori terlaris"], ["get_top_categories"], ""),
    ("TC04", "get_top_categories", ["3 kategori dengan penjualan terendah"], ["get_top_categories"], "terendah=True"),
    ("TC05", "get_top_categories", ["urutkan kategori dari yang paling laris"], ["get_top_categories"], ""),
    ("TC06", "get_top_categories", ["kategori apa yang paling banyak terjual"], ["get_top_categories"], ""),
    ("TC07", "get_top_categories", ["kategori mana yang paling jarang dibeli pelanggan"], ["get_top_categories"], "terendah=True"),
    ("TC08", "get_top_categories", ["sebutkan 10 kategori terlaris"], ["get_top_categories"], ""),
    ("TC09", "get_top_categories", ["kategori dengan penjualan paling rendah apa saja"], ["get_top_categories"], "terendah=True"),
    ("TC10", "get_top_categories", ["yang paling laku kategori apa"], ["get_top_categories"], ""),
    ("TC11", "get_top_categories", ["kategori paling tidak laku apa"], ["get_top_categories"], "terendah=True"),
    ("TC12", "get_top_categories", ["20 kategori teratas"], ["get_top_categories"], "top_n > jumlah kategori unik (cuma ~20)"),

    # --- get_worst_sellers (10) ---
    ("WS01", "get_worst_sellers", ["produk paling tidak laku"], ["get_worst_sellers"], ""),
    ("WS02", "get_worst_sellers", ["produk yang belum pernah terjual sama sekali"], ["get_worst_sellers"], ""),
    ("WS03", "get_worst_sellers", ["produk paling tidak laku di kategori Sabun Mandi"], ["get_worst_sellers"], ""),
    ("WS04", "get_worst_sellers", ["5 produk dengan penjualan terendah kategori Sampo"], ["get_worst_sellers"], ""),
    ("WS05", "get_worst_sellers", ["produk apa yang harus didiskontinuasi"], ["get_worst_sellers"], ""),
    ("WS06", "get_worst_sellers", ["produk mana yang perlu diturunkan harganya karena tidak laku"], ["get_worst_sellers"], ""),
    ("WS07", "get_worst_sellers", ["produk terjual paling sedikit di kategori Permen"], ["get_worst_sellers"], ""),
    ("WS08", "get_worst_sellers", ["cari produk yang penjualannya nol"], ["get_worst_sellers"], ""),
    ("WS09", "get_worst_sellers", ["produk apa kandidat dihentikan dari kategori Es Krim"], ["get_worst_sellers"], ""),
    ("WS10", "get_worst_sellers", ["10 produk paling tidak laku secara keseluruhan"], ["get_worst_sellers"], ""),

    # --- get_category_assortment (10) ---
    ("CA01", "get_category_assortment", ["kategori mana yang variasi produknya paling sedikit"], ["get_category_assortment"], ""),
    ("CA02", "get_category_assortment", ["kategori dengan jumlah produk paling banyak"], ["get_category_assortment"], "terendah=False"),
    ("CA03", "get_category_assortment", ["kategori apa yang punya paling sedikit pilihan produk"], ["get_category_assortment"], ""),
    ("CA04", "get_category_assortment", ["5 kategori dengan assortment paling sempit"], ["get_category_assortment"], ""),
    ("CA05", "get_category_assortment", ["kategori mana yang perlu ditambah variasi produknya"], ["get_category_assortment"], ""),
    ("CA06", "get_category_assortment", ["kategori apa yang paling banyak jenis produknya"], ["get_category_assortment"], "terendah=False"),
    ("CA07", "get_category_assortment", ["kategori dengan hanya 1 produk apa saja"], ["get_category_assortment"], ""),
    ("CA08", "get_category_assortment", ["top 3 kategori dengan produk terbanyak"], ["get_category_assortment"], "terendah=False"),
    ("CA09", "get_category_assortment", ["kategori mana yang kekurangan variasi dibanding lainnya"], ["get_category_assortment"], ""),
    ("CA10", "get_category_assortment", ["sebutkan kategori dengan jumlah produk paling sedikit"], ["get_category_assortment"], ""),

    # --- get_price_range (10) ---
    ("PR01", "get_price_range", ["berapa rentang harga kategori Sabun Mandi"], ["get_price_range"], ""),
    ("PR02", "get_price_range", ["harga termurah dan termahal untuk sampo"], ["get_price_range"], ""),
    ("PR03", "get_price_range", ["rentang harga produk kategori Teh & Kopi Siap Minum"], ["get_price_range"], ""),
    ("PR04", "get_price_range", ["berapa harga median kategori Keripik & Kerupuk"], ["get_price_range"], ""),
    ("PR05", "get_price_range", ["harga termahal di kategori Parfum & Cologne"], ["get_price_range"], ""),
    ("PR06", "get_price_range", ["rentang harga untuk popok celana"], ["get_price_range"], ""),
    ("PR07", "get_price_range", ["berapa kisaran harga produk susu cair"], ["get_price_range"], ""),
    ("PR08", "get_price_range", ["harga termurah kategori Permen"], ["get_price_range"], ""),
    ("PR09", "get_price_range", ["rentang harga kategori Elektronik"], ["get_price_range"], "kategori tidak ada di katalog, harus bilang tidak ada"),
    ("PR10", "get_price_range", ["berapa rentang harga produk kategori Perawatan & Pembersih Kain"], ["get_price_range"], ""),

    # --- get_trending_products / get_trending_categories (naik daun) (5) ---
    ("TR01", "get_trending_categories", ["kategori apa yang lagi tren sekarang"], ["get_trending_categories"], "dipindah dari EDW05 -- sekarang bisa dijawab lewat trending, bukan ditolak"),
    ("TR02", "get_trending_categories", ["kategori mana yang lagi naik daun"], ["get_trending_categories"], ""),
    ("TR03", "get_trending_products", ["produk sabun mandi apa yang lagi trending"], ["get_trending_products"], ""),
    ("TR04", "get_trending_products", ["produk apa yang lagi hits di kategori minuman"], ["get_trending_products"], ""),
    ("TR05", "get_trending_products", ["produk apa yang belakangan ini penjualannya naik"], ["get_trending_products"], ""),

    # --- get_peak_hours (jam ramai) (3) ---
    ("PH01", "get_peak_hours", ["jam berapa penjualan paling ramai"], ["get_peak_hours"], ""),
    ("PH02", "get_peak_hours", ["jam berapa kategori minuman paling laris terjual"], ["get_peak_hours"], ""),
    ("PH03", "get_peak_hours", ["waktu paling ramai untuk sabun mandi jam berapa"], ["get_peak_hours"], ""),

    # --- Filter tanggal spesifik (start_date/end_date) (3) ---
    ("DT01", "date_filter", ["penjualan tanggal 2 Agustus 2026 kategori apa yang paling laris"], ["get_top_categories"], "tanggal ada di data (2026-08-02), harus terjawab dengan filter tanggal"),
    ("DT02", "date_filter", ["produk terlaris kategori Minuman tanggal 1 Agustus 2026"], ["get_top_sellers"], "tanggal ada di data (2026-08-01)"),
    ("DT03", "date_filter", ["penjualan tanggal 25 Desember 2026 gimana"], ["get_top_categories"], "tanggal DI LUAR data yang tersedia -- tool tetap terpanggil tapi harus mengembalikan pesan jujur rentang tidak tersedia, bukan mengarang; baca jawabannya, jangan cuma cek nama tool"),

    # --- Regresi drift-bahasa: segmen dengan padanan Inggris jelas (3) ---
    ("LD01", "language_drift", ["produk terlaris kategori minuman"], ["get_top_sellers"], "'minuman'='drink' -- kalau argumen tool diam-diam diterjemahkan ke Inggris, hasilnya kosong; baca jawabannya, harus berisi produk nyata bukan 'tidak ada produk yang cocok'"),
    ("LD02", "language_drift", ["kategori makanan penjualannya berapa"], ["get_top_categories"], "'makanan'='food' -- cek jawaban bukan penolakan kosong"),
    ("LD03", "language_drift", ["cari susu cair rendah lemak yang lagi trending"], ["get_trending_products"], "'susu'='milk' -- cek jawaban bukan penolakan kosong"),

    # --- Edge cases: refusal bertema waktu (HARUS tidak pakai tool) (4) ---
    # Catatan: kolom transaction_time ADA sejak branch ini (bukan lagi alasan
    # penolakan) -- yang bikin ini tetap harus ditolak adalah rentang data yang
    # cuma ~3 hari (transaction_day1-3.csv), jadi frasa relatif ("minggu ini",
    # "bulan lalu", "tahun ini") cakupannya tidak jelas/pasti di luar rentang itu.
    ("EDW01", "edge_waktu", ["penjualan minggu ini gimana"], [], "harus menolak, frasa relatif ('minggu ini') yang cakupannya tidak jelas dengan data cuma ~3 hari"),
    ("EDW02", "edge_waktu", ["tren penjualan bulan lalu apa"], [], "harus menolak, frasa relatif ('bulan lalu') jelas di luar rentang data yang cuma ~3 hari"),
    ("EDW03", "edge_waktu", ["produk apa yang laku hari ini"], [], "harus menolak, 'hari ini' (tanggal berjalan) tidak pasti termasuk dalam rentang data yang cuma ~3 hari"),
    ("EDW04", "edge_waktu", ["bagaimana penjualan tahun ini dibanding tahun lalu"], [], "harus menolak, perbandingan tahun ini vs tahun lalu jauh di luar rentang data yang cuma ~3 hari"),

    # --- Edge cases: refusal per-pelanggan (HARUS tidak pakai tool) (3) ---
    ("EDU01", "edge_pelanggan", ["pelanggan mana yang paling sering beli sabun mandi"], [], "harus menolak, tidak ada user_id"),
    ("EDU02", "edge_pelanggan", ["siapa pembeli terbanyak bulan ini"], [], "harus menolak, tidak ada user_id + timestamp"),
    ("EDU03", "edge_pelanggan", ["riwayat pembelian pelanggan si Budi apa saja"], [], "harus menolak, tidak ada data per-pelanggan"),

    # --- Edge cases: luar katalog / robustness (5) ---
    ("EDX01", "edge_luar_katalog", ["cari produk pesawat terbang"], ["search_catalog"], "harus bilang tidak ketemu, bukan mengarang"),
    ("EDX02", "edge_luar_katalog", ["asdkjaskdj123 apa itu"], [], "input tidak bermakna, tidak boleh crash"),
    ("EDX03", "edge_luar_katalog", ["apa itu machine learning"], [], "di luar topik, harus menolak sopan bukan menjawab asal"),
    ("EDX04", "edge_luar_katalog", ["produk terlaris kategori Barang Antik"], ["get_top_sellers"], "kategori tidak ada, harus bilang tidak ada data"),
    ("EDX05", "edge_luar_katalog", ["kategoro paling laris apa"], ["get_top_categories"], "typo 'kategoro', tes robustness NLU"),

    # --- Multi-turn (memori sesi dalam satu proses) (3) ---
    ("MT01", "multi_turn", ["produk terlaris kategori Minuman", "yang paling murah dari situ berapa harganya?"], ["get_top_sellers"], "giliran ke-2 seharusnya TIDAK perlu tool baru, jawab dari konteks"),
    ("MT02", "multi_turn", ["kategori mana yang paling sedikit terjual", "kasih rekomendasi produk mirip yang lebih laku untuk kategori itu"], ["get_top_categories", "find_cross_sell_candidates"], "giliran ke-2 butuh tool baru berdasar konteks giliran pertama"),
    ("MT03", "multi_turn", ["produk paling tidak laku di kategori Sabun Mandi", "kalau kategori Sampo gimana?"], ["get_worst_sellers"], "giliran ke-2: segmen baru, harus panggil ulang tool dengan argumen baru"),

    # --- Kombinasi/compound (butuh >1 tool dalam satu giliran) (5) ---
    ("CP01", "compound", ["kasih rekomendasi lengkap: produk terlaris kategori Personal Care, rentang harganya, dan produk cross-sell-nya"], ["get_top_sellers", "get_price_range", "find_cross_sell_candidates"], "3 tool dalam satu giliran"),
    ("CP02", "compound", ["bandingkan kategori Makanan dan Minuman, mana yang lebih laris"], ["get_top_categories"], "perbandingan, cek tidak mengarang angka"),
    ("CP03", "compound", ["kategori mana yang butuh perhatian: variasi produk sedikit tapi penjualan juga rendah"], ["get_category_assortment", "get_top_categories"], "gabungan assortment + top_categories"),
    ("CP04", "compound", ["cari sabun mandi murah lalu kasih tau juga produk cross-sell yang cocok"], ["search_catalog", "find_cross_sell_candidates"], "search + cross-sell dalam satu giliran"),
    ("CP05", "compound", ["produk terlaris kategori Kebutuhan Dapur dan berapa jumlah variasi produk di kategori itu"], ["get_top_sellers", "get_category_assortment"], "top_sellers + assortment"),

    # --- Variasi top_n / boundary (5) ---
    ("BD01", "boundary", ["top 1 produk terlaris kategori Makanan"], ["get_top_sellers"], "top_n=1"),
    ("BD02", "boundary", ["1 kategori paling laris saja"], ["get_top_categories"], "top_n=1"),
    ("BD03", "boundary", ["50 produk terlaris kategori Minuman"], ["get_top_sellers"], "top_n besar"),
    ("BD04", "boundary", ["kategori Sabun Mandi paling laris atau paling sedikit?"], ["get_top_sellers"], "ambigu, cek model minta klarifikasi atau default masuk akal"),
    ("BD05", "boundary", ["produk termurah dan termahal di seluruh katalog"], ["get_price_range"], "tanpa segmen spesifik, tes argumen kosong/luas"),
]


def _count_lines(path):
    try:
        with open(path, encoding="utf-8") as f:
            return sum(1 for _ in f)
    except FileNotFoundError:
        return 0


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


async def run_case(session_id, questions):
    session_service = InMemorySessionService()
    await session_service.create_session(app_name=APP_NAME, user_id=TEST_USER, session_id=session_id)
    runner = Runner(agent=root_agent, app_name=APP_NAME, session_service=session_service)

    turns = []
    for q in questions:
        before = _count_lines(TOOL_LOG_PATH)
        started = time.monotonic()
        error = None
        final_text = "(tidak ada respons)"
        try:
            final_text = await ask(runner, q, user_id=TEST_USER, session_id=session_id)
        except Exception as e:
            error = f"{type(e).__name__}: {e}"
            traceback.print_exc()
        duration = time.monotonic() - started
        tools_called, specialists_called = _new_tool_calls(TOOL_LOG_PATH, before)
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
    return turns


async def main():
    total = len(CASES)
    print(f"Menjalankan {total} kasus uji...", flush=True)
    with open(REPORT_PATH, "w", encoding="utf-8") as report:
        for i, (case_id, tool_area, questions, expected_tools, note) in enumerate(CASES, start=1):
            print(f"[{i}/{total}] {case_id} ({tool_area})...", flush=True)
            turns = await run_case(f"test_{case_id}", questions)
            actual_tools = [t for turn in turns for t in turn["tools_called"]]
            had_error = any(turn["error"] for turn in turns)
            record = {
                "case_id": case_id,
                "tool_area": tool_area,
                "expected_tools": expected_tools,
                "actual_tools": actual_tools,
                "note": note,
                "had_error": had_error,
                "turns": turns,
            }
            report.write(json.dumps(record, ensure_ascii=False) + "\n")
            report.flush()
    print("SELESAI")


if __name__ == "__main__":
    asyncio.run(main())
