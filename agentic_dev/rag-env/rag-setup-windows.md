# Setup RAG Lokal untuk Rekomendasi & Analisis Penjualan (Windows + Ollama)

## Spesifikasi Environment

| Item | Detail |
|---|---|
| OS | Windows (native, tanpa WSL) |
| VRAM | 8 GB |
| RAM | 32 GB DDR5 |
| Runtime LLM | Ollama |
| Model LLM | `qwen3-agent:latest` (5.2 GB, hasil `ollama cp`/Modelfile dari `qwen3:8b`, mendukung tool-calling) |
| Model Embedding | `nomic-embed-text` (sudah di-pull) |
| Vector DB | ChromaDB (persistent, lokal, path `D:/agentic/chroma_db`) |
| Agent Framework | `google-adk` (2.8.0+, dengan extra `[extensions]` untuk LiteLLM) |

## Tujuan Sistem

Awalnya sistem ini dirancang sebagai RAG sederhana (retrieve produk -> generate
jawaban), lalu jadi agen tool-calling manual (loop `ollama.chat()` sendiri),
dan sekarang jadi **agen berbasis `google-adk`** yang menggabungkan dua sumber
data untuk memberi rekomendasi bisnis, bukan sekadar menjawab pertanyaan produk:

1. Pengguna memasukkan sebuah **segmen produk** (kategori/kata kunci, mis. "sabun mandi"),
   atau pertanyaan lanjutan yang merujuk ke percakapan sebelumnya.
2. `root_agent` (`qwen3-agent:latest` via ADK `LiteLlm` -> Ollama) tidak punya tool
   sendiri -- tugasnya cuma merutekan pertanyaan ke salah satu spesialis
   (`kategori_specialist` atau `produk_specialist`), yang baru memanggil tool
   yang sesuai: produk/kategori terlaris atau paling tidak laku, kesenjangan
   variasi produk per kategori, rentang harga suatu segmen, atau produk mirip
   berpenjualan rendah sebagai kandidat cross-sell. Daftar lengkap tool ada di
   bagian [Tools yang Tersedia](#tools-yang-tersedia) di bawah; pengelompokan
   router+spesialis (hasil migrasi Graph) ada di `infrastructure_agentic.md`
   bagian Graph.
3. Model menyusun rekomendasi akhir: temuan yang relevan dan alasannya.

**Kenapa ADK, bukan loop manual:** ADK's `Runner` + `InMemorySessionService`
memberi **session state** -- pertanyaan lanjutan dalam satu run yang sama
(mis. "dari rekomendasi tadi, mana yang paling murah?") tetap ingat konteks
sebelumnya tanpa perlu tool dipanggil ulang. Loop manual sebelumnya tidak
punya mekanisme ini (single-shot: satu segmen, satu jawaban, keluar).
Tools-nya sendiri tetap fungsi Python biasa dengan Google-style docstring -- ADK
yang otomatis mengubahnya jadi tool schema. Jumlahnya sudah bertambah sejak migrasi
awal (lihat [Tools yang Tersedia](#tools-yang-tersedia)); mekanismenya tidak berubah,
tapi sejak migrasi Graph tool-nya sekarang dibagi ke 2 spesialis
(`kategori_specialist`, `produk_specialist`) di balik `root_agent` router, bukan
satu agent datar yang memegang semuanya -- lihat `infrastructure_agentic.md`
bagian Graph untuk detail arsitektur dan status verifikasinya.

Implementasi lengkap ada di [`query.py`](query.py).

## Tools yang Tersedia

Setiap kali sebuah pertanyaan tidak bisa dijawab, penyebabnya salah satu dari tiga
hal: (1) memang belum ada tool untuk itu -- solusinya tambah tool baru, (2) tool-nya
ada tapi argumen/edge case tertentu belum divalidasi -- solusinya tambah guard, bukan
tool baru, atau (3) masalah infrastruktur (mis. tool memblok event loop) -- solusinya
perbaiki plumbing-nya. Jangan langsung menambah tool baru sebelum memastikan yang mana.

| Tool | Sumber data | Untuk pertanyaan seperti |
|---|---|---|
| `get_top_categories(top_n, terendah, start_date, end_date)` | transaction_data/transaction_day*.csv | "kategori apa yang paling laris" / "paling sedikit", opsional difilter tanggal/rentang tanggal |
| `get_top_sellers(segment, top_n, start_date, end_date)` | transaction_data/transaction_day*.csv | "produk terlaris" secara umum ATAU di kategori/segmen X (segmen kosong = seluruh data), opsional difilter tanggal/rentang tanggal -- SATU ranking gabungan untuk seluruh rentang |
| `get_top_sellers_by_day(segment, top_n, start_date, end_date)` | transaction_data/transaction_day*.csv | sama seperti `get_top_sellers`, TAPI untuk pertanyaan yang eksplisit minta breakdown "per hari"/"tiap hari" lintas beberapa tanggal -- ranking TERPISAH per tanggal, bukan satu ranking gabungan (lihat Known Issues: gabung-jadi-satu bisa menyembunyikan pemenang harian yang sebenarnya) |
| `get_trending_products(segment, top_n)` | transaction_data/transaction_day*.csv | "produk apa yang lagi naik daun/trending di segmen X" -- bandingkan net qty tanggal terbaru vs tanggal sebelumnya di data |
| `get_trending_categories(top_n)` | transaction_data/transaction_day*.csv | "kategori apa yang lagi naik daun/trending" -- versi level-kategori dari tool di atas |
| `get_peak_hours(segment, top_n)` | transaction_data/transaction_day*.csv | "jam berapa penjualan paling ramai" (segmen kosong = seluruh data), untuk keputusan staffing/timing promo |
| `get_worst_sellers(segment, top_n)` | katalog_produk.csv | "produk paling tidak laku / belum pernah terjual" |
| `get_category_assortment(top_n, terendah)` | katalog_produk.csv | "kategori dengan variasi produk paling sedikit/banyak" |
| `get_price_range(segment)` | katalog_produk.csv | "rentang harga produk di kategori/segmen X" (segment kosong = seluruh katalog) |
| `find_cross_sell_candidates(product_name, top_k)` | katalog_produk.csv + ChromaDB | "produk apa yang cocok dipromosikan bareng produk X" |
| `search_catalog(query, category, max_price)` | katalog_produk.csv + ChromaDB | pencarian bebas berdasarkan kemiripan makna |

**Rentang waktu yang didukung:** sejak `transaction_time` ditambahkan ke data
transaksi (lihat `2026-09-06-transaction-data-v2-design.md`), tool-tool di atas BISA
menjawab pertanyaan bertema waktu -- tapi cuma untuk tanggal yang benar-benar ada di
`transaction_data/transaction_day*.csv` yang ter-load. Rentang yang tersedia
dihitung otomatis dari data saat itu (bukan hardcode di prompt) dan disisipkan ke
instruksi agen setiap giliran, sama seperti info katalog di `_build_produk_instruction`.
Pertanyaan dengan tanggal/rentang DI LUAR data yang ter-load, atau frasa relatif yang
cakupannya tidak jelas (mis. "bulan lalu" kalau data cuma mencakup beberapa hari),
tetap ditolak jujur oleh system prompt, bukan dikarang. **Yang masih tidak bisa
dijawab sama sekali:** pertanyaan per-pelanggan (mis. "pelanggan mana yang paling
sering beli X") -- data tidak punya kolom `user_id` sama sekali, terlepas dari
kolom waktu.

## Dua Sumber Data, Dua Peran Berbeda

| Sumber | Isi | Peran |
|---|---|---|
| `katalog_produk.csv` + koleksi ChromaDB `products` | Produk hasil crawl (nama, kategori, harga, deskripsi, **terjual**) | Pencarian semantik produk + metadata terstruktur. Kolom `terjual` diisi dari agregasi `transaction_data/transaction_day*.csv` (net qty, lihat di bawah). |
| `transaction_data/transaction_day*.csv` | Log transaksi mentah, satu file per hari (mis. `transaction_day1.csv` = 2026-08-01), @ ~1 juta baris: `product_name, product_category_name_lvl_0, product_price, product_short_desc, transaction_time, item_qty` | Dipakai **live** untuk hitung produk terlaris per segmen (`get_top_sellers`), tren antar hari, jam ramai, dan filter tanggal. |

**Terjual = net qty, bukan jumlah baris:** `item_qty` bisa negatif (retur) --
"terjual" yang dilaporkan semua tool adalah `sum(item_qty)` per produk/kategori
(retur mengurangi angka), BUKAN jumlah baris transaksi seperti implementasi lama
sebelum kolom ini ada.

**Keterbatasan yang masih berlaku:** data transaksi **tidak punya kolom
`user_id`** -- riwayat/rekomendasi per-pengguna (`get_user_history()` dari TODO
awal) tetap tidak bisa dibangun dari data ini tanpa kolom tambahan. Kolom waktu
(`transaction_time`) SUDAH ada sejak `2026-09-06-transaction-data-v2-design.md`,
jadi pertanyaan bertema waktu sekarang bisa dijawab selama tanggalnya ada di data
yang ter-load -- lihat catatan "Rentang waktu yang didukung" di bagian
[Tools yang Tersedia](#tools-yang-tersedia).

## Struktur Folder (aktual)

```
D:\agentic\
├── agentic_dev\rag-env\           # virtual environment + semua script
│   ├── crawl_alfagift.py          # crawl seluruh katalog alfagift.id -> katalog_produk.csv
│   ├── aggregate_sales.py         # agregasi transaction_data/transaction_day*.csv -> kolom "terjual" (net qty) di katalog
│   ├── build_index.py             # index katalog_produk.csv (+terjual) ke ChromaDB
│   ├── query.py                   # agen tool-calling: segmen -> rekomendasi
│   ├── test_agent_cases.py        # 117 kasus uji regresi thd root_agent (router + 2 spesialis sejak migrasi Graph, lihat infrastructure_agentic.md bagian Graph), lihat PANDUAN_PENGGUNAAN.md
│   ├── test_report.jsonl          # hasil run test_agent_cases.py terakhir (per-kasus, JSON lines)
│   ├── benchmark_verify_loop.py   # ukur biaya programatik vs LoopAgent vs hybrid utk loop verifikasi -- lihat infrastructure_agentic.md bagian Graph #Bagian 2
│   ├── tool_calls.log             # log tiap panggilan tool: nama, argumen, durasi, status
│   ├── rag-setup-windows.md       # dokumen ini (setup, arsitektur, troubleshooting)
│   ├── infrastructure_agentic.md  # peta Prompt/Context/Harness/Loop/Graph proyek ini
│   └── PANDUAN_PENGGUNAAN.md      # cara pakai & cara testing agen sehari-hari
├── chroma_db\
│   ├── katalog_produk.csv         # id, nama, kategori, harga, deskripsi, terjual
│   ├── agent_sessions.db          # riwayat percakapan (SqliteSessionService), lintas-restart
│   └── (file persistent ChromaDB)
└── transaction_data\
    ├── transaction_day1.csv       # log transaksi mentah 2026-08-01 (~1 juta baris)
    ├── transaction_day2.csv       # log transaksi mentah 2026-08-02 (~1 juta baris)
    └── transaction_day3.csv       # log transaksi mentah 2026-08-03 (~1 juta baris) -- dst,
                                    # file baru transaction_dayN.csv otomatis ter-load (glob)
```

> Catatan: struktur folder ini menyimpang dari rencana awal (`C:\rag-project\`)
> -- semua path sekarang absolut dan konsisten mengarah ke lokasi di atas,
> dipilih langsung saat development, bukan default lama.

## Alur Kerja

### 1. Crawl katalog produk

```powershell
cd D:\agentic\agentic_dev\rag-env
.\Scripts\python.exe crawl_alfagift.py
```

Crawl seluruh 51 kategori alfagift.id via Playwright (headless Chromium) +
network interception ke API `webcommerce-gw.alfagift.id`. **Resumable**: aman
di-Ctrl+C, jalankan lagi untuk lanjut (skip id yang sudah tercatat). Bisa makan
waktu 2-3+ jam untuk seluruh katalog (~15.000-20.000 produk).

### 2. Agregasi data penjualan jadi sinyal popularitas

```powershell
.\Scripts\python.exe aggregate_sales.py
```

Jalankan **setelah** crawl selesai sepenuhnya (bukan sambil crawl jalan --
keduanya menulis file yang sama dan bisa balapan/race condition). Script ini
melakukan full recompute dari seluruh `transaction_data/transaction_day*.csv`
yang ada (net qty, bukan jumlah baris -- lihat
`2026-09-06-transaction-data-v2-design.md`), jadi aman dijalankan ulang kapan
saja setelah katalog final ATAU setelah ada file `transaction_dayN.csv` baru.

### 3. Build index ChromaDB

```powershell
.\Scripts\python.exe build_index.py
```

Meng-embed setiap baris katalog (termasuk `terjual`) dengan `nomic-embed-text`
dan menyimpannya ke koleksi ChromaDB `products`.

**Kenapa `build_index.py` lambat:** satu baris = satu panggilan HTTP embedding
ke Ollama, dijalankan berurutan (bukan batch, bukan paralel). Diukur langsung:
~4 baris/detik (~0.24 detik/baris). Untuk katalog 8.000-an baris itu ~30-35
menit; untuk katalog penuh 15.000-20.000 baris bisa ~1-1.5 jam. Script sudah
mencetak progress (`[i/total] ... estimasi sisa X menit`) tiap 50 baris supaya
jelas masih berjalan, bukan macet. Mempercepatnya (batching/paralel request ke
Ollama) belum dilakukan -- lihat TODO.

### 4. Jalankan agen

```powershell
pip install "google-adk[extensions]"   # sekali saja, kalau belum
.\Scripts\python.exe query.py
```

Membuka sesi interaktif multi-turn: ketik segmen produk atau pertanyaan
lanjutan, ketik `keluar` untuk berhenti. Selama proses masih berjalan,
konteks percakapan (rekomendasi sebelumnya) tetap diingat.

## Prasyarat

- [x] Ollama terpasang (native Windows), server jalan (`ollama serve` / tray app)
- [x] Model LLM `qwen3-agent:latest` tersedia dan mendukung tool-calling
- [x] Model embedding `nomic-embed-text` sudah di-pull
- [x] Python 3.10+ terpasang (di virtual environment `rag-env`)
- [x] `google-adk[extensions]` terpasang (extra `[extensions]` wajib untuk `LiteLlm`)
- [x] Data katalog produk (hasil crawl, kolom id/nama/kategori/harga/deskripsi/terjual)
- [x] Data transaksi mentah (`transaction_data/transaction_day*.csv`, disediakan pengguna, satu file per hari)

## Batasan VRAM 8GB / RAM 32GB

- `num_ctx` dijaga di 4096-8192 -- context window besar lebih cepat bikin VRAM overflow dibanding ukuran model itu sendiri
- ChromaDB sepenuhnya jalan di RAM/disk, tidak memakai VRAM sama sekali
- Kalau VRAM masih mepet, embedding bisa dipindah ke CPU pakai `sentence-transformers` (`all-MiniLM-L6-v2`) -- 32GB RAM lebih dari cukup untuk itu
- Cek pemakaian VRAM: Task Manager -> tab Performance -> GPU -> "Dedicated GPU memory usage", atau `nvidia-smi` di PowerShell
- Cek Ollama benar-benar pakai GPU: `ollama ps` saat model aktif, kolom `PROCESSOR` harus menunjukkan persentase GPU

## Known Issues / Troubleshooting

- **Execution policy error di PowerShell** -> `Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass`
- **Model lambat / RAM ter-swap berat** -> turun ke quant lebih kecil, mis. `qwen3-agent:8b-q4_K_S`, atau model 4B
- **Windows Defender memperlambat crawl/build index pertama kali** -> exclude folder `D:\agentic` di Windows Security -> Virus & threat protection -> Exclusions
- **Path error di Python** -> selalu pakai forward slash `/` meski di Windows, hindari backslash mentah
- **`katalog_produk.csv` punya baris dengan jumlah kolom tidak konsisten** -> terjadi kalau `aggregate_sales.py` dijalankan SAAT `crawl_alfagift.py` masih aktif menulis file yang sama. Jangan jalankan keduanya bersamaan; kalau sudah terjadi, baris pendek bisa ditambal manual dengan nilai `terjual=0` di kolom terakhir.
- **`ollama` tidak ditemukan di PATH** -> pastikan proses Ollama (`ollama serve` / tray app) berjalan; cek dengan `curl http://localhost:11434/api/tags`
- **`ImportError: LiteLLM support requires: pip install google-adk[extensions]`** -> `google-adk` dasar tidak menyertakan LiteLLM; install dengan extra `[extensions]`.
- **Jawaban akhir berisi monolog/reasoning model, bukan jawaban bersih** -> `qwen3-agent:latest` mengembalikan reasoning trace sebagai `Part` terpisah dengan `thought=True`. Kalau kode mengambil `event.content.parts[0].text` tanpa filter, yang terambil bisa jadi reasoning-nya, bukan jawaban akhir. `query.py` sudah memfilter part yang `thought=True`.
- **`IndexError: list index out of range` di `chromadb`'s `normalize_embeddings`** -> terjadi kalau tool `search_catalog`/`find_cross_sell_candidates` dipanggil model dengan argumen string kosong; `ollama.embeddings(prompt="")` diam-diam mengembalikan vektor kosong. `query.py` sudah menolak argumen kosong sebelum memanggil embedding.
- **Traceback `LiteLLM:ERROR: logging_worker.py ... TimeoutError` muncul di terminal saat agen jalan** -> tidak fatal (dicatat lalu ditelan di dalam LiteLLM sendiri, tidak menghentikan `query.py`), tapi menandakan event loop asyncio sedang macet. Root cause: `google-adk` memanggil tool sinkron langsung di event loop kalau tidak dibungkus `async` (lihat `google.adk.tools.function_tool._invoke_callable`), dan tool di sini melakukan panggilan HTTP ke Ollama + scan pandas atas `transaction_data/transaction_day*.csv` (~3 juta baris gabungan sejak `transaction_time`/`item_qty` ditambahkan, dulu ~1.5 juta baris di satu file) -- selama itu event loop beku, termasuk task logging internal LiteLLM yang punya batas waktu 20 detik (`LOGGING_WORKER_MAX_TIME_PER_COROUTINE`). Semua tool di `query.py` sudah dibungkus `async def` yang menjalankan implementasinya lewat `asyncio.to_thread` untuk menghindari ini. Kalau muncul lagi setelah menambah tool baru, kemungkinan tool itu belum dibungkus dengan pola yang sama.
- **Agen menjawab "tool tidak mendukung" untuk pertanyaan yang masuk akal** (mis. dulu tidak ada cara mencari kategori dengan penjualan terendah) -> ini bukan bug, tapi tanda ada kesenjangan kemampuan tool. Solusinya tambah tool baru (lihat [Tools yang Tersedia](#tools-yang-tersedia)) -- tapi cek dulu apakah ini benar-benar kesenjangan kemampuan, bukan sekadar argumen yang belum divalidasi di tool yang sudah ada (dua hal itu butuh perbaikan berbeda).
- **(Ditemukan & diperbaiki lewat `test_agent_cases.py`, 117 kasus)** `get_price_range` menolak segment kosong ("Segmen kosong, tidak bisa menghitung rentang harga") padahal pertanyaan "rentang harga seluruh katalog" itu valid -> tidak konsisten dengan `get_worst_sellers` yang sudah memperlakukan segment kosong sebagai "seluruh katalog". Diperbaiki: segment kosong sekarang menghitung rentang harga seluruh `katalog_df` (8.461 produk), bukan menolak.
- **(Ditemukan & diperbaiki lewat laporan pengguna langsung)** `get_top_sellers` punya BUG YANG SAMA PERSIS dengan poin `get_price_range` di atas -- ini luput sebelumnya karena SELURUH kasus `TS01`-`TS14` di `test_agent_cases.py` sengaja menyebut segmen/kategori, jadi tidak ada satu kasus pun yang menguji "top N produk terlaris" polos tanpa segmen. Efeknya: pertanyaan seumum "top 2 produk terlaris" (tanpa nama produk/kategori) SELALU ditolak ("Segmen kosong, tidak bisa mencari produk terlaris.") walau data transaksinya jelas ada -- diverifikasi lewat `python -c` langsung terhadap 3 juta baris data nyata. Diperbaiki sama seperti `get_price_range`: segmen kosong sekarang berarti "seluruh data", bukan error (lihat `_filter_by_segment` di `query.py`). Ditemukan BERSAMAAN dengan gap kedua: `start_date`/`end_date` di `get_top_sellers` cuma mendukung SATU rentang yang di-agregasi jadi satu ranking gabungan -- untuk pertanyaan yang eksplisit minta breakdown "per hari" lintas beberapa tanggal (mis. "top 2 terlaris per hari tanggal 2026-08-01 dan 2026-08-03"), ranking gabungan itu BISA MENYESATKAN karena pemenang harian yang sebenarnya bisa berbeda dari pemenang rentang gabungan (diverifikasi dengan data nyata: pemenang gabungan 3-hari beda dari pemenang tiap hari individual). Tool baru `get_top_sellers_by_day` ditambahkan untuk kasus ini -- ranking terpisah per tanggal, bukan satu angka gabungan.
- **(Ditemukan lewat suite 200-kasus, diperbaiki sebagian -- lihat `test_report.jsonl` untuk baseline "sebelum" dan `test_report_date_subset_rerun.jsonl` untuk "sesudah")** Begitu `get_top_sellers_by_day` ada, model kadang bingung memilih ANTARA `get_top_sellers`/`get_top_sellers_by_day` di kedua arah -- kadang breakdown per-hari dijawab pakai ranking gabungan (silent, tanpa kutipan bahwa itu bukan per-hari), kadang pertanyaan tanggal/rentang BIASA malah dipecah per hari padahal tidak diminta. Akar masalah: kalimat aturan di `PRODUK_INSTRUCTION` ("...or names more than one specific date") ambigu -- rentang tanggal APA PUN pada dasarnya "menyebut lebih dari satu tanggal", jadi kalimat itu sendiri yang menciptakan sinyal palsu. Diperbaiki: aturan ditulis ulang jadi murni berbasis kata kunci eksplisit ("per hari"/"tiap hari"/"setiap hari"/"harian"/daftar 3+ tanggal satu-satu) -- rentang tanggal semata TIDAK PERNAH jadi alasan pakai tool per-hari. Diukur pada 62 kasus tanggal/rentang/per-hari/language-drift: mismatch mentah turun dari 40% (25/62) ke 21% (13/62), TAPI investigasi lebih dalam menunjukkan sebagian besar 13 sisa itu BUKAN bug nyata (5 kasus: penolakan jujur di luar rentang lewat jalur berbeda dari yang diasumsikan skenario uji; 1 kasus: rentang satu-hari yang hasilnya identik lewat tool mana pun; jadi tingkat masalah NYATA setelah investigasi manual cuma ~3/62 (~5%): dua kasus miss per-hari yang persisten (`PD06`, `PD10`) dan satu regresi baru murni karena stokastisitas model (`PD14`, prompt SAMA gagal setelah sebelumnya lolos). Ditambahkan kanari observability `_log_possible_per_day_miss()` (baris `NOTE ... KEMUNGKINAN PER-HARI MISS` di `tool_calls.log`) untuk melacak kemunculan pola ini di pemakaian nyata TANPA mengubah jawaban -- didokumentasikan dulu (pola sama seperti `_warn_if_session_growing`), bukan langsung dibangun mekanisme auto-retry yang lebih kompleks, karena bukti nyata seberapa sering ini terjadi di luar suite pengujian belum ada. **Retraining model TIDAK dilakukan** -- tidak ada pipeline fine-tuning di proyek ini, dan sisa miss-rate ini adalah stokastisitas model 8B lokal pada parafrase, bukan model yang secara fundamental tidak paham konsepnya (di 200-kasus penuh, tool yang benar tetap dipanggil di mayoritas kasus).
- **(Ditemukan bersamaan di suite 200-kasus, diperbaiki)** `kategori_specialist` kadang menolak pertanyaan PERBANDINGAN antar kategori bernama yang DIGABUNG dengan filter tanggal (mis. "bandingkan kategori Makanan dan Minuman mana yang lebih laris tanggal 2026-08-03"), padahal versi TANPA tanggalnya (`CP02`) sudah terjawab benar dan `get_top_categories` memang mendukung `start_date`/`end_date` untuk kasus ini -- model salah mengira ini butuh kemampuan yang tidak ada. Diperbaiki: `KATEGORI_INSTRUCTION` sekarang eksplisit bilang perbandingan 2+ kategori bernama TETAP pakai `get_top_categories` (tidak ada tool "bandingkan kategori" terpisah -- panggil dengan `top_n` cukup besar lalu ambil baris yang relevan dari hasilnya), dan bahwa ini digabung bebas dengan filter tanggal. Diverifikasi ulang secara live: jawaban sekarang menyebut angka nyata (Minuman 343.584x vs Makanan 300.466x pada 2026-08-03), dicocokkan langsung ke `_get_top_categories_impl` -- bukan penolakan lagi.
- **(Ditemukan, belum diperbaiki -- diagnosis lebih lanjut diperlukan sebelum memutuskan perbaikan)** Kasus `EDW03` ("produk apa yang laku hari ini") kadang GAGAL menolak -- model diam-diam menganggap "hari ini" = tanggal terakhir yang ada di data (2026-08-03) dan menjawab percaya diri, alih-alih menolak seperti 3 kasus penolakan waktu-relatif lain (`EDW01`/`EDW02`/`EDW04`) yang konsisten benar. Ini bukan regresi baru dari `get_top_sellers_by_day` (risiko yang sama sudah ada sejak `get_top_sellers` punya `start_date`/`end_date`), tapi jadi lebih kelihatan sekarang karena lebih banyak tool tanggal tersedia. Belum diperbaiki sengaja -- baru 1 dari 4 kasus waktu-relatif yang gagal di satu kali running (model probabilistik, perlu beberapa kali ulang untuk memastikan ini pola konsisten atau kebetulan sebelum menulis ulang instruksi lagi).
- **(Dicoba & DIBATALKAN setelah diukur -- lihat `temp_experiment_baseline.jsonl`/`temp_experiment_after.jsonl`, 2026-09-06)** Hipotesis: menurunkan `temperature` LiteLlm dari default Modelfile (0.6, default resmi Qwen3 thinking-mode) ke 0.2 akan mengurangi flakiness routing `get_top_sellers` vs `get_top_sellers_by_day` yang tersisa (lihat poin di atas), karena tool-routing seharusnya tidak butuh keragaman kreatif. Diuji lewat eksperimen berpasangan 20-trial (4 pertanyaan x 5 percobaan, termasuk 1 kasus kontrol yang sudah konsisten benar dan 2 kasus miss yang persisten) SEBELUM mengubah kode apa pun, baru mengubah `temperature`, lalu mengulang 20 trial yang SAMA PERSIS untuk perbandingan apple-to-apple. Hasil: turun dari 11/20 (55%) ke 8/20 (40%) -- LEBIH BURUK, bukan lebih baik. Kasus `PD10` khususnya turun dari 3/5 ke 0/5. Penjelasan: temperature mengontrol VARIANCE, bukan MODE (jawaban paling mungkin) -- kalau jawaban paling mungkin model untuk suatu parafrase memang salah (`PD06`/`PD10`), menurunkan temperature membuat model makin konsisten mengulang jawaban salah itu, bukan membetulkannya; cuma menguntungkan kasus yang jawaban paling mungkinnya SUDAH benar (`PD01`/`PD14`, keduanya tidak berubah). **Perubahan dibatalkan/dikembalikan ke default Modelfile** setelah pengukuran ini -- pelajarannya: sampling-parameter tuning bukan lever yang tepat untuk bias tool-choice yang konsisten pada parafrase tertentu.
- **(Diperbaiki lewat backstop deterministik, bukan prompt/sampling -- lihat `temp_experiment_autocorrect.jsonl`, 2026-09-06)** Setelah instruksi & sampling-parameter TERBUKTI tidak cukup (dua poin di atas) untuk sisa miss-rate `get_top_sellers` vs `get_top_sellers_by_day`, `_maybe_correct_per_day_miss()` ditambahkan sebagai koreksi AKTIF (bukan cuma log) di `_log_after_tool`: kalau model memanggil `get_top_sellers` dengan rentang multi-hari nyata (`start_date != end_date`, keduanya terisi) PADAHAL pertanyaan menyebut sinyal per-hari eksplisit, `tool_response`-nya diganti otomatis dengan hasil `_get_top_sellers_by_day_impl` (argumen sama) SEBELUM spesialis sempat menulis jawaban dari data yang salah -- memanfaatkan kontrak `after_tool_callback` ADK yang dikonfirmasi langsung dari source terinstall (`google/adk/flows/llm_flows/functions.py` "Step 6: if alternative response exists from after_tool_callback, use it instead of the original function response"). Diverifikasi ulang dengan 20 trial SAMA PERSIS seperti eksperimen temperature (dibandingkan lewat isi angka jawaban terhadap ground truth `_get_top_sellers_by_day_impl`, BUKAN cuma nama tool yang terpanggil -- nama tool di tool_calls.log tetap "get_top_sellers" walau kontennya sudah dikoreksi): **11/20 (55%) -> 19/20 (95%)**, termasuk `PD06` yang tadinya 0/5 KONSISTEN jadi 5/5 bersih. Satu sisa kegagalan (`PD10` trial 5) BUKAN bug koreksi -- `tool_calls.log` mengonfirmasi koreksi tetap terpanggil dan data yang dikirim ke spesialis sudah benar (breakdown 3 hari lengkap), TAPI jawaban akhir spesialis cuma merangkum hari pertama saja sambil tetap mengklaim mencakup seluruh rentang -- ini masalah BERBEDA (kelengkapan ringkasan LLM atas hasil tool multi-bagian), bukan lagi soal tool/data yang salah, didokumentasikan di sini untuk kerja lanjutan, belum diperbaiki.
- **(Ditemukan & diperbaiki, lalu diselesaikan penuh lewat `2026-09-06-transaction-data-v2-design.md`)** Pertanyaan tren tanpa kata waktu eksplisit (mis. "kategori apa yang lagi tren sekarang") tidak tertangkap aturan penolakan bertema waktu di system prompt -- karena instruksinya cuma memberi contoh frasa eksplisit ("minggu ini", "bulan lalu"), model menjawabnya seolah itu pertanyaan "kategori paling laris total" biasa (mengarang kesan tren dari angka akumulasi). Perbaikan awal: system prompt eksplisit menyebut kata seperti "tren", "lagi hits/viral", "terkini", "belakangan ini" sebagai sinyal tema waktu yang tetap harus ditolak. **Resolusi final:** sejak `transaction_time` ditambahkan ke data, pertanyaan ini TIDAK PERLU ditolak lagi -- `get_trending_categories`/`get_trending_products` sekarang benar-benar menjawabnya (membandingkan net qty dua tanggal terbaru di data). Persis frasa ini jadi kasus uji `TR01` di `test_agent_cases.py`, dan sekarang lolos sebagai JAWABAN (bukan penolakan).
- **(Ditemukan & diperbaiki)** `find_cross_sell_candidates` kadang tidak terpanggil walau pertanyaan eksplisit minta "produk serupa/mirip" -- terutama kalau ada embel-embel lain ("...yang penjualannya rendah", "...yang lebih laku untuk kategori itu") atau kalau nama produk konkretnya baru didapat dari tool lain di giliran yang sama (mis. turunan dari kategori). Model cenderung berhenti di tool pertama dan menyuruh pengguna mencari sendiri, alih-alih mengklaim hubungan cross-sell tanpa membuktikannya lewat tool. Diperbaiki: aturan #6 di system prompt sekarang eksplisit mengizinkan/mendorong chaining dua tool dalam satu giliran (cari nama produk konkret dulu, baru panggil `find_cross_sell_candidates` dengan nama itu) dan melarang mengklaim cross-sell tanpa memanggil tool-nya.
- **(Ditemukan, belum diperbaiki -- masalah data, bukan bug kode)** Kolom `product_category_name_lvl_0` di `transaction_data/transaction_day*.csv` kadang berisi label yang tidak nyambung dengan produknya (mis. produk sampo Zinc tercatat berkategori "Fashion (Old)"). `get_top_sellers` menampilkan kolom ini apa adanya, jadi noise data mentah ini ikut terlihat pengguna. Bukan bug -- `get_top_sellers` menemukan produknya lewat pencocokan `segment` di `product_name` ATAU `product_category_name_lvl_0`, jadi hasilnya tetap benar meski label kategori yang ditampilkan salah. Kalau mau dibersihkan: ganti kolom kategori yang ditampilkan `get_top_sellers` dengan join ke `katalog_df["kategori"]` (lebih bersih) alih-alih memakai `product_category_name_lvl_0` mentah dari `transaction_data/transaction_day*.csv`.
- **(Ditemukan & diperbaiki, ditemukan lewat testing manual di luar 104-kasus)** `search_catalog` dengan argumen `category` bisa mengembalikan "Tidak ada produk yang cocok" padahal produknya jelas ada -- akar masalahnya sama dengan poin di atas: `katalog_produk.csv["kategori"]` (mis. "Sabun Mandi") dan `transaction_data/transaction_day*.csv["product_category_name_lvl_0"]` (mis. "Personal Care") pakai vokabuler berbeda untuk kategori yang sama. Model kadang mengisi `category` dengan istilah dari vokabuler `get_top_sellers` (mis. "Personal Care") padahal `search_catalog` cuma mengenal vokabuler `katalog_df["kategori"]` -- filternya lalu tidak cocok dengan SATU PUN dari 15 kandidat semantik teratas, hasilnya kosong walau pencarian semantiknya sendiri dapat kandidat relevan. Diperbaiki: kalau filter kategori bikin nol hasil padahal hasil semantik (tanpa filter kategori) tidak kosong, `search_catalog` sekarang menurunkan filter itu jadi catatan ("Kategori 'X' tidak ketemu persis...") dan tetap menampilkan hasil pencarian semantiknya, bukan mengembalikan nol hasil. Diverifikasi: `_search_catalog_impl("sabun mandi", "Personal Care", 0)` sekarang mengembalikan produk sabun mandi asli (dulu: "Tidak ada produk yang cocok"), sementara `category="Sabun Mandi"` (vokabuler yang benar) tetap memfilter seperti biasa.
- **Baris `verify_and_revise | MASIH MISMATCH...` muncul di `tool_calls.log`** -> bukan crash, ini observability yang disengaja dari loop verifikasi akurasi (`verify_and_revise()` di `query.py`, lihat `infrastructure_agentic.md` bagian Graph #Bagian 2). Artinya: draft jawaban punya angka yang tidak cocok data tool, langkah revisi otomatis mencoba memperbaiki, TAPI hasil revisinya sendiri masih tidak cocok -- diamati langsung ini BISA terjadi (model revisi berbasis LLM lokal, probabilistik, bukan jaminan keras) meski jarang. Tidak ada retry lanjutan (disengaja, supaya biaya tetap murah) -- kalau baris ini sering muncul, jawaban yang dikirim ke pengguna kemungkinan masih salah dan perlu dicek manual dari `draft_nums`/`revised_nums`/`tool_nums` yang dicatat di baris yang sama.
- **(Ditemukan lewat testing interaktif pengguna di luar 104-kasus, MITIGASI BERLAPIS -- bukan satu fix tunggal, lihat `infrastructure_agentic.md` bagian Graph #4 untuk kronologi lengkap)** Pertanyaan yang dibungkus sebagai permintaan kreatif/strategis (mis. "berikan aku 5 rekomendasi promo yang bisa dibuat berdasarkan 5 produk terlaris") kadang membuat root menjawab lancar dengan nama produk, harga, dan persentase diskon spesifik TANPA memanggil tool apa pun -- dikonfirmasi lewat `tool_calls.log` (nol baris baru untuk giliran itu). Percobaan 1 (paragraf baru di `ROOT_INSTRUCTION`) mengurangi tapi TIDAK menghilangkan masalah -- pengguna mengulang pertanyaan identik dan bug kambuh lagi. Percobaan 2 (paksa tool-call di level API lewat `tool_choice`) gagal total: LiteLLM membuang parameter itu untuk provider `ollama_chat` (`llms/ollama/chat/transformation.py:186`, komentar sumbernya sendiri "causes ollama requests to hang"). Perbaikan aktual yang dipasang: `verify_and_revise()` sekarang menolak draft jawaban kalau nol tool dipanggil giliran ini TAPI draft tetap menyebut >= 3 angka konkret asing (bukan dari pertanyaan pengguna) -- pola yang cuma terjadi pada fabrikasi, karena penolakan sah (tema waktu/per-pelanggan) tidak pernah menyebut angka spesifik. Draft yang ditolak diganti pesan jujur yang meminta pengguna bertanya ulang lebih eksplisit, bukan dikirim apa adanya.

## TODO — Lanjutan

- [x] Verifikasi `qwen3-agent:latest` mendukung tool-calling (`capabilities: ["completion","tools","thinking"]` dari `/api/tags`)
- [x] Tambah agentic tool-calling layer -- sekarang 7 tool, dipecah jadi 2 spesialis (`kategori_specialist`, `produk_specialist`) di balik `root_agent` router (migrasi Graph Bagian 1, lihat `infrastructure_agentic.md` bagian Graph), lihat [Tools yang Tersedia](#tools-yang-tersedia) untuk daftar lengkap tool
- [x] Migrasi ke `google-adk` untuk session state (percakapan multi-turn dalam satu run)
- [x] Bungkus semua tool jadi `async` + `asyncio.to_thread` supaya tidak memblok event loop (lihat Known Issues soal `LoggingWorker` timeout)
- [x] Sesi persisten lintas-restart lewat `SqliteSessionService` (`agent_sessions.db`), ganti `InMemorySessionService`
- [x] Observability minimal: `before_tool_callback`/`after_tool_callback` mencatat tiap panggilan tool ke `tool_calls.log`
- [x] Uji regresi otomatis: `test_agent_cases.py` (117 kasus/120 giliran), lihat `PANDUAN_PENGGUNAAN.md`
- [x] Instruksi dinamis: `_build_produk_instruction()` (`InstructionProvider`, **REVISI setelah migrasi Graph:** dulu `_build_instruction()` dipasang di satu-satunya root agent, sekarang dipasang hanya di `produk_specialist`) menyisipkan jumlah produk/index/tanggal update ke instruksinya tiap giliran
- [x] Retry sekali untuk panggilan embedding Ollama yang transient-fail (`_embed()` di `query.py`)
- [x] Loop verifikasi akurasi jawaban akhir vs. data tool -- `verify_and_revise()` di `query.py` (opsi hybrid, dipilih lewat benchmark nyata di `benchmark_verify_loop.py`, lihat `infrastructure_agentic.md` bagian Graph #Bagian 2 untuk angka lengkap)
- [ ] `get_user_history()` -- **tidak bisa dibangun** dari `transaction_data/transaction_day*.csv` saat ini -- `transaction_time` sudah ada sejak `2026-09-06-transaction-data-v2-design.md`, tapi tetap tidak ada kolom `user_id`. Perlu sumber data baru kalau personalisasi per-user tetap diinginkan.
- [x] Memori lintas-run -- sudah lewat `SqliteSessionService` persistent (raw histori percakapan bertahan lintas restart)
- [x] Kanari ukuran sesi: `_warn_if_session_growing()` di `query.py` memperingatkan sekali per ambang batas (60/120/240/480/960 event) kalau sesi tunggal-abadi (`SESSION_ID` tetap) mulai mendekati batas `num_ctx=8192` -- **bukan solusi penuh**, cuma sinyal dini; lihat `infrastructure_agentic.md` bagian Context untuk kenapa windowing/summarization sungguhan sengaja ditunda
- [ ] Memori semantik lintas-sesi (`google.adk.memory.BaseMemoryService`) -- **ditunda, keputusan sadar**: `query.py` cuma punya satu `SESSION_ID` abadi, sementara API ini dirancang untuk mencari lintas banyak sesi; dipilih menangani risiko context-window (poin kanari di atas) dulu karena lebih konkret, lihat `infrastructure_agentic.md` bagian Context
- [ ] Percepat `build_index.py` -- saat ini satu request embedding per baris secara berurutan (~4 baris/detik); bisa dipercepat dengan batching atau request paralel ke Ollama kalau GPU/CPU masih ada headroom
- [ ] Tambah error handling: koneksi Ollama gagal, tidak ada file `transaction_data/transaction_day*.csv` yang cocok pola glob
- [x] Uji ketahanan agen terhadap segmen ambigu / di luar katalog -- lewat `test_agent_cases.py` (117 kasus/120 giliran, termasuk kategori EDX/BD khusus untuk ini); 0 crash, 3 bug ditemukan+diperbaiki (lihat Known Issues)
- [x] Evaluasi kualitas retrieval & kualitas rekomendasi cross-sell secara sistematis -- `test_agent_cases.py` (bukan cuma spot-check manual lagi), tapi masih penilaian manual per-kasus (baca jawaban satu-satu), belum ada scoring otomatis -- lihat poin baru di bawah kalau mau dikembangkan lebih jauh
- [ ] Perbaiki kualitas kolom kategori yang ditampilkan `get_top_sellers` -- saat ini pakai `product_category_name_lvl_0` mentah dari `transaction_data/transaction_day*.csv` yang kadang tidak nyambung dengan produknya (lihat Known Issues); pertimbangkan join ke `katalog_df["kategori"]` yang lebih bersih
- [ ] Scoring otomatis untuk `test_agent_cases.py` -- saat ini "benar/salah" masih dinilai manual dengan membaca `test_report.jsonl`; bisa dikembangkan jadi assertion otomatis (mis. cek angka di jawaban cocok dengan hasil tool, bukan cuma cek nama tool yang terpanggil)
- [ ] Setelah crawl penuh selesai: jalankan ulang `aggregate_sales.py` lalu `build_index.py` untuk index produksi final (index saat ini kemungkinan masih index test skala kecil)
- [x] **Implementasi `2026-09-06-transaction-data-v2-design.md`** (sudah diimplementasikan dan diverifikasi lewat regresi live 117-kasus): net qty (`sum(item_qty)`) ganti hitung baris, glob multi-file `transaction_day*.csv`, filter tanggal opsional di `get_top_sellers`/`get_top_categories`, tool baru `get_trending_products`/`get_trending_categories`/`get_peak_hours`, instruksi dwibahasa (Inggris untuk LLM, Indonesia untuk jawaban akhir) + mitigasi drift-bahasa saat ekstraksi entitas, dan regenerasi `katalog_produk.csv` + index ChromaDB dari data transaksi baru
