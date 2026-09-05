# Infrastruktur Agentic — Peta Prompt / Context / Harness / Loop / Graph

Dokumen ini memetakan `query.py` (dan proyek ini secara umum) ke kerangka
5-lapis yang kamu pakai untuk berpikir soal sistem agentic: **Prompt ->
Context -> Harness -> Loop -> Graph**. Tujuannya: lihat bagian mana yang
sudah ada, bagian mana yang belum, dan tool/library apa (yang sudah
ter-install atau layak ditambah) yang cocok untuk tiap lapisan.

Semua klaim soal `google-adk` di bawah sudah diverifikasi langsung ke kode
sumber yang ter-install di `Lib/site-packages/google/adk/` pada venv ini
(2.8.0+ `[extensions]`), bukan diasumsikan dari dokumentasi umum.

## Ringkasan Status

> **Update:** item 1-3 di "Ringkasan Prioritas" (aturan kutip-angka-persis,
> tool-call logging, persistensi sesi) sudah diimplementasikan di `query.py`
> dan diverifikasi jalan (restart proses tetap ingat percakapan sebelumnya).
> Ditambah lewat run 104-kasus `test_agent_cases.py`: instruksi dinamis
> (`InstructionProvider`), retry sekali untuk panggilan embedding Ollama,
> kanari ukuran sesi, loop verifikasi akurasi hybrid (`verify_and_revise()`,
> dipilih lewat benchmark nyata di `benchmark_verify_loop.py`), dan 5 bug
> nyata ditemukan+diperbaiki di `query.py` (4 dari 104-kasus regresi, 1
> lagi ditemukan belakangan lewat testing interaktif pengguna: framing
> "buatkan rekomendasi promo" bikin model menjawab tanpa panggil tool sama
> sekali -- lihat `rag-setup-windows.md` Known Issues). Graph Bagian 1 & 2
> sudah dikonfirmasi (lihat bagian 5) --
> Bagian 2 langsung diimplementasikan, Bagian 1 (migrasi struktur ke
> `AgentTool` + spesialis) masih menunggu spec tertulis + implementasi
> terpisah.

| Lapisan | Status | Implementasi saat ini |
|---|---|---|
| **Prompt** | Ada | `_build_instruction()` (InstructionProvider) di `query.py` -- system prompt statis + info katalog dinamis (jumlah produk, jumlah ter-index, tanggal update) tiap giliran |
| **Context** | Sebagian | RAG (ChromaDB) + data terstruktur (pandas) + sesi persisten (`SqliteSessionService` -> `agent_sessions.db`) ada; memori semantik lintas-sesi belum -- **menunggu keputusan scoping** (lihat bagian Context: desain sesi tunggal-abadi saat ini tidak cocok langsung dengan API `BaseMemoryService` yang berbasis multi-sesi) |
| **Harness** | Ada | `google-adk` (`Agent` + `Runner` + `LiteLlm` -> Ollama) + `before_tool_callback`/`after_tool_callback` -> `tool_calls.log` + `test_agent_cases.py` (104 kasus regresi otomatis) |
| **Loop** | Ada | Tool-calling loop (ReAct-style) dari ADK + retry sekali untuk embedding Ollama + `verify_and_revise()` (loop verifikasi akurasi hybrid, dipilih lewat benchmark nyata) |
| **Graph** | **Desain dikonfirmasi, migrasi struktur belum dikode** | Masih satu agent datar dengan 7 tool; Bagian 1 (routing+spesialis) & Bagian 2 (verifikasi akurasi) dikonfirmasi -- Bagian 2 sudah jalan di `query.py` (`verify_and_revise()`), Bagian 1 masih perlu spec+implementasi terpisah, lihat bagian 5 |

Baris paling penting: **Graph** benar-benar belum tersentuh sama sekali.
Konsekuensinya sudah kelihatan langsung selama debugging sesi ini: setiap
kali ada jenis pertanyaan baru yang salah pilih tool ("kategori tertinggi"
dijawab dari produk, bukan lewat tool kategori), perbaikannya selalu "tambah
baris di satu system prompt yang itu-itu juga" -- pola yang makin rapuh
seiring jumlah tool bertambah (sekarang sudah 7). Ini gejala klasik agent
tunggal yang mestinya sudah dipecah lewat Graph. Detail & saran ada di
bagian Graph di bawah.

---

## 1. Prompt

**Definisi:** instruksi statis yang membentuk "peran" dan aturan main
model, terpisah dari input pengguna per-giliran.

**Yang sudah ada:**
- `SYSTEM_PROMPT` di `query.py` (string statis, ~30 baris) -- daftar 7 tool
  dan kapan masing-masing dipakai, plus larangan menjawab pertanyaan
  bertema waktu.
- Input pengguna per-giliran dikirim terpisah lewat
  `types.Content(role="user", parts=[types.Part(text=query)])` di `ask()`.

**Yang belum:**
- Prompt-nya masih satu blok teks statis dengan 7 aturan routing tool --
  ini justru gejala dari kekosongan di lapisan Graph: routing yang
  seharusnya jadi struktur (node/edge) malah dipaksa jadi aturan bahasa
  alami yang harus terus ditambal (lihat bagian Graph untuk rencana
  memindahkan sebagian aturan ini ke struktur).
- Tidak ada mekanisme A/B atau versioning prompt -- setiap perubahan
  langsung mengubah file produksi. (Belum diprioritaskan -- satu pemilik,
  satu deployment, risiko rendah untuk saat ini.)

**Saran tool:**
- ~~`instruction` di `Agent` boleh berupa *callable*
  (`InstructionProvider`)...~~ **Selesai** -- `_build_instruction()` di
  `query.py` sekarang jadi `instruction` (bukan string statis lagi),
  menyisipkan jumlah produk katalog, jumlah yang sudah ter-index ChromaDB,
  dan tanggal katalog terakhir diperbarui ke system prompt tiap giliran,
  tanpa perlu edit manual saat data berubah.
- **Kalau prompt makin kompleks:** pindahkan sebagian aturan routing (poin
  1-7 di `SYSTEM_PROMPT`) ke struktur Graph (lihat bagian Graph) alih-alih
  menambah lebih banyak aturan bahasa alami.

---

## 2. Context

**Definisi:** informasi yang disuntikkan ke model di luar prompt statis --
hasil retrieval, data terstruktur, riwayat percakapan, hasil tool call
sebelumnya.

**Yang sudah ada:**
- **Retrieval semantik (RAG):** koleksi ChromaDB `products`, di-query lewat
  `ollama.embeddings()` + `collection.query()` di `search_catalog` dan
  `find_cross_sell_candidates`.
- **Data terstruktur:** `katalog_df` (pandas, dari `katalog_produk.csv`) dan
  `_transactions_cache` (lazy-load `transaction_data.csv`, ~1.5 juta baris)
  -- dipakai langsung sebagai sumber query pandas di semua tool lainnya.
- **Riwayat percakapan (session state):** `InMemorySessionService` +
  `SESSION_ID`/`USER_ID` tetap -- context antar-giliran dalam satu proses
  `query.py` yang sama terjaga (ini alasan migrasi ke ADK).
- **Hasil tool call:** otomatis masuk kembali ke context model oleh ADK
  sebagai bagian dari loop tool-calling (lihat bagian Loop).

**Yang belum:**
- ~~**Memori lintas-sesi:** `InMemorySessionService` hilang total begitu
  `query.py` di-restart~~ -- **Selesai** (lihat Ringkasan Prioritas #3):
  sudah pindah ke `SqliteSessionService` -> `agent_sessions.db`, histori
  mentah bertahan lintas restart.
- **Memori semantik lintas-sesi** (beda dari poin di atas -- ini soal
  *mencari* percakapan lama secara semantik, mis. "inget rekomendasi yang
  pernah diberikan bulan lalu", bukan sekadar histori yang tetap ada):
  belum ada. **Catatan arsitektur penting sebelum ini dibangun:** `query.py`
  saat ini cuma punya SATU `SESSION_ID` tetap ("cli_session") yang dipakai
  selamanya -- tidak ada konsep "sesi baru per hari/topik". API
  `BaseMemoryService.search_memory()` (lihat Saran tool) dirancang untuk
  mencari *lintas beberapa sesi* milik satu user/app -- dengan cuma satu
  sesi abadi, tidak ada sesi lama lain untuk dicari. Sebelum
  mengimplementasikan ini, perlu diputuskan dulu: (a) apakah desain sesi
  tunggal-abadi mau diubah jadi multi-sesi (mis. sesi baru tiap hari, sesi
  lama di-`add_session_to_memory()`), atau (b) masalah yang sebenarnya
  ingin diselesaikan adalah lain -- mis. sesi tunggal yang terus tumbuh
  lama-lama melebihi `num_ctx=8192`, yang butuh solusi berbeda
  (summarization/windowing histori, bukan pencarian lintas-sesi).
- **Context per-pengguna:** tidak bisa dibangun dari data yang ada
  (`transaction_data.csv` tidak punya `user_id`) -- ini batasan data, bukan
  arsitektur, jadi tidak ada tool yang bisa menutupinya.
- **Context temporal:** tidak ada kolom timestamp -- sama, batasan data.

**Saran tool:**
- **Memori semantik lintas-sesi (kalau desain multi-sesi di atas dipilih):**
  `google.adk.memory.BaseMemoryService` (`google/adk/memory/`, diverifikasi
  ke source: `add_session_to_memory(session)` + `search_memory(app_name,
  user_id, query)`, dan `google.adk.tools.load_memory_tool.load_memory`
  sebagai tool siap pakai yang tinggal didaftarkan ke `Agent` kalau
  `Runner` sudah dikasih `memory_service`). Untuk tetap 100% offline,
  implementasi custom `BaseMemoryService` yang menyimpan ke koleksi
  ChromaDB kedua ("conversation_history") jauh lebih murah daripada
  `VertexAiRagMemoryService`/`VertexAiMemoryBankService` bawaan (itu butuh
  cloud, tidak cocok untuk setup offline ini).
- **Kalau masalah sebenarnya adalah context-window (opsi b di atas):**
  tidak perlu memory service sama sekali -- cukup ringkas/pangkas event
  lama di session sebelum dikirim ke model (mis. simpan N giliran terakhir
  utuh + ringkasan giliran sebelumnya), lebih murah dan tidak menambah
  dependency baru.

**Keputusan (dikonfirmasi):** opsi context-window dipilih (bukan memori
semantik lintas-sesi) -- alasan: risiko yang lebih konkret dan lebih dekat
adalah sesi tunggal-abadi tumbuh melebihi `num_ctx=8192`, bukan kebutuhan
mencari sesi lama yang belum ada use case nyatanya. Diverifikasi ke source
(`google/adk/flows/llm_flows/contents.py`): `include_contents="default"`
(default `Agent`) mengirim SELURUH riwayat sesi ke model tiap giliran,
tidak ada windowing/summarization bawaan.

**Diimplementasikan (langkah pertama, sengaja minimal):** `_warn_if_session_growing()`
di `query.py` -- kanari murah yang mencatat peringatan sekali per ambang
batas (60/120/240/480/960 event) begitu ukuran sesi mendekati risiko,
bukan solusi penuh. Sengaja BUKAN summarization otomatis -- itu butuh
panggilan LLM tambahan tiap kali dipangkas (biaya inferensi lagi di
hardware yang sudah terbatas) dan desain sendiri (kapan memangkas, apa
yang tetap utuh, apakah ringkasan lama masih akurat untuk pertanyaan
lanjutan) -- terlalu besar untuk dibangun sebelum ada bukti nyata
masalahnya muncul. Kalau peringatan ini mulai muncul di pemakaian nyata,
itu sinyal untuk membangun windowing/summarization sungguhan.

---

## 3. Harness

**Definisi:** runtime/framework yang merangkai prompt + context + model +
tool jadi satu alur eksekusi -- bukan agen itu sendiri, tapi "mesin" yang
menjalankannya.

**Yang sudah ada:**
- `google-adk`: `Agent` (definisi agent), `LiteLlm` (adapter ke Ollama lewat
  LiteLLM), `Runner` (eksekusi), `InMemorySessionService` (state). Ini
  sudah harness off-the-shelf yang cukup lengkap -- bukan ditulis manual
  (dulu memang manual lewat loop `ollama.chat()`, sudah dimigrasikan).
- Async-safe: semua tool dibungkus `async def` + `asyncio.to_thread` supaya
  tidak memblok event loop harness (perbaikan dari sesi debugging
  sebelumnya).

**Yang belum:**
- ~~**Observability/tracing:** tidak ada logging terstruktur...~~ --
  **Selesai** (lihat Ringkasan Prioritas #2): `before_tool_callback`/
  `after_tool_callback` mencatat tiap panggilan ke `tool_calls.log`.
- ~~**Test/eval otomatis:** pengujian sejauh ini manual...~~ -- **Selesai**:
  `test_agent_cases.py` (104 kasus/107 giliran) menjalankan `root_agent`
  produksi lewat `InMemorySessionService` terisolasi dan menulis hasil ke
  `test_report.jsonl`. Bukan `adk eval` bawaan (lihat Saran tool di bawah
  untuk kenapa) -- custom karena butuh menilai jawaban akhir secara
  kualitatif (baca teksnya), bukan cuma cocokkan nama tool yang terpanggil.
  Sudah terbukti berguna: run pertama menemukan 3 bug nyata (lihat
  `rag-setup-windows.md` Known Issues) yang lolos dari testing manual.

**Saran tool:**
- ~~Callback/plugin bawaan ADK...~~ **Selesai**, lihat di atas.
- **`google-adk` punya modul evaluation bawaan**
  (`google/adk/evaluation/`, `google/adk/cli/utils/evals.py`) untuk
  mendefinisikan set pertanyaan + tool-call yang diharapkan, lalu
  menjalankannya sebagai regression test (`adk eval`). Belum dipakai --
  `test_agent_cases.py` custom dipilih karena run pertama menunjukkan tool
  yang "salah" dibanding ekspektasi kadang tetap menghasilkan jawaban yang
  benar (strategi tool berbeda tapi valid, lihat contoh SC12 di
  `rag-setup-windows.md`), yang butuh baca teks jawabannya, bukan cuma
  cocokkan nama tool -- kalau `adk eval` punya mode penilaian teks bebas
  yang setara, layak dievaluasi lagi ke depan untuk gantikan/lengkapi
  harness sendiri ini.

---

## 4. Loop

**Definisi:** mekanisme iteratif yang memungkinkan agent memanggil tool,
menerima hasil, lalu memutuskan langkah berikutnya -- sampai kondisi
berhenti tercapai.

**Yang sudah ada:**
- **Tool-calling loop (ReAct-style):** ditangani otomatis oleh
  `Runner.run_async()` di dalam ADK -- model panggil tool, hasil masuk
  balik ke context, model lanjut atau selesai. Tidak ditulis manual;
  ini bagian dari Harness (poin 3) yang otomatis menyediakan Loop.
- **Loop interaktif (human-in-the-loop):** `while True: input()...` di
  `main()` -- loop percakapan CLI, bukan loop otonom.
- **Resiliensi per-giliran:** baru ditambahkan sesi ini -- satu giliran
  yang gagal/timeout/dibatalkan (`try/except` di sekitar `ask()`) tidak
  lagi mematikan seluruh loop.

**Yang belum:** (semua item di bawah sudah selesai per update terbaru)

- ~~**Verifikasi/reflection:** tidak ada langkah "cek ulang" setelah model
  menyusun jawaban akhir...~~ -- **Selesai**: `verify_and_revise()` di
  `query.py`, dipasang di akhir `ask()`. Opsi hybrid dipilih lewat
  benchmark nyata di hardware ini (lihat bagian Graph #Bagian 2 untuk
  angka lengkap) -- cek programatik murah di semua giliran, eskalasi ke
  satu panggilan model kedua cuma kalau ada mismatch atau klaim tanpa
  angka verifiable (~8% giliran, diukur dari 104 kasus nyata).
- ~~**Retry otomatis:** kegagalan satu tool call (mis. Ollama sempat
  lambat) langsung jadi jawaban gagal...~~ -- **Selesai (sebagian)**:
  `_embed()` di `query.py` (dipakai oleh `search_catalog` dan
  `find_cross_sell_candidates`, satu-satunya panggilan I/O ke Ollama yang
  rawan lambat/timeout sesaat) sekarang retry sekali dengan jeda 1 detik
  sebelum menyerah. Tool berbasis pandas murni (`get_top_sellers` dkk)
  tidak butuh ini -- kegagalannya kalau ada berarti bug data, bukan
  transient, jadi retry tidak akan menolong.

**Saran tool:**
- **`google.adk.agents.LoopAgent`** (`google/adk/agents/loop_agent.py`,
  sudah ter-install): workflow agent yang mengulang sub-agent sampai
  kondisi berhenti terpenuhi. Cocok untuk pola "generate jawaban -> cek
  konsistensi dengan hasil tool -> kalau tidak konsisten, ulangi" tanpa
  menulis loop manual sendiri. Masih salah satu opsi yang dipertimbangkan
  untuk verifikasi/reflection di atas -- lihat bagian Graph #Bagian 2.
- ~~Lebih murah dulu sebelum ke LoopAgent: tambah instruksi eksplisit...~~
  **Selesai** -- paragraf "PENTING: kutip angka..." di `SYSTEM_PROMPT`.
  Menutup sebagian besar kasus salah-tafsir, tapi tidak semua (`test_report.jsonl`
  dari 104-kasus run masih menunjukkan beberapa kasus perlu chaining tool
  yang tidak selalu terjadi otomatis, mis. kasus MT02/CP04 di
  `rag-setup-windows.md`) -- inilah yang membuat verifikasi programatik di
  atas masih relevan untuk dikerjakan, bukan cuma "selesai karena prompt
  sudah ditambal".

---

## 5. Graph — **desain sedang berjalan (belum diimplementasikan)**

**Definisi:** struktur eksplisit (node = agent/langkah, edge = alur
kontrol/data) untuk mengoordinasikan lebih dari satu agent atau tahap
pemrosesan -- beda dari Loop (satu agent mengulang dirinya sendiri), Graph
mendistribusikan tugas ke beberapa agent/peran yang berbeda.

**Kondisi saat ini (sebelum migrasi):** satu `root_agent` datar
(`sales_recommender`) dengan 7 tool dan satu system prompt yang mendaftar
semuanya beserta aturan kapan dipakai. Tidak ada `sub_agents`, tidak ada
routing eksplisit, tidak ada tahap terpisah.

**Kenapa dikerjakan sekarang, bukan ditunda:** awalnya rekomendasi di
dokumen ini adalah tunggu sampai jumlah tool lewat 12-15 atau salah-pilih-
tool jadi masalah berulang. Keputusan pemilik proyek: mulai sekarang,
selagi jumlah tool masih kecil (7), supaya migrasi tidak perlu dilakukan
di tengah katalog tool yang sudah besar dan berantakan. Pola bug yang
berulang sepanjang sesi sebelumnya -- "pertanyaan jenis baru muncul ->
model bingung/pilih tool salah -> tambah aturan baru di satu system
prompt" -- tetap jadi alasan utama; migrasi ini menghilangkan pola itu
secara struktural, bukan menambal lagi.

### Desain yang disepakati (bagian 1: struktur routing)

**Mekanisme: `google.adk.tools.agent_tool.AgentTool`, BUKAN
`transfer_to_agent`.** `AgentTool` membungkus sebuah `Agent` supaya bisa
dipanggil seperti tool biasa oleh agent lain -- panggil, terima hasil,
kontrol tetap di pemanggil. `transfer_to_agent` sebaliknya adalah
one-way handoff (sub-agent mengambil alih sisa giliran, tidak
mengembalikan kontrol) -- ini akan merusak kebiasaan agent sekarang yang
rutin menggabungkan hasil beberapa tool jadi satu jawaban (mis. produk
terlaris + kategorinya + kandidat cross-sell dalam satu respons). Karena
itu `AgentTool` yang dipilih, meski `transfer_to_agent` sempat disebut di
draf awal dokumen ini sebelum perbedaan ini diverifikasi ke kode sumber.

**Root agent jadi pure router:** `sales_recommender` tidak lagi punya
tool data langsung sama sekali -- semua 7 tool pindah ke spesialis,
`tools=[]` root cuma berisi `AgentTool` yang membungkus tiap spesialis.
Root hanya bertugas memilih spesialis mana yang relevan dan menyusun hasil
akhirnya. Ini yang membuat prompt root tetap pendek berapa pun jumlah tool
di dalam tiap spesialis bertambah nanti.

**Pengelompokan spesialis -- REVISI, lihat spec detail:** tabel 3-spesialis
di bawah ini adalah proposal AWAL dan sudah DIREVISI jadi 2 spesialis di
`2026-09-05-graph-migration-design.md` bagian 3 (ditemukan: memisahkan
`find_cross_sell_candidates` dari `get_top_sellers`/`get_top_categories`
akan memecah chaining satu-giliran yang baru diperbaiki sesi sebelumnya).
Tabel lama (dipertahankan di sini untuk riwayat keputusan):

| Spesialis (proposal awal, SUDAH DIREVISI) | Tool | Domain pertanyaan |
|---|---|---|
| `kategori_specialist` | `get_top_categories`, `get_category_assortment` | Pertanyaan level kategori |
| `produk_specialist` | `get_top_sellers`, `get_worst_sellers`, `search_catalog` | Pertanyaan level produk |
| `strategi_specialist` | `get_price_range`, `find_cross_sell_candidates` | Pricing & promosi |

Pengelompokan final (2 spesialis) ada di spec bagian 3. Menambah tool
baru nanti = putuskan masuk spesialis mana (atau bikin spesialis baru) --
keputusan kecil dan terbatas, bukan menulis ulang satu paragraf routing
besar seperti sekarang.

**Dikonfirmasi (dengan catatan "dinamis"):** pemilik proyek meminta
pengelompokan ini tetap terbuka menambah lebih dari 3 domain seiring
bertambahnya tool, bukan dikunci selamanya di 3. Ini sudah otomatis
didukung oleh mekanisme `AgentTool` di atas -- `root_agent.tools` cuma
berupa list `AgentTool`, jadi menambah spesialis ke-4/ke-5 nanti = definisikan
`Agent` baru + tambahkan `AgentTool`-nya ke list, TANPA merestrukturisasi
spesialis yang sudah ada. Tabel 3-domain di atas jadi konfigurasi AWAL untuk
7 tool yang ada sekarang, bukan plafon arsitektur.

### Bagian 2 (selesai -- diimplementasikan): loop verifikasi akurasi

Diminta terpisah: proses yang mengecek jawaban akhir cocok dengan hasil
tool sebelum sampai ke pengguna, dan mencoba ulang kalau tidak cocok --
ini sebenarnya perluasan dari gap di bagian **Loop** (lihat di atas),
bukan bagian dari Graph itu sendiri, tapi didesain bersamaan karena
diminta dalam paket yang sama.

**Benchmark nyata dulu, sebelum memilih** (`benchmark_verify_loop.py`,
diukur langsung di hardware ini -- RTX 5060 8GB VRAM, `qwen3-agent:latest`):

| Opsi | Cara ukur | Hasil |
|---|---|---|
| Baseline (tanpa verifikasi) | 4 pertanyaan representatif (simple + compound) lewat `root_agent` asli | rata-rata **31.2s/giliran** |
| Kritik model kedua tiap giliran (LoopAgent-style) | panggilan `ollama.chat()` kedua per pertanyaan yang sama, prompt mengecek draft vs data tool | rata-rata **17.4s tambahan** -> **48.6s/giliran, SELALU** (+56%) |
| Cek programatik (regex angka + set comparison) | 4000x panggilan `timeit`-style | **0.019 ms/panggilan** -- praktis nol |
| VRAM sebelum/sesudah kritik | `nvidia-smi` snapshot | 7691 MiB (96% util) -> 7331 MiB (35% util) -- **tidak ada model swap/thrashing** (kritik pakai model yang sama, bukan model kedua terpisah), tapi VRAM sudah ~94% terpakai saat generate SATU model saja -- headroom tipis |
| Rasio giliran yang butuh eskalasi (hybrid) | dari 104 kasus nyata `test_report.jsonl`: giliran yang jawabannya tidak punya angka verifiable sama sekali | **9/107 (8.4%)** -- lalu diperketat lagi saat implementasi (lihat di bawah): giliran TANPA tool dipanggil sama sekali (mis. penolakan tema waktu) dikeluarkan dari hitungan ini karena tidak ada yang bisa/perlu diverifikasi, jadi rasio nyata di produksi lebih rendah dari 8.4% |

**Proyeksi rata-rata latensi/giliran:** programatik-saja ~31.2s (tapi nol
cakupan untuk klaim kualitatif) vs LoopAgent-tiap-giliran ~48.6s (SELALU)
vs **hybrid ~32.7s** (31.2s + 8%*17.4s). Hybrid menang telak -- overhead
rata-rata cuma +4.8% dibanding +56% punya LoopAgent, dan VRAM yang sudah
mepet (94% terpakai saat idle-generate) membuat beban ganda PERMANEN di
setiap giliran (opsi LoopAgent) berisiko lebih tinggi daripada beban ganda
SESEKALI (opsi hybrid, cuma saat eskalasi).

**Keputusan:** hybrid dipilih dan diimplementasikan di `query.py`:
- `_extract_numbers()` + perbandingan set: cek murah dulu, jalan di SETIAP
  giliran yang tool-nya dipanggil.
- Giliran TANPA tool dipanggil sama sekali (mis. penolakan tema waktu) --
  langsung dilewati, tidak ada yang bisa diverifikasi tanpa data tool.
- Kalau draft punya angka dan semuanya cocok dengan data tool giliran itu
  -> selesai, TANPA panggilan model kedua.
- Kalau draft punya angka yang TIDAK cocok, ATAU draft tidak punya angka
  verifiable sama sekali (klaim kualitatif) -> eskalasi SATU panggilan
  `ollama.chat()` tambahan yang membandingkan draft dengan data tool
  mentah (`_current_turn_tool_outputs`, dikumpulkan langsung dari
  `tool_response` asli di `after_tool_callback` -- BUKAN dari baris log
  `tool_calls.log` yang cuma metadata) dan mengembalikan versi final
  (dikonfirmasi sama, atau direvisi).
- `ask()` di `query.py` sekarang memanggil `verify_and_revise()` di akhir
  tiap giliran -- otomatis berlaku untuk `main()` (CLI produksi) DAN
  `test_agent_cases.py` (di-refactor supaya reuse `ask()` yang sama,
  bukan duplikat loop-nya sendiri, supaya suite regresi menguji jalur yang
  sungguhan dipakai pengguna).

**Batasan yang jujur diakui:** langkah revisi ini sendiri berbasis model
lokal, jadi PROBABILISTIK, bukan jaminan keras -- diamati langsung saat
verifikasi: dari beberapa percobaan dengan angka yang sengaja dipalsukan,
1 percobaan gagal (model cuma mengulang draft yang salah apa adanya),
sisanya berhasil mengoreksi ke angka yang benar. Tidak ditambah retry
otomatis di sini (itu menghilangkan penghematan biaya hybrid) -- sebagai
gantinya, kalau hasil revisi MASIH mismatch dengan data tool, itu dicatat
ke `tool_calls.log` (baris `NOTE ... verify_and_revise | MASIH
MISMATCH...`, prefix "NOTE " supaya `test_agent_cases.py` tidak salah
menganggapnya sebagai tool yang terpanggil) supaya kegagalan ini kelihatan
untuk investigasi, bukan diam-diam terkirim ke pengguna tanpa jejak.
Catatan penting: opsi LoopAgent-tiap-giliran PUN tidak lebih andal dari
ini -- keduanya pakai model lokal yang sama, jadi biaya ekstra LoopAgent
tidak membeli keandalan ekstra, cuma membeli kesempatan mencoba di setiap
giliran alih-alih cuma yang butuh eskalasi.

**Dua bug nyata ditemukan SETELAH implementasi awal, lewat pengecekan
langsung (bukan cuma percaya proyeksi ~8% dari benchmark) -- keduanya
sempat bikin proses uji ulang macet/jauh lebih lambat dari proyeksi:**

1. **Marker list bernomor dianggap "angka data".** Jawaban markdown yang
   umum dipakai model ini ("1. **Produk A** ... 2. **Produk B** ...")
   membuat regex ekstraksi angka menangkap "1", "2", dst. sebagai angka
   yang harus dicocokkan ke data tool -- padahal itu cuma nomor urut list,
   dan data tool sendiri berformat bullet "-" (tidak bernomor), jadi
   "1"/"2" HAMPIR TIDAK PERNAH cocok. Efeknya: eskalasi ke model kedua
   terjadi di HAMPIR SETIAP giliran berformat list (bukan cuma ~8%), dan
   proses uji 12-kasus yang sedang berjalan sempat macet karenanya.
   Diperbaiki: `_LIST_MARKER_RE` membuang marker list ("^\s*\d+[.)]\s+")
   sebelum ekstraksi angka.
2. **Angka yang diulang dari pertanyaan pengguna sendiri dianggap
   "mismatch".** Setelah bug #1 diperbaiki, rasio eskalasi masih ~50%
   (bukan ~8%) -- diselidiki dengan membandingkan draft & data tool MENTAH
   untuk satu kasus nyata ("5 produk terlaris untuk sabun mandi"): draft
   dimulai "Berikut **5** produk terlaris...", mengulang angka "5" dari
   PERMINTAAN pengguna sendiri (bukan dari data tool) -- pola ini sangat
   umum ("top 5", "harga di bawah 10000") karena memang begitu cara wajar
   menjawab. Diperbaiki: `_verify_and_revise_impl` sekarang menerima
   `question` (pertanyaan asli giliran itu) dan menganggap angka dari
   PERTANYAAN, bukan cuma dari data tool, sebagai valid juga
   (`allowed_nums = tool_nums | _extract_numbers(question)`).

Pelajaran dari kedua bug ini: benchmark awal (~8% eskalasi) diukur dari
104 kasus yang SUDAH lolos, bukan dari mengamati mekanisme cek itu sendiri
gagal/berhasil pada teks mentah -- proyeksi biaya dari benchmark tetap
valid sebagai perbandingan ANTAR opsi (hybrid tetap jauh lebih murah dari
LoopAgent-tiap-giliran), tapi angka absolutnya (~8%) sempat meleset jauh
sampai kedua bug di atas diperbaiki. Setelah diperbaiki dan diuji ulang
langsung terhadap kasus yang sebelumnya gagal, kedua kasus repro (list
bernomor, angka dari pertanyaan) sudah tidak eskalasi lagi seperti
seharusnya.

**Validasi akhir (12 kasus/14 giliran representatif, setelah kedua bug di
atas diperbaiki):** 493.6 detik total (~35s/giliran rata-rata), **0
error**. Eskalasi terjadi di 2/14 giliran (~14%) -- turun drastis dari
~100% (bug list-marker) dan ~50% (bug angka-dari-pertanyaan), mendekati
proyeksi awal ~8%. Kedua eskalasi yang tersisa diselidiki: sama-sama pola
bug #1 (marker urut seperti "4."/"5.") tapi dalam bentuk yang TIDAK
tertangkap `_LIST_MARKER_RE` karena tidak persis di awal baris (mis.
tercampur baris lain sebelum nomor urutnya). **Keputusan sadar: tidak
dikejar lagi** -- dua ronde perbaikan sudah menurunkan tingkat eskalasi
mendekati target awal, dan tiap pola markdown baru yang mungkin muncul
dari model generatif ini sulit dienumerasi habis lewat regex; residual
~14% ini bukan kesalahan data (2 kasus itu tetap dicatat & terlihat di
`tool_calls.log`, bukan kegagalan senyap), cuma biaya ekstra kecil yang
lebih murah daripada terus menambal regex untuk kasus yang semakin jarang.

**Di luar cakupan migrasi ini (sengaja ditunda, didiskusikan terpisah):**
agent yang menulis tool baru secara otonom saat tidak ada tool yang cocok.
Ini sempat diusulkan tapi ditahan karena risikonya nyata: kode baru yang
ditulis dan dijalankan tanpa review manusia, oleh model lokal 8B, langsung
terhadap data penjualan asli -- kelas kesalahan yang sepanjang sesi-sesi
sebelumnya justru banyak ditemukan di kode yang *sudah* ditulis dan
direview manusia. Butuh desain guardrail sendiri (sandboxing eksekusi,
alur review sebelum tool baru dipercaya) sebelum layak dibangun.

**Status:** Bagian 1 (struktur routing + pengelompokan spesialis, dengan
catatan tetap dinamis/terbuka menambah spesialis baru) **dikonfirmasi,
spec tertulis SELESAI** di `2026-09-05-graph-migration-design.md`
(mekanisme direvisi ke `sub_agents`+`mode="single_turn"`, pengelompokan
direvisi ke 2 spesialis, plus 2 temuan tambahan: koreksi callback root
supaya tidak mencemari ground-truth `verify_and_revise`, dan gap konteks
lintas-giliran karena spesialis kehilangan riwayat percakapan -- lihat
spec bagian 9-10) -- BELUM diimplementasikan ke kode, menunggu proses
implementasi (writing-plans). Bagian 2 (loop verifikasi akurasi)
**dikonfirmasi DAN diimplementasikan** langsung di `query.py` (lihat di
atas) karena ternyata tidak bergantung pada migrasi Graph itu sendiri --
cukup dipasang di `ask()` yang sudah ada.

---

## Ringkasan Prioritas (kalau mau mulai dari yang termurah)

1. ~~**Loop** -- tambah aturan "kutip angka persis dari tool" di
   `SYSTEM_PROMPT`.~~ **Selesai** -- lihat paragraf "PENTING: kutip angka..."
   di `SYSTEM_PROMPT`.
2. ~~**Harness** -- tambah `before_tool_callback`/`after_tool_callback`
   ringan untuk logging durasi tool.~~ **Selesai** -- `_log_before_tool`/
   `_log_after_tool` di `query.py`, output ke `tool_calls.log` (format:
   timestamp | nama tool | args | durasi | status).
3. ~~**Context** -- persist session state ke file lokal supaya percakapan
   tidak hilang total tiap restart.~~ **Selesai** -- `SqliteSessionService`
   (`google.adk.sessions.sqlite_session_service`, pakai `aiosqlite` yang
   sudah ter-install) menulis ke `D:/agentic/chroma_db/agent_sessions.db`.
   `main()` sekarang `get_session()` dulu sebelum `create_session()`, jadi
   restart `query.py` melanjutkan sesi `cli_session` yang sama alih-alih
   membuat sesi kosong baru. Diverifikasi: restart proses lalu bertanya
   "mana yang paling murah?" tanpa menyebut ulang produk -- terjawab benar
   dari histori sesi sebelumnya, tanpa panggilan tool baru (dibuktikan lewat
   `tool_calls.log` yang tidak bertambah baris di giliran itu).
4. ~~**Prompt** -- pindahkan `instruction` dari string statis ke
   `InstructionProvider`.~~ **Selesai** -- `_build_instruction()` di
   `query.py`, menyisipkan jumlah produk katalog/index dan tanggal update.
5. ~~**Loop** -- retry sekali untuk panggilan tool yang rawan
   transient-fail.~~ **Selesai** -- `_embed()` di `query.py` retry sekali
   dengan jeda 1 detik untuk panggilan `ollama.embeddings()`.
6. **Graph** -- **sedang dikerjakan** (dimulai lebih cepat dari rencana:
   diputuskan untuk membangun sekarang selagi jumlah tool masih kecil,
   bukan menunggu sampai jadi masalah). Bagian 1 (struktur routing +
   pengelompokan spesialis) sudah didesain DAN spec tertulis selesai
   (`2026-09-05-graph-migration-design.md`) -- menunggu review akhir
   pemilik proyek atas spec itu, baru lanjut ke implementation plan
   (writing-plans) -- lihat bagian 5 di atas.
7. **Context (memori semantik lintas-sesi)** -- **ditunda, keputusan
   sadar**: kanari ukuran sesi (`_warn_if_session_growing()`) dipasang
   sebagai langkah pertama untuk risiko context-window yang lebih nyata;
   memori semantik lintas-sesi penuh ditunda sampai ada desain multi-sesi
   yang jelas, lihat bagian 2 di atas.
8. ~~**Loop (verifikasi/reflection)**~~ -- **Selesai**: `verify_and_revise()`
   (opsi hybrid, dipilih lewat benchmark nyata di `benchmark_verify_loop.py`),
   lihat bagian 5 #Bagian 2 untuk detail & angka lengkap.
9. **Graph (migrasi struktur `AgentTool` + spesialis)** -- Bagian 1 & 2
   sudah dikonfirmasi, tapi migrasi struktural itu sendiri (memecah
   `root_agent` jadi router + spesialis) **belum ditulis ke kode** --
   masih perlu spec tertulis + proses implementasi terpisah kalau mau
   dilanjutkan.
