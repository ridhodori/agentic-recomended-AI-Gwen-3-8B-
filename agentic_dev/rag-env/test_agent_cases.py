"""
Kasus uji untuk root_agent di query.py (200 kasus) -- mencakup kesebelas tool
(search_catalog, get_top_sellers, get_top_sellers_by_day, find_cross_sell_candidates,
get_top_categories, get_worst_sellers, get_category_assortment, get_price_range,
get_trending_products, get_trending_categories, get_peak_hours) plus kasus edge:
pertanyaan bertema waktu/per-pelanggan yang HARUS ditolak (lihat PANDUAN_PENGGUNAAN.md
dan rag-setup-windows.md), segmen di luar katalog, multi-turn, dan pertanyaan
gabungan yang butuh beberapa tool sekaligus dalam satu giliran.

Grup tanggal DIPERLUAS jadi 5 kategori terpisah (86 kasus total) supaya filter
tanggal/rentang tanggal -- termasuk bug awal yang memicu penambahan
get_top_sellers_by_day -- punya cakupan uji yang proporsional, bukan cuma 3-4
kasus token seperti sebelumnya:
  - date_filter: SATU tanggal spesifik (start_date == end_date)
  - date_range: RENTANG tanggal, SATU ranking gabungan (get_top_sellers/get_top_categories)
  - per_day: breakdown TERPISAH per hari lintas rentang/beberapa tanggal (get_top_sellers_by_day)
    -- PD01 adalah PROMPT ASLI dari laporan bug pengguna, kata demi kata
  - date_edge: rentang terbalik/sebagian di luar data/format tanggal tidak biasa
  - compound_date: >1 tool sekaligus, salah satunya difilter tanggal

Sengaja pakai InMemorySessionService per kasus (bukan SqliteSessionService produksi
di agent_sessions.db) supaya riwayat uji coba tidak tercampur dengan percakapan asli,
dan supaya tiap kasus mulai dari konteks bersih (kecuali kasus multi-turn yang memang
sengaja berbagi satu session_id).

Cara pakai:
    ./Scripts/python.exe test_agent_cases.py

200 kasus (naik dari 106) -- regresi penuh diperkirakan ~50-70 menit (dulu
~25-30 menit di 104-106 kasus), jalankan di background kalau perlu terus
bekerja sambil menunggu. Hasil ditulis progresif ke test_report.jsonl (satu
JSON per kasus, di-flush tiap kasus selesai, termasuk teks pertanyaan asli di
tiap "turns[].question") supaya bisa dipantau sambil proses masih jalan.
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
    # --- search_catalog (14) ---
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
    ("SC13", "search_catalog", ["cari minuman berenergi"], ["search_catalog"], ""),
    ("SC14", "search_catalog", ["ada obat sakit kepala tidak?"], ["search_catalog"], ""),

    # --- get_top_sellers, tanpa filter tanggal (17) ---
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
    ("TS15", "get_top_sellers", ["top 2 produk terlaris"], ["get_top_sellers"], "TIDAK menyebut segmen/kategori sama sekali -- dulu selalu ditolak ('Segmen kosong'), sekarang harus menjawab dari seluruh data (root cause dari laporan bug awal pengguna)"),
    ("TS16", "get_top_sellers", ["produk terlaris apa saja di seluruh katalog"], ["get_top_sellers"], "variasi frasa segmen kosong"),
    ("TS17", "get_top_sellers", ["top 5 produk paling laris secara keseluruhan"], ["get_top_sellers"], "variasi frasa segmen kosong"),

    # --- find_cross_sell_candidates (13) ---
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
    ("CS11", "find_cross_sell_candidates", ["produk apa yang cocok untuk cross-sell dengan Teh Pucuk Harum Melati 350 ml"], ["find_cross_sell_candidates"], ""),
    ("CS12", "find_cross_sell_candidates", ["cari kandidat cross-sell untuk kategori Sampo, ambil dulu produk terlarisnya"], ["get_top_sellers", "find_cross_sell_candidates"], "kategori (bukan nama produk konkret) -- harus rantai get_top_sellers dulu untuk dapat nama produk, baru find_cross_sell_candidates, sesuai aturan #6 PRODUK_INSTRUCTION"),
    ("CS13", "find_cross_sell_candidates", ["cari produk mirip Indomie Goreng yang belum laku, ambil 5 saja"], ["find_cross_sell_candidates"], ""),

    # --- get_top_categories, tanpa filter tanggal (14) ---
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
    ("TC13", "get_top_categories", ["sebutkan kategori penjualan tertinggi"], ["get_top_categories"], ""),
    ("TC14", "get_top_categories", ["kategori mana yang paling banyak dibeli"], ["get_top_categories"], ""),

    # --- get_worst_sellers (12) ---
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
    ("WS11", "get_worst_sellers", ["produk kategori Minuman yang paling tidak laku apa"], ["get_worst_sellers"], ""),
    ("WS12", "get_worst_sellers", ["adakah produk yang sama sekali belum pernah terjual di kategori Makanan"], ["get_worst_sellers"], ""),

    # --- get_category_assortment (12) ---
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
    ("CA11", "get_category_assortment", ["kategori mana yang variasinya paling banyak dibanding lainnya"], ["get_category_assortment"], ""),
    ("CA12", "get_category_assortment", ["berapa kategori yang cuma punya sedikit produk"], ["get_category_assortment"], ""),

    # --- get_price_range (12) ---
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
    ("PR11", "get_price_range", ["rentang harga kategori Minuman berapa"], ["get_price_range"], ""),
    ("PR12", "get_price_range", ["harga median untuk kategori Sabun Mandi"], ["get_price_range"], ""),

    # --- get_trending_products / get_trending_categories (naik daun) (7) ---
    ("TR01", "get_trending_categories", ["kategori apa yang lagi tren sekarang"], ["get_trending_categories"], "dipindah dari EDW05 -- sekarang bisa dijawab lewat trending, bukan ditolak"),
    ("TR02", "get_trending_categories", ["kategori mana yang lagi naik daun"], ["get_trending_categories"], ""),
    ("TR03", "get_trending_products", ["produk sabun mandi apa yang lagi trending"], ["get_trending_products"], ""),
    ("TR04", "get_trending_products", ["produk apa yang lagi hits di kategori minuman"], ["get_trending_products"], ""),
    ("TR05", "get_trending_products", ["produk apa yang belakangan ini penjualannya naik"], ["get_trending_products"], ""),
    ("TR06", "get_trending_categories", ["kategori minuman lagi naik daun tidak"], ["get_trending_categories"], ""),
    ("TR07", "get_trending_products", ["produk apa yang performanya naik dibanding hari sebelumnya"], ["get_trending_products"], ""),

    # --- get_peak_hours (jam ramai) (5) ---
    ("PH01", "get_peak_hours", ["jam berapa penjualan paling ramai"], ["get_peak_hours"], ""),
    ("PH02", "get_peak_hours", ["jam berapa kategori minuman paling laris terjual"], ["get_peak_hours"], ""),
    ("PH03", "get_peak_hours", ["waktu paling ramai untuk sabun mandi jam berapa"], ["get_peak_hours"], ""),
    ("PH04", "get_peak_hours", ["jam berapa kategori makanan paling ramai terjual"], ["get_peak_hours"], ""),
    ("PH05", "get_peak_hours", ["waktu tersibuk penjualan sepanjang hari jam berapa"], ["get_peak_hours"], ""),

    # --- Filter tanggal spesifik, SATU tanggal (start_date == end_date) (10) ---
    ("DT01", "date_filter", ["penjualan tanggal 2 Agustus 2026 kategori apa yang paling laris"], ["get_top_categories"], "tanggal ada di data (2026-08-02), harus terjawab dengan filter tanggal"),
    ("DT02", "date_filter", ["produk terlaris kategori Minuman tanggal 1 Agustus 2026"], ["get_top_sellers"], "tanggal ada di data (2026-08-01)"),
    ("DT03", "date_filter", ["penjualan tanggal 25 Desember 2026 gimana"], ["get_top_categories"], "tanggal DI LUAR data yang tersedia -- tool tetap terpanggil tapi harus mengembalikan pesan jujur rentang tidak tersedia, bukan mengarang; baca jawabannya, jangan cuma cek nama tool"),
    ("DT04", "date_filter", ["kategori apa yang paling laris di tanggal 3 Agustus 2026"], ["get_top_categories"], "tanggal ada di data (2026-08-03)"),
    ("DT05", "date_filter", ["produk terlaris tanggal 2026-08-02"], ["get_top_sellers"], "segmen KOSONG + satu tanggal spesifik sekaligus -- kombinasi dua perbaikan (segmen opsional + filter tanggal)"),
    ("DT06", "date_filter", ["top 5 produk terlaris kategori Minuman pada tanggal 2026-08-03"], ["get_top_sellers"], ""),
    ("DT07", "date_filter", ["produk apa yang paling laku tanggal 1 Agustus 2026 untuk kategori Personal Care"], ["get_top_sellers"], ""),
    ("DT08", "date_filter", ["kategori terlaris pada 2026-08-01"], ["get_top_categories"], ""),
    ("DT09", "date_filter", ["kategori dengan penjualan terendah tanggal 3 Agustus 2026"], ["get_top_categories"], "terendah=True + filter tanggal"),
    ("DT10", "date_filter", ["produk terlaris sabun mandi pada tanggal 02/08/2026"], ["get_top_sellers"], "format tanggal DD/MM/YYYY -- model harus mengonversi ke YYYY-MM-DD (2026-08-02) sebelum memanggil tool, baca argumen di tool_calls.log"),

    # --- Filter RENTANG tanggal, SATU ranking gabungan (bukan per hari) (16) ---
    ("RD01", "date_range", ["produk terlaris kategori Minuman dari tanggal 2026-08-01 sampai 2026-08-03"], ["get_top_sellers"], "rentang 3 hari, SATU ranking gabungan -- bukan breakdown per hari (beda dari kasus PD)"),
    ("RD02", "date_range", ["top 5 produk terlaris dari 1 Agustus sampai 3 Agustus 2026"], ["get_top_sellers"], "segmen kosong + rentang tanggal"),
    ("RD03", "date_range", ["kategori apa yang paling laris dari tanggal 2026-08-01 hingga 2026-08-02"], ["get_top_categories"], ""),
    ("RD04", "date_range", ["produk terlaris kategori Sabun Mandi antara tanggal 1 dan 2 Agustus 2026"], ["get_top_sellers"], ""),
    ("RD05", "date_range", ["penjualan kategori Makanan dari tanggal 2026-08-02 sampai 2026-08-03 gimana"], ["get_top_categories"], ""),
    ("RD06", "date_range", ["5 kategori terlaris dalam rentang 2026-08-01 - 2026-08-03"], ["get_top_categories"], ""),
    ("RD07", "date_range", ["produk terlaris minuman rentang tanggal 2 sampai 3 Agustus 2026"], ["get_top_sellers"], ""),
    ("RD08", "date_range", ["top 3 produk terlaris kategori Kebutuhan Rumah periode 2026-08-01 sampai 2026-08-02"], ["get_top_sellers"], ""),
    ("RD09", "date_range", ["kategori dengan penjualan terendah dari tanggal 1 Agustus sampai 3 Agustus 2026"], ["get_top_categories"], "terendah=True + rentang"),
    ("RD10", "date_range", ["produk apa yang paling laris sepanjang tanggal 2026-08-01 sampai 2026-08-03"], ["get_top_sellers"], "segmen kosong + rentang penuh"),
    ("RD11", "date_range", ["top 10 produk terlaris kategori Personal Care untuk rentang 2026-08-01 hingga 2026-08-03"], ["get_top_sellers"], ""),
    ("RD12", "date_range", ["kategori apa yang penjualannya paling tinggi dari tanggal 2026-08-01 ke 2026-08-02"], ["get_top_categories"], ""),
    ("RD13", "date_range", ["produk terlaris kategori Sampo dari awal sampai akhir data yang ada"], ["get_top_sellers"], "frasa relatif TAPI eksplisit merujuk 'seluruh data yang ada' (bukan periode kalender relatif seperti EDW) -- harus dijawab, bukan ditolak; boleh start_date/end_date kosong ATAU diisi rentang penuh 2026-08-01..2026-08-03"),
    ("RD14", "date_range", ["5 produk terlaris kategori Kebutuhan Dapur dari tanggal 2026-08-01 sampai 2026-08-03"], ["get_top_sellers"], ""),
    ("RD15", "date_range", ["kategori terlaris untuk periode 1-3 Agustus 2026"], ["get_top_categories"], ""),
    ("RD16", "date_range", ["kategori paling sedikit terjual dari tanggal 2026-08-01 sampai 2026-08-03"], ["get_top_categories"], "terendah=True + rentang penuh"),

    # --- Breakdown PER HARI lintas tanggal (get_top_sellers_by_day) (15) ---
    ("PD01", "per_day", ["berikan aku 2 produk terlaris perhari dengan filter tanggal 2026-08-01 dan 2026-08-03"], ["get_top_sellers_by_day"], "PROMPT ASLI DARI PENGGUNA (laporan bug yang memicu perbaikan get_top_sellers_by_day) -- harus pakai get_top_sellers_by_day, dan jawaban HARUS di-group by date (top 2 terpisah untuk tiap tanggal), bukan satu ranking gabungan; baca jawabannya kata per kata, ini kasus prioritas tertinggi di suite ini"),
    ("PD02", "per_day", ["top 2 produk terlaris per hari untuk tanggal 2026-08-01 dan 2026-08-03"], ["get_top_sellers_by_day"], "variasi kata dari PD01 ('untuk tanggal X dan Y' vs 'dengan filter tanggal X dan Y') -- harus tetap breakdown per tanggal"),
    ("PD03", "per_day", ["top 3 produk terlaris per hari dari tanggal 2026-08-01 sampai 2026-08-03"], ["get_top_sellers_by_day"], "top_n=3, frasa 'dari...sampai' bukan 'dan'"),
    ("PD04", "per_day", ["produk terlaris kategori Minuman per hari untuk tanggal 1 sampai 3 Agustus 2026"], ["get_top_sellers_by_day"], "dengan segmen + breakdown per hari"),
    ("PD05", "per_day", ["tiap hari produk apa yang paling laris dari tanggal 2026-08-01 sampai 2026-08-02"], ["get_top_sellers_by_day"], "'tiap hari' di awal kalimat, rentang cuma 2 hari"),
    ("PD06", "per_day", ["setiap hari, produk terlaris kategori Sabun Mandi apa saja dari 2026-08-01 sampai 2026-08-03"], ["get_top_sellers_by_day"], "'setiap hari'"),
    ("PD07", "per_day", ["breakdown produk terlaris harian dari tanggal 2026-08-01 sampai 2026-08-03"], ["get_top_sellers_by_day"], "kata 'harian'/'breakdown' bukan 'per hari' literal"),
    ("PD08", "per_day", ["top 1 produk terlaris per hari untuk tanggal 2026-08-01, 2026-08-02, dan 2026-08-03"], ["get_top_sellers_by_day"], "top_n=1, 3 tanggal disebut eksplisit satu-satu (bukan rentang 'dari...sampai')"),
    ("PD09", "per_day", ["produk terlaris kategori Personal Care, dipisah per hari, tanggal 2026-08-01 dan 2026-08-03"], ["get_top_sellers_by_day"], "'dipisah per hari' di tengah kalimat"),
    ("PD10", "per_day", ["5 produk terlaris per hari kategori Kebutuhan Rumah dari 2026-08-01 sampai 2026-08-03"], ["get_top_sellers_by_day"], "top_n=5"),
    ("PD11", "per_day", ["produk apa yang terlaris di masing-masing hari antara 2026-08-01 dan 2026-08-03"], ["get_top_sellers_by_day"], "'masing-masing hari' -- sinonim 'per hari'"),
    ("PD12", "per_day", ["top 2 produk terlaris per hari untuk tanggal 2026-08-02 dan 2026-08-03"], ["get_top_sellers_by_day"], "hanya 2 dari 3 hari yang tersedia (bukan 08-01)"),
    ("PD13", "per_day", ["produk terlaris kategori Minuman perhari tanggal 2026-08-01"], ["get_top_sellers_by_day"], "EDGE: 'perhari' disebut tapi cuma SATU tanggal -- get_top_sellers biasa juga masuk akal di sini (hasilnya identik utk 1 hari), catat tool mana yang sebenarnya dipilih model, baca jawabannya bukan cuma nama tool"),
    ("PD14", "per_day", ["top 2 produk terlaris perhari dengan filter tanggal 2026-08-01 dan 2026-08-03 untuk kategori Sabun Mandi"], ["get_top_sellers_by_day"], "variasi PD01 DENGAN segmen eksplisit (Sabun Mandi)"),
    ("PD15", "per_day", ["bandingkan produk terlaris tiap hari dari tanggal 2026-08-01 sampai 2026-08-03"], ["get_top_sellers_by_day"], "kata 'bandingkan' -- pastikan tidak keliru dirutekan ke get_trending_products (itu untuk 2 tanggal TERBARU otomatis, bukan tanggal yang diminta eksplisit)"),

    # --- Tanggal: edge case / boundary / format tidak biasa (10) ---
    ("DTB01", "date_edge", ["produk terlaris tanggal 2026-08-04"], ["get_top_sellers"], "satu hari SETELAH data terakhir (di luar rentang) -- harus tolak jujur rentang tidak tersedia, bukan mengarang"),
    ("DTB02", "date_edge", ["top 2 produk terlaris per hari dari tanggal 2026-07-30 sampai 2026-08-01"], ["get_top_sellers_by_day"], "rentang SEBAGIAN di luar data (cuma 2026-08-01 yang valid) -- baca jawabannya: harus jujur soal bagian rentang yang tidak tersedia, bukan diam-diam cuma nampilin 1 hari tanpa penjelasan"),
    ("DTB03", "date_edge", ["kategori terlaris dari tanggal 2026-08-03 sampai 2026-08-01"], ["get_top_categories"], "start_date > end_date (terbalik) -- cek robustness: model menukar urutan sendiri, atau tool mengembalikan pesan error format; baca jawaban, jangan asumsikan crash otomatis lolos"),
    ("DTB04", "date_edge", ["produk terlaris tanggal 2026-08-01 sampai 2026-08-01"], ["get_top_sellers"], "start_date == end_date, ditulis sebagai rentang -- hasil harus sama dengan filter satu tanggal biasa"),
    ("DTB05", "date_edge", ["top 2 produk terlaris per hari tanggal 2026-08-01 sampai 2026-08-01"], ["get_top_sellers_by_day"], "rentang satu-hari TAPI eksplisit minta 'per hari' -- breakdown-nya cuma 1 blok tanggal, bukan error"),
    ("DTB06", "date_edge", ["kategori terlaris mulai tanggal 2026-08-01 saja, tanpa batas akhir"], ["get_top_categories"], "cuma start_date diisi, end_date kosong -- default ke tanggal maksimum yang tersedia"),
    ("DTB07", "date_edge", ["produk terlaris kategori Minuman sampai tanggal 2026-08-02"], ["get_top_sellers"], "cuma end_date diisi, start_date kosong -- default ke tanggal minimum yang tersedia"),
    ("DTB08", "date_edge", ["penjualan tanggal 2027-01-01 gimana"], ["get_top_categories"], "jauh di luar rentang (tahun berbeda) -- pesan jujur, bukan mengarang"),
    ("DTB09", "date_edge", ["produk terlaris per hari dari tanggal 2020-01-01 sampai 2020-01-03"], ["get_top_sellers_by_day"], "rentang valid formatnya TAPI seluruhnya di luar data (tahun 2020) -- harus tolak jujur, bukan mengembalikan breakdown kosong yang membingungkan"),
    ("DTB10", "date_edge", ["top 2 produk terlaris per hari dengan format tanggal 01-08-2026 sampai 03-08-2026"], ["get_top_sellers_by_day"], "format DD-MM-YYYY (ambigu dgn MM-DD-YYYY) -- model harus konversi ke YYYY-MM-DD (2026-08-01/03) sebelum panggil tool; baca args di tool_calls.log, bukan cuma nama tool"),

    # --- Kombinasi/compound DENGAN filter tanggal (5) ---
    ("CPD01", "compound_date", ["produk terlaris kategori Sabun Mandi tanggal 2026-08-01, dan berapa rentang harganya"], ["get_top_sellers", "get_price_range"], "top_sellers dgn tanggal + price_range dalam satu giliran"),
    ("CPD02", "compound_date", ["top 2 produk terlaris per hari dari tanggal 2026-08-01 sampai 2026-08-03 untuk kategori Minuman, sekalian kasih rentang harganya"], ["get_top_sellers_by_day", "get_price_range"], "per-day breakdown + price_range sekaligus"),
    ("CPD03", "compound_date", ["kategori terlaris tanggal 2026-08-02 dan berapa variasi produknya"], ["get_top_categories", "get_category_assortment"], "top_categories dgn tanggal + assortment"),
    ("CPD04", "compound_date", ["produk terlaris kategori Personal Care tanggal 1 Agustus 2026, lalu cari juga produk cross-sell-nya"], ["get_top_sellers", "find_cross_sell_candidates"], "top_sellers dgn tanggal, lalu cross-sell dari hasilnya"),
    ("CPD05", "compound_date", ["bandingkan kategori Makanan dan Minuman mana yang lebih laris tanggal 2026-08-03"], ["get_top_categories"], "perbandingan dua kategori spesifik + filter tanggal, cek tidak mengarang angka"),

    # --- Regresi drift-bahasa: segmen dengan padanan Inggris jelas (6) ---
    ("LD01", "language_drift", ["produk terlaris kategori minuman"], ["get_top_sellers"], "'minuman'='drink' -- kalau argumen tool diam-diam diterjemahkan ke Inggris, hasilnya kosong; baca jawabannya, harus berisi produk nyata bukan 'tidak ada produk yang cocok'"),
    ("LD02", "language_drift", ["kategori makanan penjualannya berapa"], ["get_top_categories"], "'makanan'='food' -- cek jawaban bukan penolakan kosong"),
    ("LD03", "language_drift", ["cari susu cair rendah lemak yang lagi trending"], ["get_trending_products"], "'susu'='milk' -- cek jawaban bukan penolakan kosong"),
    ("LD04", "language_drift", ["produk terlaris kategori minuman tanggal 2026-08-01"], ["get_top_sellers"], "'minuman'='drink' DIGABUNG filter tanggal -- pastikan drift-safety tetap berlaku walau ada tanggal"),
    ("LD05", "language_drift", ["top 2 produk terlaris per hari kategori makanan dari tanggal 2026-08-01 sampai 2026-08-03"], ["get_top_sellers_by_day"], "'makanan'='food' DIGABUNG breakdown per hari"),
    ("LD06", "language_drift", ["produk terlaris sampo tanggal 2026-08-02"], ["get_top_sellers"], "'sampo'='shampoo' + filter tanggal"),

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

    # --- Edge cases: luar katalog / robustness (7) ---
    ("EDX01", "edge_luar_katalog", ["cari produk pesawat terbang"], ["search_catalog"], "harus bilang tidak ketemu, bukan mengarang"),
    ("EDX02", "edge_luar_katalog", ["asdkjaskdj123 apa itu"], [], "input tidak bermakna, tidak boleh crash"),
    ("EDX03", "edge_luar_katalog", ["apa itu machine learning"], [], "di luar topik, harus menolak sopan bukan menjawab asal"),
    ("EDX04", "edge_luar_katalog", ["produk terlaris kategori Barang Antik"], ["get_top_sellers"], "kategori tidak ada, harus bilang tidak ada data"),
    ("EDX05", "edge_luar_katalog", ["kategoro paling laris apa"], ["get_top_categories"], "typo 'kategoro', tes robustness NLU"),
    ("EDX06", "edge_luar_katalog", ["cari produk unicorn ajaib"], ["search_catalog"], "query nonsens, tidak boleh mengarang produk"),
    ("EDX07", "edge_luar_katalog", ["produk terlaris kategori Kendaraan Bermotor"], ["get_top_sellers"], "kategori tidak ada, harus bilang tidak ada data"),

    # --- Multi-turn (memori sesi dalam satu proses) (6) ---
    ("MT01", "multi_turn", ["produk terlaris kategori Minuman", "yang paling murah dari situ berapa harganya?"], ["get_top_sellers"], "giliran ke-2 seharusnya TIDAK perlu tool baru, jawab dari konteks"),
    ("MT02", "multi_turn", ["kategori mana yang paling sedikit terjual", "kasih rekomendasi produk mirip yang lebih laku untuk kategori itu"], ["get_top_categories", "find_cross_sell_candidates"], "giliran ke-2 butuh tool baru berdasar konteks giliran pertama"),
    ("MT03", "multi_turn", ["produk paling tidak laku di kategori Sabun Mandi", "kalau kategori Sampo gimana?"], ["get_worst_sellers"], "giliran ke-2: segmen baru, harus panggil ulang tool dengan argumen baru"),
    ("MTD01", "multi_turn", ["produk terlaris tanggal 2026-08-01", "kalau tanggal 2026-08-03 gimana?"], ["get_top_sellers"], "giliran ke-2: tanggal baru, harus panggil ulang get_top_sellers dengan start_date/end_date=2026-08-03 (bukan menjawab dari memori giliran 1)"),
    ("MTD02", "multi_turn", ["top 2 produk terlaris per hari dari tanggal 2026-08-01 sampai 2026-08-03", "yang paling laris di tanggal 2026-08-01, harganya berapa?"], ["get_top_sellers_by_day"], "giliran ke-2 TIDAK perlu tool baru -- harga produk itu sudah ada di hasil get_top_sellers_by_day giliran 1, harus jawab dari konteks"),
    ("MTD03", "multi_turn", ["kategori terlaris tanggal 2026-08-01", "gimana kalau dibandingkan dengan tanggal 2026-08-03?"], ["get_top_categories"], "giliran ke-2 butuh PANGGILAN BARU get_top_categories utk tanggal 2026-08-03 (data giliran 1 cuma py 08-01) -- cek tool_calls.log giliran 2 tidak kosong"),

    # --- Kombinasi/compound, tanpa filter tanggal (5) ---
    ("CP01", "compound", ["kasih rekomendasi lengkap: produk terlaris kategori Personal Care, rentang harganya, dan produk cross-sell-nya"], ["get_top_sellers", "get_price_range", "find_cross_sell_candidates"], "3 tool dalam satu giliran"),
    ("CP02", "compound", ["bandingkan kategori Makanan dan Minuman, mana yang lebih laris"], ["get_top_categories"], "perbandingan, cek tidak mengarang angka"),
    ("CP03", "compound", ["kategori mana yang butuh perhatian: variasi produk sedikit tapi penjualan juga rendah"], ["get_category_assortment", "get_top_categories"], "gabungan assortment + top_categories"),
    ("CP04", "compound", ["cari sabun mandi murah lalu kasih tau juga produk cross-sell yang cocok"], ["search_catalog", "find_cross_sell_candidates"], "search + cross-sell dalam satu giliran"),
    ("CP05", "compound", ["produk terlaris kategori Kebutuhan Dapur dan berapa jumlah variasi produk di kategori itu"], ["get_top_sellers", "get_category_assortment"], "top_sellers + assortment"),

    # --- Variasi top_n / boundary (7) ---
    ("BD01", "boundary", ["top 1 produk terlaris kategori Makanan"], ["get_top_sellers"], "top_n=1"),
    ("BD02", "boundary", ["1 kategori paling laris saja"], ["get_top_categories"], "top_n=1"),
    ("BD03", "boundary", ["50 produk terlaris kategori Minuman"], ["get_top_sellers"], "top_n besar"),
    ("BD04", "boundary", ["kategori Sabun Mandi paling laris atau paling sedikit?"], ["get_top_sellers"], "ambigu, cek model minta klarifikasi atau default masuk akal"),
    ("BD05", "boundary", ["produk termurah dan termahal di seluruh katalog"], ["get_price_range"], "tanpa segmen spesifik, tes argumen kosong/luas"),
    ("BD06", "boundary", ["top 100 produk terlaris kategori Minuman"], ["get_top_sellers"], "top_n sangat besar, cek tidak crash"),
    ("BD07", "boundary", ["kategori paling laris atau paling sedikit terjual, yang mana saja boleh"], ["get_top_categories"], "ambigu eksplisit ('yang mana saja boleh') -- cek default masuk akal"),
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
            print(f"[{i}/{total}] {case_id} ({tool_area}): {questions}", flush=True)
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
