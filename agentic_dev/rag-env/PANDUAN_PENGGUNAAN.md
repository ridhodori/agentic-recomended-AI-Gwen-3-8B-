# Panduan Penggunaan — Agen Rekomendasi & Analisis Penjualan Alfagift

Dokumen ini tentang **cara memakai dan menguji** sistem yang sudah dibangun.
Untuk detail setup awal, arsitektur, dan troubleshooting teknis, lihat
[`rag-setup-windows.md`](rag-setup-windows.md).

## Ringkasan

Sistem ini punya 4 script yang dijalankan **berurutan**, satu kali (atau
sesekali saat data berubah), lalu agen (`query.py`) dipakai berulang-ulang:

| Urutan | Script | Fungsi | Perlu diulang? |
|---|---|---|---|
| 1 | `crawl_alfagift.py` | Ambil katalog produk dari alfagift.id -> `katalog_produk.csv` | Sesekali, kalau katalog mau di-refresh |
| 2 | `aggregate_sales.py` | Hitung popularitas produk (net qty) dari `transaction_data/transaction_day*.csv` -> kolom `terjual` | Setiap kali `katalog_produk.csv` berubah, atau ada file `transaction_dayN.csv` baru/berubah |
| 3 | `build_index.py` | Embed katalog ke ChromaDB supaya bisa dicari | Setiap kali `katalog_produk.csv` berubah |
| 4 | `query.py` | **Pakai agennya** -- ini yang dijalankan berulang-ulang | Setiap kali mau tanya |

## Menjalankan Agen

```powershell
cd D:\agentic\agentic_dev\rag-env
.\Scripts\python.exe query.py
```

Setelah muncul prompt `Anda:`, ketik segmen produk (kategori atau kata kunci),
misalnya:

```
Anda: sabun mandi
```

Agen akan menjawab dengan: produk terlaris di segmen itu, dan produk mirip
yang penjualannya masih rendah (kandidat untuk dipromosikan bersama produk
terlaris). Ini **satu sesi percakapan** -- boleh lanjut tanya tanpa mengulang
konteks, misalnya:

```
Anda: dari situ, mana yang paling murah?
Anda: kalau untuk kategori minuman gimana?
```

Ketik `keluar` untuk berhenti. Konteks percakapan **tetap tersimpan** ke
`D:/agentic/chroma_db/agent_sessions.db` bahkan setelah program ditutup --
jalankan `query.py` lagi dan kamu bisa langsung lanjut tanya tanpa mengulang
konteks, mis. "dari yang tadi disebut, mana yang paling murah?" tetap
terjawab benar meski itu giliran pertama di proses yang baru. Kalau mau
mulai sesi baru yang benar-benar kosong, hapus `agent_sessions.db`.

### Contoh pertanyaan yang bisa dijawab

Selain "segmen produk" polos, agen ini sekarang bisa menjawab pertanyaan gaya
product owner / sales strategist berikut (lihat [Tools yang Tersedia di
rag-setup-windows.md](rag-setup-windows.md#tools-yang-tersedia) untuk daftar
tool lengkapnya):

- "Kategori apa yang paling laris?" / "kategori mana yang penjualannya paling sedikit?"
- "Produk terlaris di kategori sabun mandi?" (segmen boleh dikosongkan untuk "top N produk terlaris" secara umum, lintas seluruh katalog)
- "Top 2 produk terlaris per hari untuk tanggal 2026-08-01 dan 2026-08-03?" (breakdown TERPISAH per tanggal, bukan satu ranking gabungan untuk seluruh rentang)
- "Produk apa yang paling tidak laku / belum pernah terjual?"
- "Kategori mana yang variasi produknya paling sedikit?" (assortment gap)
- "Berapa rentang harga produk di kategori minuman?" (atau seluruh katalog kalau segmen dikosongkan)
- "Produk apa yang cocok dipromosikan bareng [produk X]?"
- "Penjualan tanggal 2 Agustus 2026 kategori apa yang paling laris?" (filter tanggal/rentang tanggal spesifik, kalau tanggalnya ada di data yang ter-load)
- "Produk sabun mandi apa yang lagi naik daun/trending?" (bandingkan tanggal terbaru vs tanggal sebelumnya di data)
- "Jam berapa penjualan minuman paling ramai?" (peak hour, untuk staffing/timing promo)

**Yang TIDAK bisa dijawab:** pertanyaan per-pelanggan (mis. "pelanggan mana
yang paling sering beli X") -- data tidak punya kolom `user_id` sama sekali.
Pertanyaan bertema waktu **BISA** dijawab sekarang (`transaction_time` sudah
ada di data transaksi, lihat `2026-09-06-transaction-data-v2-design.md`),
TAPI cuma untuk tanggal/rentang yang benar-benar ada di
`transaction_data/transaction_day*.csv` yang ter-load -- agen tahu rentang
tanggal yang tersedia secara dinamis (bukan hardcode) dan akan menolak jujur
kalau tanggalnya di luar itu, atau kalau frasa waktunya relatif dan tidak
jelas cakupannya (mis. "bulan lalu" ketika data cuma mencakup beberapa hari).
Agen seharusnya menolak dengan jujur, bukan mengarang jawaban di kedua kasus
ini; kalau ia mengarang, itu bug (laporkan / cek system prompt di `query.py`).

## Cara Menguji Agen

### 1. Cek prasyarat dulu

```powershell
curl http://localhost:11434/api/tags
```

Harus muncul daftar model termasuk `qwen3-agent:latest` dan `nomic-embed-text`.
Kalau error "connection refused", jalankan Ollama dulu (tray app atau
`ollama serve`).

### 2. Cek index ChromaDB sudah terisi

```powershell
.\Scripts\python.exe -c "import chromadb; c = chromadb.PersistentClient(path='D:/agentic/chroma_db'); print(c.get_collection('products').count())"
```

Kalau hasilnya `0` atau error "collection does not exist", jalankan
`build_index.py` dulu (lihat tabel urutan di atas) -- agen tidak akan bisa
menjawab apa-apa tanpa ini.

### 3. Coba segmen yang jelas ada di katalog

Contoh segmen yang bagus untuk uji coba awal (kategori umum, kemungkinan
besar sudah ter-index): `sabun mandi`, `minuman`, `mie instan`, `sampo`,
`deterjen`. Perhatikan jawabannya menyebutkan produk **nyata** dengan harga
dan jumlah `terjual` yang masuk akal (bukan produk asal-asalan atau harga 0
semua) -- itu tandanya `search_catalog`/`get_top_sellers` benar-benar
mengambil data, bukan mengarang.

### 4. Uji ingatan multi-turn (fitur utama migrasi ke ADK)

Dalam satu sesi yang sama, tanya lanjutan yang **merujuk balik** ke jawaban
sebelumnya, tanpa menyebut ulang nama produknya:

```
Anda: sabun mandi
Anda: yang tadi disebut, mana yang paling murah?
```

Kalau agen bisa menjawab tanpa kamu mengulang nama produk, berarti session
state jalan dengan benar. Kalau agen malah bingung / menjawab seolah tidak
tahu konteks sebelumnya, ada regresi -- cek apakah `SESSION_ID` di `query.py`
konsisten dipakai di setiap panggilan (harus sama sepanjang satu proses
`query.py`, itu yang membuat ADK tahu ini percakapan yang sama).

### 5. Uji kasus tepi (edge case)

- Segmen yang jelas tidak ada di katalog (mis. `pesawat terbang`) -> agen
  harus bilang tidak ketemu, bukan mengarang produk.
- Ketik kosong lalu Enter -> tidak boleh crash (langsung minta input lagi).

### 6. Uji regresi otomatis (117 kasus)

```powershell
.\Scripts\python.exe test_agent_cases.py
```

Menjalankan 117 kasus / 120 giliran terhadap `root_agent` yang sama dengan
`query.py` (pakai `InMemorySessionService` supaya tidak tercampur riwayat
sesi asli), mencakup ketujuh tool, kasus edge (penolakan tema waktu/pelanggan,
segmen di luar katalog, input tidak bermakna), multi-turn, dan pertanyaan
gabungan yang butuh beberapa tool sekaligus. Hasil ditulis progresif ke
`test_report.jsonl` (satu JSON per kasus) -- jalan sekitar 25-30 menit untuk
117 kasus. Cek tool yang benar-benar terpanggil vs. yang diharapkan, dan baca
`answer` tiap giliran untuk menilai apakah jawabannya masuk akal (nama tool
yang beda dari ekspektasi belum tentu salah -- baca dulu jawabannya sebelum
menyimpulkan bug, lihat catatan di `rag-setup-windows.md` bagian Known Issues
untuk contoh nyata dari run terakhir).

### 7. Kalau mau uji cepat tanpa nunggu index penuh

Index penuh butuh puluhan menit (lihat bagian "Kenapa `build_index.py`
lambat" di `rag-setup-windows.md`). Untuk uji cepat fungsi agen saja (bukan
kelengkapan datanya), index sebagian kecil katalog dulu -- filter
`katalog_produk.csv` ke satu-dua kategori sebelum jalankan `build_index.py`,
atau tunggu progress print (`[i/total] ... estimasi sisa X menit`) sampai
cukup satu kategori selesai, lalu langsung coba `query.py` dengan segmen dari
kategori itu.

## Tanda Sesuatu Salah

| Gejala | Kemungkinan penyebab |
|---|---|
| Agen bilang "tidak ada produk yang cocok" untuk segmen umum | Index belum lengkap / belum di-build sama sekali |
| Jawaban berisi teks aneh seperti "Okay, let me process this..." | Bug lama (sudah diperbaiki) di mana reasoning model ikut tercetak -- pastikan `query.py` versi terbaru |
| Error `IndexError` dari ChromaDB | Tool dipanggil dengan argumen kosong -- sudah ada guard di `query.py`, kalau masih muncul berarti ada tool baru tanpa guard serupa |
| `search_catalog`/cari produk mirip kadang terasa lebih lambat sesekali (bukan konsisten) | Normal -- `query.py` sekarang retry sekali otomatis kalau panggilan embedding ke Ollama gagal/lambat sesaat, sebelum benar-benar error ke pengguna |
| Sesekali (bukan tiap giliran) jawaban terasa 2-3x lebih lama dari biasanya | Normal -- ada loop verifikasi akurasi (`verify_and_revise()`) yang mengecek angka di jawaban terhadap data tool; kalau ada yang tidak cocok atau jawabannya klaim kualitatif tanpa angka, sistem memanggil model sekali lagi untuk mengoreksi sebelum dikirim. Diukur cuma ~8% giliran yang butuh ini, jadi mayoritas giliran tidak terpengaruh -- lihat `infrastructure_agentic.md` bagian Graph #Bagian 2 untuk benchmark lengkap |
| `harga: 0` di beberapa produk | Bukan bug -- beberapa baris di `transaction_data/transaction_day*.csv` memang tercatat harga 0 (data asli, bukan hasil olahan kita) |
| Agen tidak ingat pertanyaan sebelumnya | `SESSION_ID`/`USER_ID` di `query.py` berubah antar panggilan, sesi baru dibuat setiap turn, atau `agent_sessions.db` terhapus/rusak |
| Mau tahu tool apa saja yang dipanggil dan berapa lama | Cek `tool_calls.log` (format: waktu \| nama tool \| argumen \| durasi \| status) -- lebih cepat daripada menebak dari traceback |
| Muncul traceback `LiteLLM:ERROR: logging_worker.py ... TimeoutError` di terminal | Tidak fatal (lihat Known Issues di `rag-setup-windows.md`) -- kalau sering muncul setelah menambah tool baru, tool itu kemungkinan belum dibungkus `async` + `asyncio.to_thread` seperti tool lainnya |
| Agen bilang "tool tidak mendukung ini" untuk pertanyaan yang masuk akal (bukan bertema waktu/per-pelanggan) | Kesenjangan kemampuan tool yang nyata, bukan bug -- lihat [Tools yang Tersedia](rag-setup-windows.md#tools-yang-tersedia), pertimbangkan tambah tool baru |

## Referensi File

- [`crawl_alfagift.py`](crawl_alfagift.py) -- crawler katalog
- [`aggregate_sales.py`](aggregate_sales.py) -- agregasi popularitas
- [`build_index.py`](build_index.py) -- indexer ChromaDB
- [`query.py`](query.py) -- agen (jalankan ini untuk pakai)
- [`test_agent_cases.py`](test_agent_cases.py) -- 117 kasus uji regresi terhadap `root_agent`
- [`rag-setup-windows.md`](rag-setup-windows.md) -- setup, arsitektur, troubleshooting teknis
