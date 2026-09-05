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
| `get_top_categories(top_n, terendah)` | transaction_data.csv | "kategori apa yang paling laris" / "paling sedikit" |
| `get_top_sellers(segment, top_n)` | transaction_data.csv | "produk terlaris di kategori/segmen X" |
| `get_worst_sellers(segment, top_n)` | katalog_produk.csv | "produk paling tidak laku / belum pernah terjual" |
| `get_category_assortment(top_n, terendah)` | katalog_produk.csv | "kategori dengan variasi produk paling sedikit/banyak" |
| `get_price_range(segment)` | katalog_produk.csv | "rentang harga produk di kategori/segmen X" (segment kosong = seluruh katalog) |
| `find_cross_sell_candidates(product_name, top_k)` | katalog_produk.csv + ChromaDB | "produk apa yang cocok dipromosikan bareng produk X" |
| `search_catalog(query, category, max_price)` | katalog_produk.csv + ChromaDB | pencarian bebas berdasarkan kemiripan makna |

**Batasan yang berlaku ke semua tool di atas:** tidak ada kolom timestamp/tanggal di
kedua sumber data, jadi tidak ada satu pun tool yang bisa menjawab pertanyaan bertema
waktu (mis. "penjualan minggu ini", "tren bulan lalu"). System prompt agen sudah
diinstruksikan untuk menolak menjawab pertanyaan seperti ini alih-alih mengarang.

## Dua Sumber Data, Dua Peran Berbeda

| Sumber | Isi | Peran |
|---|---|---|
| `katalog_produk.csv` + koleksi ChromaDB `products` | Produk hasil crawl (nama, kategori, harga, deskripsi, **terjual**) | Pencarian semantik produk + metadata terstruktur. Kolom `terjual` diisi dari agregasi `transaction_data.csv`. |
| `transaction_data.csv` | Log transaksi mentah (~1.5 juta baris): `product_name, product_category_name_lvl_0, product_price, product_short_desc` | Dipakai **live** untuk hitung produk terlaris per segmen (`get_top_sellers`). |

**Keterbatasan penting:** `transaction_data.csv` **tidak punya kolom timestamp
atau user_id**. Artinya sistem ini bisa menghitung *seberapa sering* sebuah
produk terjual, tapi **tidak bisa** menjawab pertanyaan bertema waktu (mis.
"apa yang laku minggu ini") atau riwayat per-pengguna (`get_user_history()`
dari TODO awal tidak bisa dibangun dari data ini tanpa kolom tambahan).

## Struktur Folder (aktual)

```
D:\agentic\
├── agentic_dev\rag-env\           # virtual environment + semua script
│   ├── crawl_alfagift.py          # crawl seluruh katalog alfagift.id -> katalog_produk.csv
│   ├── aggregate_sales.py         # agregasi transaction_data.csv -> kolom "terjual" di katalog
│   ├── build_index.py             # index katalog_produk.csv (+terjual) ke ChromaDB
│   ├── query.py                   # agen tool-calling: segmen -> rekomendasi
│   ├── test_agent_cases.py        # 104 kasus uji regresi thd root_agent (router + 2 spesialis sejak migrasi Graph, lihat infrastructure_agentic.md bagian Graph), lihat PANDUAN_PENGGUNAAN.md
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
    └── transaction_data.csv       # log transaksi mentah (~1.5 juta baris, ~222 MB)
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
melakukan full recompute dari `transaction_data.csv`, jadi aman dijalankan
ulang kapan saja setelah katalog final.

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
- [x] Data transaksi mentah (`transaction_data.csv`, disediakan pengguna)

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
- **Traceback `LiteLLM:ERROR: logging_worker.py ... TimeoutError` muncul di terminal saat agen jalan** -> tidak fatal (dicatat lalu ditelan di dalam LiteLLM sendiri, tidak menghentikan `query.py`), tapi menandakan event loop asyncio sedang macet. Root cause: `google-adk` memanggil tool sinkron langsung di event loop kalau tidak dibungkus `async` (lihat `google.adk.tools.function_tool._invoke_callable`), dan tool di sini melakukan panggilan HTTP ke Ollama + scan pandas atas `transaction_data.csv` (~1.5 juta baris) -- selama itu event loop beku, termasuk task logging internal LiteLLM yang punya batas waktu 20 detik (`LOGGING_WORKER_MAX_TIME_PER_COROUTINE`). Semua tool di `query.py` sudah dibungkus `async def` yang menjalankan implementasinya lewat `asyncio.to_thread` untuk menghindari ini. Kalau muncul lagi setelah menambah tool baru, kemungkinan tool itu belum dibungkus dengan pola yang sama.
- **Agen menjawab "tool tidak mendukung" untuk pertanyaan yang masuk akal** (mis. dulu tidak ada cara mencari kategori dengan penjualan terendah) -> ini bukan bug, tapi tanda ada kesenjangan kemampuan tool. Solusinya tambah tool baru (lihat [Tools yang Tersedia](#tools-yang-tersedia)) -- tapi cek dulu apakah ini benar-benar kesenjangan kemampuan, bukan sekadar argumen yang belum divalidasi di tool yang sudah ada (dua hal itu butuh perbaikan berbeda).
- **(Ditemukan & diperbaiki lewat `test_agent_cases.py`, 104 kasus)** `get_price_range` menolak segment kosong ("Segmen kosong, tidak bisa menghitung rentang harga") padahal pertanyaan "rentang harga seluruh katalog" itu valid -> tidak konsisten dengan `get_worst_sellers` yang sudah memperlakukan segment kosong sebagai "seluruh katalog". Diperbaiki: segment kosong sekarang menghitung rentang harga seluruh `katalog_df` (8.461 produk), bukan menolak.
- **(Ditemukan & diperbaiki)** Pertanyaan tren tanpa kata waktu eksplisit (mis. "kategori apa yang lagi tren sekarang") tidak tertangkap aturan penolakan bertema waktu di system prompt -- karena instruksinya cuma memberi contoh frasa eksplisit ("minggu ini", "bulan lalu"), model menjawabnya seolah itu pertanyaan "kategori paling laris total" biasa (mengarang kesan tren dari angka akumulasi). Diperbaiki: system prompt sekarang eksplisit menyebut kata seperti "tren", "lagi hits/viral", "terkini", "belakangan ini" sebagai sinyal tema waktu yang tetap harus ditolak, sambil boleh menawarkan angka akumulasi total sebagai alternatif asal tidak disamarkan sebagai tren.
- **(Ditemukan & diperbaiki)** `find_cross_sell_candidates` kadang tidak terpanggil walau pertanyaan eksplisit minta "produk serupa/mirip" -- terutama kalau ada embel-embel lain ("...yang penjualannya rendah", "...yang lebih laku untuk kategori itu") atau kalau nama produk konkretnya baru didapat dari tool lain di giliran yang sama (mis. turunan dari kategori). Model cenderung berhenti di tool pertama dan menyuruh pengguna mencari sendiri, alih-alih mengklaim hubungan cross-sell tanpa membuktikannya lewat tool. Diperbaiki: aturan #6 di system prompt sekarang eksplisit mengizinkan/mendorong chaining dua tool dalam satu giliran (cari nama produk konkret dulu, baru panggil `find_cross_sell_candidates` dengan nama itu) dan melarang mengklaim cross-sell tanpa memanggil tool-nya.
- **(Ditemukan, belum diperbaiki -- masalah data, bukan bug kode)** Kolom `product_category_name_lvl_0` di `transaction_data.csv` kadang berisi label yang tidak nyambung dengan produknya (mis. produk sampo Zinc tercatat berkategori "Fashion (Old)"). `get_top_sellers` menampilkan kolom ini apa adanya, jadi noise data mentah ini ikut terlihat pengguna. Bukan bug -- `get_top_sellers` menemukan produknya lewat pencocokan `segment` di `product_name` ATAU `product_category_name_lvl_0`, jadi hasilnya tetap benar meski label kategori yang ditampilkan salah. Kalau mau dibersihkan: ganti kolom kategori yang ditampilkan `get_top_sellers` dengan join ke `katalog_df["kategori"]` (lebih bersih) alih-alih memakai `product_category_name_lvl_0` mentah dari `transaction_data.csv`.
- **(Ditemukan & diperbaiki, ditemukan lewat testing manual di luar 104-kasus)** `search_catalog` dengan argumen `category` bisa mengembalikan "Tidak ada produk yang cocok" padahal produknya jelas ada -- akar masalahnya sama dengan poin di atas: `katalog_produk.csv["kategori"]` (mis. "Sabun Mandi") dan `transaction_data.csv["product_category_name_lvl_0"]` (mis. "Personal Care") pakai vokabuler berbeda untuk kategori yang sama. Model kadang mengisi `category` dengan istilah dari vokabuler `get_top_sellers` (mis. "Personal Care") padahal `search_catalog` cuma mengenal vokabuler `katalog_df["kategori"]` -- filternya lalu tidak cocok dengan SATU PUN dari 15 kandidat semantik teratas, hasilnya kosong walau pencarian semantiknya sendiri dapat kandidat relevan. Diperbaiki: kalau filter kategori bikin nol hasil padahal hasil semantik (tanpa filter kategori) tidak kosong, `search_catalog` sekarang menurunkan filter itu jadi catatan ("Kategori 'X' tidak ketemu persis...") dan tetap menampilkan hasil pencarian semantiknya, bukan mengembalikan nol hasil. Diverifikasi: `_search_catalog_impl("sabun mandi", "Personal Care", 0)` sekarang mengembalikan produk sabun mandi asli (dulu: "Tidak ada produk yang cocok"), sementara `category="Sabun Mandi"` (vokabuler yang benar) tetap memfilter seperti biasa.
- **Baris `verify_and_revise | MASIH MISMATCH...` muncul di `tool_calls.log`** -> bukan crash, ini observability yang disengaja dari loop verifikasi akurasi (`verify_and_revise()` di `query.py`, lihat `infrastructure_agentic.md` bagian Graph #Bagian 2). Artinya: draft jawaban punya angka yang tidak cocok data tool, langkah revisi otomatis mencoba memperbaiki, TAPI hasil revisinya sendiri masih tidak cocok -- diamati langsung ini BISA terjadi (model revisi berbasis LLM lokal, probabilistik, bukan jaminan keras) meski jarang. Tidak ada retry lanjutan (disengaja, supaya biaya tetap murah) -- kalau baris ini sering muncul, jawaban yang dikirim ke pengguna kemungkinan masih salah dan perlu dicek manual dari `draft_nums`/`revised_nums`/`tool_nums` yang dicatat di baris yang sama.

## TODO — Lanjutan

- [x] Verifikasi `qwen3-agent:latest` mendukung tool-calling (`capabilities: ["completion","tools","thinking"]` dari `/api/tags`)
- [x] Tambah agentic tool-calling layer -- sekarang 7 tool, dipecah jadi 2 spesialis (`kategori_specialist`, `produk_specialist`) di balik `root_agent` router (migrasi Graph Bagian 1, lihat `infrastructure_agentic.md` bagian Graph), lihat [Tools yang Tersedia](#tools-yang-tersedia) untuk daftar lengkap tool
- [x] Migrasi ke `google-adk` untuk session state (percakapan multi-turn dalam satu run)
- [x] Bungkus semua tool jadi `async` + `asyncio.to_thread` supaya tidak memblok event loop (lihat Known Issues soal `LoggingWorker` timeout)
- [x] Sesi persisten lintas-restart lewat `SqliteSessionService` (`agent_sessions.db`), ganti `InMemorySessionService`
- [x] Observability minimal: `before_tool_callback`/`after_tool_callback` mencatat tiap panggilan tool ke `tool_calls.log`
- [x] Uji regresi otomatis: `test_agent_cases.py` (104 kasus/107 giliran), lihat `PANDUAN_PENGGUNAAN.md`
- [x] Instruksi dinamis: `_build_instruction()` (`InstructionProvider`) menyisipkan jumlah produk/index/tanggal update ke system prompt tiap giliran
- [x] Retry sekali untuk panggilan embedding Ollama yang transient-fail (`_embed()` di `query.py`)
- [x] Loop verifikasi akurasi jawaban akhir vs. data tool -- `verify_and_revise()` di `query.py` (opsi hybrid, dipilih lewat benchmark nyata di `benchmark_verify_loop.py`, lihat `infrastructure_agentic.md` bagian Graph #Bagian 2 untuk angka lengkap)
- [ ] `get_user_history()` -- **tidak bisa dibangun** dari `transaction_data.csv` saat ini (tidak ada user_id/timestamp). Perlu sumber data baru kalau personalisasi per-user tetap diinginkan.
- [x] Memori lintas-run -- sudah lewat `SqliteSessionService` persistent (raw histori percakapan bertahan lintas restart)
- [x] Kanari ukuran sesi: `_warn_if_session_growing()` di `query.py` memperingatkan sekali per ambang batas (60/120/240/480/960 event) kalau sesi tunggal-abadi (`SESSION_ID` tetap) mulai mendekati batas `num_ctx=8192` -- **bukan solusi penuh**, cuma sinyal dini; lihat `infrastructure_agentic.md` bagian Context untuk kenapa windowing/summarization sungguhan sengaja ditunda
- [ ] Memori semantik lintas-sesi (`google.adk.memory.BaseMemoryService`) -- **ditunda, keputusan sadar**: `query.py` cuma punya satu `SESSION_ID` abadi, sementara API ini dirancang untuk mencari lintas banyak sesi; dipilih menangani risiko context-window (poin kanari di atas) dulu karena lebih konkret, lihat `infrastructure_agentic.md` bagian Context
- [ ] Percepat `build_index.py` -- saat ini satu request embedding per baris secara berurutan (~4 baris/detik); bisa dipercepat dengan batching atau request paralel ke Ollama kalau GPU/CPU masih ada headroom
- [ ] Tambah error handling: koneksi Ollama gagal, `transaction_data.csv` tidak ditemukan
- [x] Uji ketahanan agen terhadap segmen ambigu / di luar katalog -- lewat `test_agent_cases.py` (104 kasus/107 giliran, termasuk kategori EDX/BD khusus untuk ini); 0 crash, 3 bug ditemukan+diperbaiki (lihat Known Issues)
- [x] Evaluasi kualitas retrieval & kualitas rekomendasi cross-sell secara sistematis -- `test_agent_cases.py` (bukan cuma spot-check manual lagi), tapi masih penilaian manual per-kasus (baca jawaban satu-satu), belum ada scoring otomatis -- lihat poin baru di bawah kalau mau dikembangkan lebih jauh
- [ ] Perbaiki kualitas kolom kategori yang ditampilkan `get_top_sellers` -- saat ini pakai `product_category_name_lvl_0` mentah dari `transaction_data.csv` yang kadang tidak nyambung dengan produknya (lihat Known Issues); pertimbangkan join ke `katalog_df["kategori"]` yang lebih bersih
- [ ] Scoring otomatis untuk `test_agent_cases.py` -- saat ini "benar/salah" masih dinilai manual dengan membaca `test_report.jsonl`; bisa dikembangkan jadi assertion otomatis (mis. cek angka di jawaban cocok dengan hasil tool, bukan cuma cek nama tool yang terpanggil)
- [ ] Setelah crawl penuh selesai: jalankan ulang `aggregate_sales.py` lalu `build_index.py` untuk index produksi final (index saat ini kemungkinan masih index test skala kecil)
