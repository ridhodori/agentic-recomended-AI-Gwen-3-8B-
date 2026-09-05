# Desain: Upgrade Data Transaksi (transaction_time + item_qty) & Akurasi Rekomendasi

Status: **DISETUJUI (bagian 1-6) -- siap lanjut ke implementation plan.**
Dibahas bagian per bagian dengan pengguna di sesi ini sebelum implementasi
dimulai, mengikuti pola yang sama seperti `2026-09-05-graph-migration-design.md`.

## 1. Kenapa dokumen ini ada

Sumber data transaksi berubah bentuk:

- **Sebelum:** satu file `transaction_data.csv` tanpa kolom waktu, kolom:
  `product_name, product_category_name_lvl_0, product_price,
  product_short_desc`. Karena tidak ada timestamp, `ROOT_INSTRUCTION`/
  `KATEGORI_INSTRUCTION`/`PRODUK_INSTRUCTION` di `query.py` secara eksplisit
  menolak MENTAH-MENTAH semua pertanyaan bertema waktu (lihat komentar
  modul `query.py` baris 10-12).
- **Sekarang:** file itu sudah tidak ada, digantikan tiga file harian --
  `transaction_data/transaction_day1.csv` (2026-08-01), `transaction_day2.csv`
  (2026-08-02), `transaction_day3.csv` (2026-08-03), masing-masing 1.000.000
  baris -- dengan DUA kolom baru: `transaction_time` dan `item_qty`.
  `query.py` dan `aggregate_sales.py` masih mengacu ke path file lama yang
  sudah tidak ada, jadi **keduanya sudah rusak** sampai desain ini
  diimplementasikan.

Dua masalah akurasi lama yang ditemukan sekaligus lewat perubahan ini:

1. `get_top_sellers`/`get_top_categories`/`aggregate_sales.py` selama ini
   menghitung "terjual" dengan **menghitung baris transaksi** (`count`),
   bukan jumlah unit yang benar-benar terjual -- wajar waktu itu karena tidak
   ada kolom kuantitas sama sekali. Sekarang `item_qty` ada, jadi ranking
   produk/kategori bisa (dan seharusnya) memakai jumlah unit riil.
2. Larangan blanket untuk pertanyaan bertema waktu sudah tidak akurat lagi
   -- data sekarang PUNYA tanggal riil (dan jam), jadi larangan itu perlu
   diganti jadi "jawab kalau tanggalnya ada di rentang data yang tersedia,
   tolak jujur kalau di luar itu."

Detail lain yang harus ditangani:

- Format `transaction_time` **tidak punya pemisah** antara tanggal dan jam:
  `"2026-08-0105:01:54"` (`YYYY-MM-DD` + `HH:MM:SS` langsung digabung, selalu
  18 karakter, diverifikasi di seluruh 3 file -- tidak ada baris dengan
  panjang lain). Bisa langsung di-parse dengan
  `pd.to_datetime(s, format="%Y-%m-%d%H:%M:%S")` tanpa perlu menyisipkan
  separator secara manual, karena tiap field format punya lebar tetap.
- `item_qty` **bisa negatif** (retur/pembatalan) -- rentang teramati sekitar
  -40 s.d. +168 tergantung file. Tidak pernah nol.
- Tidak ada kolom `user_id` atau sejenisnya di data baru -- larangan
  pertanyaan per-pelanggan di instruksi TETAP berlaku, tidak berubah.
- Keputusan bersama pengguna (lihat riwayat percakapan sesi ini):
  - "terjual"/ranking pakai **net**: `sum(item_qty)` per produk/kategori
    (retur MENGURANGI angka, bukan diabaikan atau dihitung sebagai baris).
  - Tiga kemampuan baru sekaligus: **trending (pertumbuhan antar hari)**,
    **penjualan per tanggal/rentang tanggal spesifik**, dan **jam ramai
    (peak hour)**.
  - **Tetap pakai google-adk**, tidak migrasi ke LangChain/LangGraph --
    struktur router + 2 spesialis, `SqliteSessionService`, dan loop
    `verify_and_revise()` sudah dibangun & di-benchmark di atas ADK, migrasi
    penuh berisiko regresi tanpa manfaat akurasi yang jelas untuk masalah
    yang sebenarnya soal data & kejelasan prompt, bukan soal framework
    orkestrasi.
  - Kalau pertanyaan pengguna TIDAK menyebut tanggal sama sekali, tool tetap
    memakai **seluruh data yang tersedia** (union semua file harian) --
    ini bukan kasus khusus, itu memang perilaku default `start_date`/
    `end_date` kosong (lihat bagian 3).

## 2. Lapisan data (`query.py` + `aggregate_sales.py`)

**Pemuatan file:**
- Ganti `TRANSACTION_CSV` (path tunggal) jadi pola glob,
  `transaction_data/transaction_day*.csv` (bukan `*.csv` polos -- sengaja
  spesifik ke pola nama file harian supaya file lain yang mungkin nanti
  ditaruh di folder yang sama, mis. dokumentasi/schema, tidak ikut kebaca
  sebagai data transaksi) -- supaya kalau pengguna menambah
  `transaction_day4.csv` dst di masa depan, tidak perlu ubah kode sama
  sekali.
- `_load_transactions()` tetap satu fungsi cache tunggal (pola sekarang
  dipertahankan), tapi sekarang: baca semua file yang cocok pola glob,
  `pd.concat(...)`, lalu parse `transaction_time` SEKALI jadi kolom
  datetime asli (bukan string) supaya semua tool hilir tinggal filter/
  `.dt.date`/`.dt.hour` tanpa parsing ulang.
- Ukuran gabungan sekarang ~3 juta baris (naik dari ~1,5 juta baris di file
  lama, disebut di komentar modul `query.py`) -- masih dalam batas wajar
  untuk pandas in-memory di mesin ini, tidak perlu chunking/lazy-loading.

**Formula "terjual" (net qty):**
- `get_top_sellers`, `get_top_categories`, dan penghitungan popularitas di
  `aggregate_sales.py` semua ganti dari `count` baris jadi
  `sum("item_qty")` per grup (produk atau kategori).
- Tidak ada floor di 0 per baris individual -- retur (`item_qty` negatif)
  MENGURANGI total grup itu apa adanya (sesuai keputusan pengguna: net,
  bukan gross-only).

**Dampak ke `aggregate_sales.py` & `build_index.py` (harus benar-benar
dijalankan ulang, bukan cuma diedit):**
- `aggregate_sales.py` menghasilkan ulang kolom `terjual` di
  `katalog_produk.csv` dari data transaksi baru (net qty, bukan count) --
  ini file yang dipakai `query.py` saat startup (`katalog_df`).
- `build_index.py` menyisipkan angka `terjual` ke TEKS yang di-embed ke
  ChromaDB (lihat `build_index.py` baris 19) -- supaya index semantik tidak
  membawa angka `terjual` basi (dari hitungan lama/salah), index harus
  di-build ulang setelah `katalog_produk.csv` diperbarui. Ini operasi lambat
  (puluhan menit, satu panggilan embedding per baris) -- akan dijadwalkan
  sebagai langkah eksplisit di plan implementasi, bukan sesuatu yang
  "otomatis" terjadi.

## 3. Tool baru & pembagian ke spesialis (tetap 2 spesialis, tidak nambah spesialis baru)

Tidak menambah spesialis ketiga -- kemampuan baru ditempelkan ke
`kategori_specialist`/`produk_specialist` yang sudah ada, mengikuti pola
pembagian yang sudah ada (level-kategori vs level-produk).

**a. Filter tanggal opsional pada tool yang sudah ada:**
- `get_top_sellers(segment, top_n, start_date="", end_date="")` --
  `produk_specialist`.
- `get_top_categories(top_n, terendah, start_date="", end_date="")` --
  `kategori_specialist`.
- Default `""` di kedua parameter = tanpa filter tanggal = seluruh data
  gabungan (perilaku default, bukan kasus khusus -- lihat bagian 1).
  Kalau diisi, format `YYYY-MM-DD`, inklusif di kedua ujung.
- Ini yang menjawab kebutuhan "penjualan tanggal/rentang tanggal spesifik"
  tanpa menambah tool baru -- cukup parameter tambahan pada tool yang sudah
  ada, spesialis mana yang menangani tidak berubah.

**b. Tool baru -- trending (pertumbuhan antar hari):**
- `get_trending_products(segment, top_n)` -- `produk_specialist`.
- `get_trending_categories(top_n)` -- `kategori_specialist`.
- Definisi "trending": bandingkan net qty pada **tanggal terbaru yang ada di
  data** vs. **tanggal sebelumnya yang ada di data** (bukan hardcode
  "day3 vs day2" -- otomatis mengikuti tanggal maksimum & tanggal
  sebelum-maksimum yang benar-benar ada di dataset gabungan). Ranking
  DESCENDING berdasarkan selisih net qty (tanggal terbaru MINUS tanggal
  sebelumnya) -- pertumbuhan terbesar duluan, sejajar dengan urutan
  "terlaris dulu" di `get_top_sellers`/`get_top_categories`.
  Ini membuat definisi "trending" otomatis mengikuti data terbaru kalau
  pengguna menambah `transaction_day4.csv` dst nanti, tanpa perlu ubah kode.
- Produk/kategori yang cuma muncul di SALAH SATU dari dua tanggal (baru
  launching, atau berhenti terjual total di tanggal terbaru) diperlakukan
  qty-nya sebagai 0 di tanggal yang tidak ada datanya (bukan di-skip) --
  produk baru yang langsung laris tampil sebagai pertumbuhan besar dari 0
  (sinyal yang justru berguna), produk yang tadinya laris lalu hilang total
  tampil sebagai penurunan besar (juga sinyal berguna, bukan noise untuk
  dibuang).
- Output tool menyebutkan EKSPLISIT dua tanggal yang dibandingkan (mis.
  "dibandingkan 2026-08-02 vs 2026-08-03") supaya jawaban akhir bisa jujur
  soal rentang yang dipakai, bukan cuma "sedang tren" tanpa konteks.

**c. Tool baru -- jam ramai (peak hour):**
- `get_peak_hours(segment="", top_n=5)` -- `produk_specialist` (pola mask
  segmen yang sama seperti `get_top_sellers`: cocok di `product_name` ATAU
  `product_category_name_lvl_0`, kosong = seluruh data; `top_n` default 5,
  konsisten dengan tool lain).
- Group by jam (0-23, diekstrak dari kolom datetime hasil parsing di bagian
  2), sum `item_qty` net per jam, urutkan turun, kembalikan `top_n` jam
  teratas.

**d. Larangan waktu & per-pelanggan di instruksi -- diganti, bukan dihapus:**
- Larangan blanket "data tidak punya kolom waktu" di
  `ROOT_INSTRUCTION`/`KATEGORI_INSTRUCTION`/`PRODUK_INSTRUCTION` diganti
  jadi: rentang tanggal yang TERSEDIA disebutkan eksplisit dan DINAMIS
  (bukan hardcode "1-3 Agustus" di string prompt -- dihitung dari data saat
  startup/tiap giliran, pola yang sama seperti `_build_produk_instruction`
  menyisipkan info katalog terkini). Pertanyaan dengan tanggal/rentang DI
  LUAR rentang tersedia tetap ditolak jujur (bukan mengarang), pertanyaan
  relatif yang tidak jelas cakupannya (mis. "bulan lalu" ketika data cuma
  3 hari) juga ditolak jujur dengan alasan eksplisit.
- Larangan pertanyaan per-pelanggan **tidak berubah sama sekali** -- tetap
  ditolak, data baru juga tidak punya `user_id`.

**e. Tool yang TIDAK berubah (di luar cakupan desain ini):**
`get_worst_sellers`, `get_price_range`, `get_category_assortment`,
`search_catalog`, `find_cross_sell_candidates` semuanya beroperasi di atas
`katalog_df` (kolom `terjual` hasil `aggregate_sales.py`), bukan langsung di
atas data transaksi mentah per-hari -- otomatis ikut memakai net qty via
regenerasi `katalog_produk.csv` (bagian 2), tapi TIDAK butuh parameter
tanggal (katalog tidak punya konsep "per hari") dan signature-nya tidak
berubah.

## 4. Arsitektur bahasa prompt: Inggris untuk LLM, Indonesia untuk manusia

Motivasi (dari diskusi dengan pengguna): `qwen3-agent` adalah model
multibahasa yang instruction-tuned -- instruksi Inggris pada praktiknya
diikuti LEBIH konsisten oleh kebanyakan model terbuka (instruction-tuning-nya
paling berat di Inggris), sementara pemahaman input Indonesia dan produksi
output Indonesia tidak terpengaruh sama sekali oleh bahasa instruksi --
model membaca instruksi Inggris dan pesan pengguna berbahasa Indonesia
BERSAMAAN di context yang sama, instruksi Inggris bukan lapisan terjemahan
yang harus dilewati teks Indonesia. Jadi mengganti instruksi ke Inggris
seharusnya MENAIKKAN akurasi kepatuhan instruksi, bukan menurunkannya.

**Yang diterjemahkan ke Inggris:**
- Docstring tiap tool (`Args`/`Returns`) -- ini yang dipakai ADK membentuk
  JSON schema function declaration yang dilihat model (lihat catatan
  `JSON_SCHEMA_FOR_FUNC_DECL` di riwayat proyek -- warning eksperimental
  google-adk yang tidak berbahaya, cuma menegaskan bahwa schema declaration
  ini memang dibentuk dari docstring/type hint Python).
- `ROOT_INSTRUCTION`, `KATEGORI_INSTRUCTION`, `PRODUK_INSTRUCTION` --
  ditulis ulang penuh dalam Inggris, TIAP instruksi tetap menutup dengan
  aturan eksplisit: *"Always respond in Bahasa Indonesia (Indonesian),
  regardless of the language of these instructions."*
- Prompt internal `verify_and_revise()` (yang meminta model menilai draft
  jawaban terhadap data tool) -- instruksinya Inggris, TAPI tetap menyertakan
  aturan eksplisit bahwa OUTPUT (draft asli atau hasil revisi) harus tetap
  dalam Bahasa Indonesia -- prompt ini istimewa karena outputnya BISA jadi
  jawaban akhir yang benar-benar dikirim ke pengguna (lihat `query.py`,
  jalur `revised` di `_verify_and_revise_impl`).

**Yang TETAP Indonesia:**
- Seluruh komentar kode panjang di `query.py` yang menjelaskan alasan/
  riwayat keputusan teknis (mis. kenapa `asyncio.to_thread`, kenapa hybrid
  verify loop) -- ini dokumentasi untuk manusia (developer proyek ini),
  tidak pernah dikirim ke model, jadi bahasanya independen dari keputusan
  ini.
- Jawaban akhir ke pengguna -- tidak berubah, tetap Bahasa Indonesia.

**Mitigasi risiko drift-bahasa pada ekstraksi entitas (WAJIB, bukan opsional):**
Risiko konkret: router/spesialis mengekstrak nilai (nama segmen/kategori/
produk) dari pertanyaan Indonesia pengguna untuk dikirim sebagai argumen
tool (mis. `segment="sabun mandi"`). Argumen ini dicocokkan via substring
literal (`str.contains`) terhadap data katalog/kategori yang JUGA berbahasa
Indonesia. Duduk di context instruksi Inggris menciptakan risiko nyata model
"ikut-ikutan" menerjemahkan entitas yang diekstrak ke Inggris (mis.
`segment="soap"`) -- substring match terhadap data Indonesia otomatis
gagal total (0 hasil) atau salah, tanpa error yang kelihatan.

Mitigasi -- satu baris aturan eksplisit ditambahkan ke KETIGA instruksi
(`ROOT_INSTRUCTION`/`KATEGORI_INSTRUCTION`/`PRODUK_INSTRUCTION`), sejajar
dengan aturan "kutip angka persis" yang sudah ada dan terbukti efektif
dengan pola yang sama:

> "Extract product/category/segment values EXACTLY as they appear in the
> user's message or catalog data -- never translate them to English, even
> though these instructions are written in English."

Kenapa cukup instruksi teks di sini (beda dari kasus root-delegation-gap
yang butuh jaring pengaman deterministik tambahan, lihat
`infrastructure_agentic.md` bagian 5): kasus root-delegation-gap gagal
karena model melewati SATU LANGKAH UTUH (tidak memanggil tool sama sekali,
tidak ada sinyal untuk dicek belakangan). Di sini modelnya TETAP memanggil
tool yang benar dengan argumen yang salah bahasa -- hasilnya "tidak ada
produk yang cocok" (empty match), yang SUDAH tertangkap sebagai gejala
kasat mata (jawaban aneh untuk segmen yang jelas ada di katalog), berbeda
dari fabrikasi diam-diam. Tetap: kalau ditemukan lewat pengujian nanti
(bagian 5) bahwa translasi entitas ini benar-benar terjadi meski sudah
diberi aturan eksplisit, backstop berikutnya yang masuk akal adalah
memvalidasi bahwa nilai argumen yang dipakai tool cocok dengan salah satu
istilah di pertanyaan asli pengguna/katalog sebelum tool dieksekusi --
ditunda sampai ada bukti nyata perlu (pola yang sama seperti keputusan
"latency regression: documented, not fixed" di riwayat proyek -- jangan
bangun mitigasi lapis kedua untuk masalah yang belum terbukti nyata
terjadi).

## 5. Pembaruan testing

`test_agent_cases.py` (104 kasus/107 giliran) -- kasus BARU ditambahkan,
bukan menulis ulang suite yang ada:

- Kasus yang menguji ranking net-qty menghasilkan angka BERBEDA dari
  sebelumnya (mis. total `terjual` sekarang bisa mencerminkan pengurangan
  retur) -- pastikan `verify_and_revise()` tidak salah menandai angka net
  yang sah sebagai mismatch.
- Kasus tanggal/rentang tanggal spesifik (`start_date`/`end_date` terisi),
  termasuk tanggal DI LUAR rentang tersedia (harus ditolak jujur, bukan
  mengarang).
- Kasus trending (produk & kategori) -- verifikasi jawaban menyebut kedua
  tanggal yang dibandingkan (lihat bagian 3b).
- Kasus jam ramai (peak hour).
- Kasus regresi untuk mitigasi bahasa di bagian 4 -- pertanyaan Indonesia
  eksplisit dengan nama segmen/kategori yang punya terjemahan Inggris jelas
  (mis. "sabun mandi", "minuman", "makanan") DAN sudah diketahui ada di
  katalog -- assert hasil tool BUKAN "tidak ada produk yang cocok"/kosong
  (gejala paling mudah terdeteksi kalau argumen tool diam-diam
  diterjemahkan ke Inggris, lihat bagian 4).
- Kasus pertanyaan bertema waktu yang SEBELUMNYA ditolak blanket -- sekarang
  harus terjawab (dalam rentang tersedia) alih-alih ditolak.
- Jalankan regresi penuh (~25-30 menit) setelah implementasi, pola yang
  sama seperti migrasi Graph.

## 6. Dokumen `.md` yang perlu diperbarui (isi, bukan cuma dokumen desain ini)

- `PANDUAN_PENGGUNAAN.md` -- bagian "Yang TIDAK bisa dijawab" sekarang
  salah soal waktu; tulis ulang dengan rentang tanggal yang benar-benar
  tersedia + contoh pertanyaan baru (trending, tanggal spesifik, jam ramai).
- `rag-setup-windows.md` -- bagian "Tools yang Tersedia" (tambah 3
  kemampuan baru + parameter tanggal opsional) dan teks Known Issues mana
  pun yang masih mengklaim tidak ada timestamp.
- `infrastructure_agentic.md` -- entri baru di "Ringkasan Prioritas",
  gaya penulisan sama seperti entri yang sudah ada (nomor urut lanjutan,
  status implementasi).
- Docstring modul `query.py` (komentar paling atas) -- saat ini menyatakan
  data transaksi tidak punya timestamp; itu sekarang salah, perlu direvisi
  sekalian menjelaskan keputusan net-qty dan pola dua-bahasa (bagian 4).

---

Status dokumen ini: **seluruh bagian (1-6) sudah dibahas dan disetujui
pengguna dalam sesi ini.** Siap untuk self-review lalu lanjut ke
implementation plan (`writing-plans`).
