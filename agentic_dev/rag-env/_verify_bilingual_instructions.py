"""Verifikasi manual: ketiga instruksi (root/kategori/produk) sekarang
Inggris, menyisipkan rentang tanggal data yang tersedia secara dinamis, dan
memuat aturan wajib (jawab Bahasa Indonesia + jangan terjemahkan
segmen/kategori/produk yang diekstrak). Juga cek tool baru sudah terdaftar
di specialist yang benar. Tidak butuh Ollama.
Jalankan: ./Scripts/python.exe _verify_bilingual_instructions.py
"""

import query


def main():
    for builder in (query._build_root_instruction, query._build_kategori_instruction, query._build_produk_instruction):
        text = builder(None)
        assert "Bahasa Indonesia" in text, f"FAIL: {builder.__name__} tidak menyebut aturan jawab Bahasa Indonesia"
        assert "NEVER translate" in text, f"FAIL: {builder.__name__} tidak menyebut aturan larangan terjemahan entitas"
        assert str(query._load_transactions()["transaction_time"].dt.date.min()) in text, (
            f"FAIL: {builder.__name__} tidak menyisipkan tanggal minimum data yang tersedia"
        )
    print("PASS: ketiga instruksi Inggris, menyebut aturan wajib, dan menyisipkan rentang tanggal dinamis")

    def _tool_name(t):
        # google-adk 2.8.0 menyimpan tools apa adanya (fungsi mentah) di
        # Agent.tools -- wrapping jadi FunctionTool (yang punya .name) baru
        # terjadi lewat canonical_tools() async saat runtime, bukan di sini.
        return getattr(t, "name", None) or getattr(t, "__name__", None)

    kategori_tools = {_tool_name(t) for t in query.kategori_specialist.tools}
    produk_tools = {_tool_name(t) for t in query.produk_specialist.tools}
    assert "get_trending_categories" in kategori_tools, f"FAIL: kategori_specialist.tools = {kategori_tools}"
    assert {"get_trending_products", "get_peak_hours"} <= produk_tools, f"FAIL: produk_specialist.tools = {produk_tools}"
    print("PASS: get_trending_categories terdaftar di kategori_specialist, get_trending_products+get_peak_hours di produk_specialist")

    assert callable(query.root_agent.instruction), "FAIL: root_agent.instruction harusnya InstructionProvider (callable), bukan string statis"
    assert callable(query.kategori_specialist.instruction), "FAIL: kategori_specialist.instruction harusnya InstructionProvider (callable)"
    print("PASS: root_agent dan kategori_specialist pakai InstructionProvider dinamis, bukan string statis")

    print("SEMUA CEK LULUS")


if __name__ == "__main__":
    main()
