# Desain: Migrasi Graph (root router + 2 spesialis)

Status: **draf. Dua dari tiga risiko di bagian 8 sudah diverifikasi/
diselesaikan lewat re-review (latensi masih terbuka, diukur saat testing);
dua temuan baru di bagian 9-10 sudah diputuskan lewat review lanjutan**
(belum diimplementasikan ke `query.py`). Ditulis setelah Bagian 1 & 2 dari
desain Graph dikonfirmasi di `infrastructure_agentic.md` bagian 5 --
dokumen ini adalah spec detail untuk Bagian 1 (Bagian 2, loop verifikasi
akurasi, sudah selesai diimplementasikan terpisah, lihat
`verify_and_revise()` di `query.py`).

**Update (sesi lanjutan):** dua hal baru ditemukan saat review lanjutan
sebelum implementasi -- keduanya mengoreksi/melengkapi bagian 5, tidak
mengubah keputusan mekanisme/pengelompokan di bagian 2-3. Lihat bagian 9
(koreksi: root callback mencemari ground-truth verifikasi) dan bagian 10
(gap baru: spesialis kehilangan riwayat percakapan).

## 1. Kenapa dokumen ini ada

`infrastructure_agentic.md` sudah menyepakati BENTUK migrasi ini
(`AgentTool`, root jadi pure router, pengelompokan 3 spesialis). Menulis
kode langsung dari kesepakatan tingkat-tinggi itu berisiko: dua hal baru
ditemukan saat menyiapkan implementasi yang **mengubah** detail teknisnya,
dan keduanya perlu direview sebelum kode ditulis:

1. Mekanisme `AgentTool` yang disepakati sebelumnya ternyata **secara
   eksplisit didiskon** di source code versi `google-adk` yang terpasang
   (lihat bagian 2).
2. Pengelompokan 3-spesialis yang disepakati sebelumnya akan **memecah**
   satu pola chaining tool yang baru saja diperbaiki & diverifikasi
   sesi ini (lihat bagian 3).

## 2. Revisi mekanisme: `sub_agents` + `mode="single_turn"`, bukan `AgentTool` manual

Diverifikasi ke source (`google/adk/tools/agent_tool.py:109-127`):

```
Note:
  To expose an agent as an inline tool of a parent LlmAgent, prefer
  setting mode='single_turn' on the sub-agent and attaching it via
  sub_agents=[...] instead of wrapping it with AgentTool. The framework
  then exposes the sub-agent as a tool automatically and runs it inline
  in the parent's session.
  ...
  Direct usage of AgentTool is discouraged.
```

Dan di `google/adk/agents/llm_agent.py:1183-1201` (`model_post_init`):
kalau sub-agent punya `mode == "single_turn"`, parent OTOMATIS menambahkan
`_SingleTurnAgentTool(sub_agent)` ke `self.tools` -- ini SUBCLASS dari
`AgentTool` yang sama, dijalankan lewat `tool_context.run_node(...)`
(primitif eksekusi inline ADK, bukan Runner/session terpisah). Jadi
perilaku fungsionalnya SAMA PERSIS dengan yang sudah disepakati (panggil
spesialis, terima hasil, kontrol tetap di root -- bukan
`transfer_to_agent` yang one-way) -- cuma cara konfigurasinya beda, dan
versi terpasang ini secara eksplisit bilang jangan pakai `AgentTool` manual.

**Keputusan:** pakai `sub_agents=[...]` + `mode="single_turn"` di tiap
spesialis. TIDAK ada perubahan pada keputusan "kenapa bukan
`transfer_to_agent`" yang sudah didokumentasikan -- itu tetap berlaku
(mekanisme baru ini bukan `transfer_to_agent`, cuma cara berbeda mencapai
efek `AgentTool` yang sama).

**Detail penting supaya tidak salah konfigurasi:** field `mode` di
`LlmAgent` defaultnya `None`, dan `model_post_init` mengisi default
`"chat"` (mekanisme `transfer_to_agent`, BUKAN yang kita mau) kalau tidak
di-set eksplisit. Tiap spesialis WAJIB diberi `mode="single_turn"` secara
eksplisit saat konstruksi -- lupa set ini akan diam-diam berubah jadi
one-way handoff, regresi persis yang ingin dihindari sejak awal desain
ini.

Tanpa `input_schema` custom di spesialis, tool yang di-expose ke root
otomatis cuma py punya satu parameter: `request: string` (diverifikasi di
`agent_tool.py:172-184`) -- root memanggil spesialis dengan satu string
pertanyaan/instruksi, bukan argumen terstruktur.

## 3. Revisi pengelompokan: `find_cross_sell_candidates` pindah ke `produk_specialist`

Pengelompokan yang disepakati sebelumnya:

| Spesialis (LAMA) | Tool |
|---|---|
| `kategori_specialist` | `get_top_categories`, `get_category_assortment` |
| `produk_specialist` | `get_top_sellers`, `get_worst_sellers`, `search_catalog` |
| `strategi_specialist` | `get_price_range`, `find_cross_sell_candidates` |

Masalah: aturan #6 di `SYSTEM_PROMPT` saat ini (`query.py:385-394`) -- hasil
perbaikan bug nyata sesi ini (kasus MT02, CS10, CP04) -- secara eksplisit
mengharuskan CHAINING dalam satu giliran: panggil `get_top_sellers`
ATAU `get_top_categories` dulu untuk dapat nama produk konkret, LALU
LANGSUNG panggil `find_cross_sell_candidates` dengan nama itu. Ini
diverifikasi bekerja lewat pengujian nyata sebelum dokumen ini ditulis.

Kalau `find_cross_sell_candidates` dipisah ke `strategi_specialist`
sementara `get_top_sellers`/`get_top_categories` ada di spesialis lain,
chaining satu-giliran ini TIDAK BISA lagi terjadi di dalam satu
pemanggilan spesialis -- root harus mengorkestrasi 2 panggilan spesialis
berurutan (panggil `produk_specialist` untuk dapat nama produk, baca
hasilnya, baru panggil `strategi_specialist` dengan nama itu). Ini bukan
tidak mungkin (LLM root BISA melakukan tool-call berurutan lintas-giliran
dalam satu turn), tapi jauh lebih rapuh daripada pola yang sudah terbukti
jalan sekarang (chaining di dalam SATU agent/system-prompt) -- dan
regresi ini tidak akan kelihatan sampai diuji ulang, karena
"kelihatannya benar" secara desain di atas kertas.

**Pengelompokan REVISI (2 spesialis, bukan 3 -- lihat juga bagian 8 poin 3
untuk alasan `strategi_specialist` ikut dilebur, ditemukan saat re-review):**

| Spesialis | Tool | Domain pertanyaan |
|---|---|---|
| `kategori_specialist` | `get_top_categories`, `get_category_assortment` | Pertanyaan level kategori |
| `produk_specialist` | `get_top_sellers`, `get_worst_sellers`, `search_catalog`, `find_cross_sell_candidates`, `get_price_range` | Pertanyaan level produk: pencarian, ranking, cross-sell, DAN harga -- keempatnya sering diminta bersamaan dalam satu pertanyaan (lihat bagian 8 poin 3) |

Ini tetap konsisten dengan catatan "dinamis" yang sudah dikonfirmasi
pemilik proyek (pengelompokan boleh direvisi, tidak dikunci selamanya) --
revisi ini justru CONTOH pemakaian sifat dinamis itu, didorong oleh bukti
nyata (bug yang sudah diperbaiki + data co-occurrence tool nyata), bukan
preferensi kosmetik. Kalau nanti tool pricing/promosi baru ditambah dan
`produk_specialist` mulai terasa penuh, pemecahan lagi jadi keputusan
kecil saat itu terjadi -- bukan sekarang, tanpa bukti kebutuhan nyata.

## 4. Instruksi tiap agent (pemecahan dari `SYSTEM_PROMPT` saat ini)

`SYSTEM_PROMPT` saat ini (`query.py:368-413`) berisi 7 aturan routing +
2 aturan universal (penolakan tema-waktu, kutip-angka-persis). Setelah
migrasi, routing per-tool tidak lagi relevan di root (root cuma pilih
spesialis, bukan tool) -- aturan-aturan itu pindah ke spesialis yang
relevan, aturan universal DIDUPLIKASI ke SEMUA agent (root + 2 spesialis)
karena tiap agent yang menghasilkan teks ke pengguna bisa melanggarnya.

**`root_agent` (instruksi baru, pendek):**
- Peran: router. Pilih 1 atau lebih spesialis berdasarkan jenis
  pertanyaan, panggil dengan `request` berisi pertanyaan pengguna (boleh
  diringkas/diperjelas kalau perlu konteks dari giliran sebelumnya).
- Tabel ringkas 2 spesialis + domain masing-masing (bukan tabel 7-tool
  yang detail lagi -- itu sudah jadi urusan internal tiap spesialis).
- Kalau pertanyaan butuh >1 spesialis (mis. kategori + rekomendasi
  produk), panggil semuanya yang relevan dalam satu giliran, lalu
  gabungkan jadi satu jawaban koheren.
- Aturan universal (waktu, kutip-angka-persis) -- root menolak LANGSUNG
  pertanyaan bertema waktu tanpa memanggil spesialis apa pun (sama seperti
  sekarang, cuma pindah dari "jangan panggil tool" jadi "jangan panggil
  spesialis"). Kutip-angka-persis berlaku juga saat root MENGGABUNGKAN
  jawaban >1 spesialis jadi satu respons -- jangan menyusun ulang angka
  yang sudah benar dari tiap spesialis.

**`kategori_specialist`:** aturan #1 (`get_top_categories` + param
`terendah`) dan #4 (`get_category_assortment`) + kedua aturan universal.

**`produk_specialist`:** aturan #2 (`get_top_sellers`), #3
(`get_worst_sellers`), #5 (`get_price_range`), #6
(`find_cross_sell_candidates` + logika chaining persis seperti sekarang,
TIDAK diubah), #7 (`search_catalog`) + kedua aturan universal.

`_build_instruction`-style dynamic info (jumlah produk katalog, jumlah
ter-index, tanggal update) tetap disisipkan, tapi cuma di spesialis yang
benar-benar memakai data itu (`produk_specialist` untuk jumlah
katalog/index, spesialis lain cukup versi tanpa info ini atau versi
ringkas) -- root tidak perlu ini karena tidak menjawab pakai data
langsung.

## 5. Integrasi dengan hal yang sudah ada (observability + verifikasi akurasi)

Ini bagian paling berisiko diam-diam rusak kalau tidak sengaja diperiksa
-- keduanya dibangun & diverifikasi sesi ini SEBELUM migrasi ini ada.

**Tool-call logging (`tool_calls.log`):** `before_tool_callback`/
`after_tool_callback` (`_log_before_tool`/`_log_after_tool`) di-attach ke
SEMUA 3 agent (root + 2 spesialis), bukan cuma root. Karena
`_SingleTurnAgentTool` adalah `BaseTool` biasa dari sudut pandang ADK,
callback di root AKAN tercatat untuk panggilan ke spesialis juga (`tool.name`
= nama spesialis, `args` = `{"request": "..."}`) -- ini bagus untuk
observability (kelihatan spesialis mana yang dipilih), bukan bug.
Callback di tiap spesialis tetap mencatat panggilan tool DATA aslinya
(`get_top_sellers` dkk) seperti sekarang, tidak berubah.

**Loop verifikasi akurasi (`verify_and_revise`):** ~~`_current_turn_tool_outputs`
diisi oleh `_log_after_tool` yang SAMA, dipakai bersama oleh semua agent...
Ini tidak masalah untuk `_verify_and_revise_impl` -- keduanya cuma jadi
lebih banyak angka valid untuk dicocokkan, tidak mengubah logika cek.~~
**DIKOREKSI, lihat bagian 9** -- klaim "tidak masalah" ini SALAH: root's
callback menambahkan teks jawaban `produk_specialist` sendiri (hasil
sintesis LLM, bukan data mentah) ke bucket yang sama dipakai sebagai
ground-truth verifikasi, yang justru melemahkan (bukan menambah) jaminan
`verify_and_revise`. Root's callback TETAP dipasang untuk `tool_calls.log`
(observability routing tetap dipertahankan), TAPI tidak boleh menulis ke
`_current_turn_tool_outputs` -- perlu fungsi callback terpisah untuk root
vs spesialis. **`ask()` di `query.py` TIDAK PERLU DIUBAH SAMA SEKALI** -- ia
cuma memanggil `runner.run_async()` pada `root_agent` dan menangkap jawaban
akhirnya, terlepas dari berapa lapis spesialis di baliknya.

**Diverifikasi (bukan lagi asumsi):** dibuktikan lewat spike terisolasi
(`_spike/callback_probe.py`, dummy router + 1 dummy specialist bertool
satu, dijalankan nyata lewat `Runner`/`InMemorySessionService`) --
urutan event yang tercatat:

```
BEFORE[ROOT] tool=dummy_specialist args={'request': '...'}
BEFORE[SPECIALIST] tool=dummy_lookup args={'item': '...'}
AFTER[SPECIALIST] tool=dummy_lookup response=...
AFTER[ROOT] tool=dummy_specialist response=...
```

Callback ROOT terpanggil untuk delegasi ke spesialis (`tool.name` =
nama spesialis), DAN callback SPESIALIS terpanggil untuk tool internalnya
sendiri (`dummy_lookup`) -- keduanya bersarang dengan urutan yang benar
(before-root -> before-specialist -> after-specialist -> after-root),
persis pola call-and-return yang diinginkan. `root.tools` juga terbukti
otomatis terisi `['dummy_specialist']` tanpa perlu wrap manual, sesuai
klaim source di bagian 2. Risiko ini SELESAI, tidak perlu lagi jadi
langkah verifikasi terpisah saat implementasi -- cukup smoke test dengan
tool ASLI (bagian 7 langkah 2) sebagai konfirmasi akhir di konteks nyata,
bukan lagi pembuktian kelayakan mekanismenya.

## 6. Kompatibilitas dengan `test_agent_cases.py` (104 kasus)

`root_agent` tetap jadi satu-satunya import yang dipakai
`test_agent_cases.py` (`from query import ... root_agent`) -- harness itu
sendiri TIDAK PERLU diubah untuk BISA jalan. Tapi hasilnya berubah bentuk:
`_new_tool_calls()` sekarang akan menangkap nama spesialis
(`produk_specialist`, dst) bercampur dengan nama tool asli
(`get_top_sellers`, dst) di `actual_tools`, karena keduanya lewat baris
log yang sama.

Konsekuensi: `expected_tools` di 104 kasus yang ada (mis.
`["search_catalog"]`) TIDAK AKAN cocok lagi dengan `actual_tools` yang
sekarang jadi `["produk_specialist", "search_catalog"]` -- SEMUA kasus
akan "gagal" di perbandingan `sorted(expected) != sorted(actual)`
meskipun perilakunya benar. Ini BUKAN regresi nyata, cuma format
`actual_tools` yang berubah bentuk.

**Rencana:** tambah 1 filter kecil di `_new_tool_calls()`
(`test_agent_cases.py`) yang memisahkan "nama spesialis" dari "nama tool
data asli" (daftar 7 nama tool yang sudah diketahui) -- laporan tetap
mencatat KEDUANYA (spesialis mana + tool data mana), tapi perbandingan
`expected_tools` di kode tes yang sudah ada tetap jalan terhadap tool
data asli saja, tidak perlu menulis ulang 104 kasus satu-satu. Ini
pekerjaan implementasi kecil, bukan bagian dari desain arsitektur --
dicatat di sini supaya tidak terlupa saat writing-plans.

## 7. Rencana pengujian & rollout

1. Implementasi root + 2 spesialis (dengan pengelompokan revisi di atas).
2. Smoke test manual dengan tool ASLI (bukan dummy): 1 pertanyaan per
   spesialis, konfirmasi akhir lewat `tool_calls.log` di konteks nyata
   (mekanisme dasarnya sendiri sudah dibuktikan lewat spike, lihat bagian 5).
3. Smoke test chaining: ulangi persis kasus MT02/CS10/CP04/SC12/CP01
   (yang memvalidasi perbaikan chaining & co-occurrence tool sebelumnya)
   untuk memastikan pengelompokan revisi (bagian 3) benar-benar
   mempertahankan perilaku itu, bukan cuma benar di atas kertas.
3b. Smoke test verifikasi TIDAK melemah (bagian 9): suntik satu angka
   yang sengaja salah ke jawaban `produk_specialist` (mis. lewat monkeypatch
   sementara pada salah satu tool) dan pastikan `verify_and_revise` di level
   root TETAP mendeteksi mismatch itu terhadap data mentah -- bukan cuma
   lolos karena angka salah itu ikut tercatat sebagai "ground truth" oleh
   callback root.
3c. Smoke test konteks lintas-giliran (bagian 10): ulangi persis MT01-MT03
   pasca-migrasi, konfirmasi root benar-benar menyusun ulang referensi
   ("dari situ", "kategori itu") jadi nilai konkret di `request` yang
   dikirim ke spesialis -- baca `tool_calls.log`/isi request untuk
   memastikan bukan cuma kebetulan lolos.
4. Update filter kecil di `test_agent_cases.py` (bagian 6).
5. Jalankan ULANG seluruh 104 kasus, bandingkan dengan hasil run
   sebelumnya (sebelum migrasi) -- fokus ke: tidak ada error baru, tidak
   ada regresi tool-routing (kasus yang tadinya benar jadi salah spesialis
   atau salah tool), dan checking latensi (1 hop routing tambahan
   diharapkan menambah sedikit waktu per giliran -- ukur seberapa besar,
   jangan asumsi). Sertakan benchmark khusus kasus compound (CP01-CP05,
   lihat bagian 10) sebelum vs sesudah migrasi -- bukan cuma menunggu hasil
   dari 104-kasus run, karena VRAM sudah ~94% terpakai saat idle-generate
   (lihat `infrastructure_agentic.md` bagian 5 #Bagian 2) dan giliran
   compound sekarang berarti beberapa nested full model run, bukan satu
   agent datar memanggil beberapa tool dalam satu pass.
6. Update `infrastructure_agentic.md` (tandai Graph Bagian 1 selesai
   diimplementasikan, bukan cuma dikonfirmasi) dan `rag-setup-windows.md`
   (arsitektur agent berubah dari 1 agent datar jadi router+2 spesialis).
7. Hapus `_spike/callback_probe.py` (throwaway, sudah tidak dibutuhkan
   setelah mekanismenya dipakai nyata di `query.py`).

## 8. Risiko & pertanyaan terbuka

- **Latensi:** 1 hop routing tambahan (root memutuskan spesialis mana,
  BARU spesialis itu jalan) berarti minimal 1 pemanggilan LLM ekstra
  dibanding sekarang (root generate keputusan routing, baru spesialis
  generate jawaban) untuk kasus SATU-spesialis -- sebelumnya cuma 1
  pemanggilan LLM (root langsung pilih tool + jawab). Untuk kasus
  MULTI-spesialis, ini sudah wajar (memang butuh >1 "pemikiran"). Belum
  diukur seberapa besar dampaknya secara nyata -- ini alasan langkah 5 di
  atas WAJIB mengukur latensi, bukan cuma cek benar/salah.
- ~~Callback spesialis via `run_node`~~ -- **selesai, diverifikasi lewat
  spike** (lihat bagian 5): callback root DAN spesialis sama-sama
  terpanggil dengan benar, urutan bersarang sesuai ekspektasi.
- ~~`strategi_specialist` masih 1 tool~~ -- **selesai, dilebur ke
  `produk_specialist`** setelah re-review menemukan bukti nyata di
  `test_report.jsonl`: `get_price_range` co-occur dengan tool produk
  dalam SATU giliran di 3/104 kasus (SC12, CP04, dan CP01 yang butuh 3
  tool sekaligus: `get_price_range` + `find_cross_sell_candidates` +
  `get_top_sellers`) -- pola yang sama persis dengan alasan cross-sell
  dipindah di bagian 3. Lihat pengelompokan final di bagian 3.

## 9. Koreksi bagian 5: root callback tidak boleh mengisi `_current_turn_tool_outputs`

Ditemukan saat re-review lanjutan, sebelum implementasi dimulai: klaim di
bagian 5 ("callback root+spesialis sama-sama mengisi
`_current_turn_tool_outputs`, tidak masalah buat `verify_and_revise`")
SALAH untuk kasus turn satu-spesialis, yang merupakan mayoritas kasus.

**Mekanisme masalahnya:** `verify_and_revise()` (lihat
`infrastructure_agentic.md` bagian 5 #Bagian 2) bekerja dengan mengecek
angka di draft jawaban ROOT terhadap `_current_turn_tool_outputs` sebagai
"ground truth". Ground truth ini SEHARUSNYA cuma berisi data mentah
deterministik (output pandas/ChromaDB -- tidak mungkin salah, karena
dihitung langsung, bukan digenerate LLM). Kalau callback root JUGA menulis
ke bucket yang sama, yang tertulis adalah **teks jawaban `produk_specialist`
sendiri** -- itu HASIL SINTESIS LLM, bisa salah dengan cara yang PERSIS
sama seperti yang coba ditangkap `verify_and_revise` di root.

Untuk turn satu-spesialis (mayoritas kasus di 104-kasus test), root
biasanya cuma me-relay ulang jawaban spesialis apa adanya sebagai draft
akhirnya. Konsekuensinya: draft root dicek terhadap ground-truth yang
SEBAGIAN BESAR berisi salinan sumber draft itu sendiri. Kalau spesialis
salah mengutip satu angka saat menyusun jawabannya dari data mentah, angka
salah itu ikut masuk ke "ground truth", dan `verify_and_revise` di root
akan menganggapnya valid -- persis kelas kesalahan yang jadi alasan
`verify_and_revise` dibangun (lihat bug #1/#2 di
`infrastructure_agentic.md` bagian 5 #Bagian 2), sekarang jadi tidak
terdeteksi lagi untuk turn satu-spesialis.

**Keputusan:** root TETAP dipasangi `before_tool_callback`/
`after_tool_callback` (observability routing di `tool_calls.log`
dipertahankan seperti bagian 5), tapi pakai fungsi callback TERPISAH dari
yang dipakai spesialis:

- Spesialis: `_log_before_tool`/`_log_after_tool` (SAMA seperti sekarang,
  tidak berubah) -- menulis ke `tool_calls.log` DAN append ke
  `_current_turn_tool_outputs`, karena tool_response di level spesialis
  memang data mentah asli.
- Root: fungsi baru (mis. `_log_before_tool_root`/`_log_after_tool_root`)
  -- menulis baris yang SAMA ke `tool_calls.log` (observability tidak
  hilang), TAPI TIDAK append ke `_current_turn_tool_outputs`. `tool.name`
  di level root adalah nama spesialis (`produk_specialist` dst), bukan
  nama tool data -- baris ini tetap berguna untuk melihat spesialis mana
  yang dipilih, cuma tidak ikut jadi bahan verifikasi angka.

Ini tidak mengubah rencana filter di bagian 6 (`test_agent_cases.py` tetap
perlu memisahkan nama spesialis dari nama tool data di `tool_calls.log`)
-- cuma menambah satu keputusan baru: nama spesialis boleh MASUK log,
tapi tidak boleh masuk bucket verifikasi.

## 10. Gap baru: spesialis kehilangan riwayat percakapan lintas-giliran

Ditemukan saat re-review lanjutan, di luar cakupan analisis bagian 2-8:
`AgentTool.run_async` (dipakai `_SingleTurnAgentTool` di baliknya, lihat
`agent_tool.py:264-289`) membuat `InMemorySessionService()` BARU dan
sesi BARU untuk setiap panggilan -- yang diteruskan dari parent cuma
`tool_context.state` (key-value, difilter buang prefix `_adk`), BUKAN
riwayat percakapan (`session.events`). Spesialis TIDAK PERNAH melihat
giliran-giliran sebelumnya secara langsung, beda dari `root_agent`
sekarang yang (lewat `include_contents="default"`) selalu melihat seluruh
riwayat sesi.

**Konsekuensi konkret:** kasus multi-turn (MT01-MT03 di
`test_agent_cases.py`) yang mengandalkan konteks giliran sebelumnya (mis.
MT01: "yang paling murah dari situ berapa harganya?" merujuk ke hasil
`get_top_sellers` giliran sebelumnya) HARUS diselesaikan root, yang satu-
satunya agent yang masih melihat riwayat penuh, SEBELUM memanggil
spesialis -- root wajib menyusun ulang referensi ("dari situ", "kategori
itu") jadi nilai konkret (nama produk/kategori eksplisit) di dalam teks
`request` yang dikirim ke spesialis.

**Opsi yang dipertimbangkan:**
- **(A, DIPILIH) Root menyusun ulang referensi lewat instruksi prompt saja,
  tanpa mekanisme baru.** Murah, konsisten dengan pola yang sudah terbukti
  jalan di aturan #6 `SYSTEM_PROMPT` saat ini (resolve kategori -> nama
  produk konkret sebelum memanggil `find_cross_sell_candidates`, dalam
  SATU agent) -- di sini cuma diperluas lintas batas root->spesialis.
  Risiko: probabilistik: kalau root gagal resolve, spesialis dapat
  request yang tidak berlabuh referensinya dan kemungkinan salah
  jawab/menolak. **Keputusan pemilik proyek:** pakai ini dulu, verifikasi
  lewat smoke test MT01-MT03 pasca-migrasi (bagian 7 langkah 3c), baru
  bangun opsi B kalau smoke test itu menunjukkan regresi nyata -- pola
  "murah dulu, ukur, baru bangun lebih" ini konsisten dengan keputusan
  context-window vs memori semantik lintas-sesi di
  `infrastructure_agentic.md` bagian Context dan keputusan hybrid
  `verify_and_revise` di bagian Graph #Bagian 2.
- **(B, DITUNDA, dicatat untuk referensi nanti) Forward konteks lewat
  `tool_context.state`.** Spesialis (atau tool-nya) menulis entitas yang
  sudah di-resolve (mis. `state['last_product']`, `state['last_category']`)
  ke session state; diverifikasi ke source (`agent_tool.py:329-331`,
  `AgentTool.run_async`) bahwa `event.actions.state_delta` DIKEMBALIKAN ke
  parent (`tool_context.state.update(event.actions.state_delta)`) --
  artinya state ini AKAN persisten lintas giliran lewat sesi
  `SqliteSessionService` root yang sudah ada, bukan cuma dalam satu
  panggilan. Lebih deterministik daripada (A), tapi butuh desain baru
  yang belum ada sama sekali di codebase ini: kunci state apa saja yang
  perlu dilacak, siapa yang menulisnya (tool? spesialis? callback?), dan
  update instruksi tiap spesialis untuk membaca state itu sebagai
  fallback konteks. TIDAK dibangun sekarang -- baru layak dikerjakan kalau
  opsi (A) terbukti tidak cukup andal lewat pengujian nyata (bukan
  diasumsikan perlu di muka).
