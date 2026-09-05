"""Verifikasi manual: root_agent jadi pure router, sub_agents ter-wrap
otomatis jadi tools (spec bagian 2). Tidak butuh Ollama (cuma memeriksa
struktur objek, tidak menjalankan model).
Jalankan: ./Scripts/python.exe _verify_router_wiring.py
"""

import query


def main():
    # Catatan: root_agent.mode sendiri TIDAK relevan diperiksa di sini -- itu
    # cuma dipakai kalau root JADI sub_agent dari agent lain (root tidak
    # punya parent). Yang benar-benar melindungi dari regresi ke
    # transfer_to_agent adalah tiap SPECIALIST wajib mode='single_turn'
    # eksplisit (sudah dicek di _verify_callback_split.py, Task 1) --
    # kalau itu lupa di-set, tool_names di bawah ini akan gagal juga (nama
    # specialist tidak akan muncul di root_agent.tools sama sekali, karena
    # model_post_init cuma wrap sub-agent yang mode-nya 'single_turn' atau
    # 'task', bukan default 'chat').
    tool_names = sorted(t.name for t in query.root_agent.tools)
    assert tool_names == ["kategori_specialist", "produk_specialist"], (
        f"FAIL: root_agent.tools seharusnya berisi kedua nama specialist, dapat: {tool_names}"
    )
    print(f"PASS: root_agent.tools otomatis ter-wrap jadi {tool_names}")

    assert len(query.root_agent.sub_agents) == 2, (
        f"FAIL: root_agent.sub_agents seharusnya berisi 2 specialist, dapat {len(query.root_agent.sub_agents)}"
    )
    print("PASS: root_agent.sub_agents berisi kedua specialist")

    print("SEMUA CEK LULUS")


if __name__ == "__main__":
    main()
