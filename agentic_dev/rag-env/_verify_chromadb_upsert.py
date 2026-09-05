"""Verifikasi manual: chromadb.Collection.add() pada ID yang SUDAH ADA diam-
diam TIDAK memperbarui dokumen (bukan error, bukan no-op yang kelihatan) --
upsert() yang benar-benar menimpa. Dikonfirmasi langsung terhadap
chromadb==1.5.9 yang terinstall. Kalau build_index.py masih pakai add(),
menjalankannya ulang setelah katalog_produk.csv berubah TIDAK memperbarui
angka 'terjual' yang ter-embed untuk produk yang sudah pernah di-index --
index semantik jadi basi selamanya. Tidak butuh Ollama (pakai
PersistentClient sementara di folder temp, bukan koleksi produksi).
Jalankan: ./Scripts/python.exe _verify_chromadb_upsert.py
"""

import tempfile

import chromadb


def main():
    tmp = tempfile.mkdtemp()
    client = chromadb.PersistentClient(path=tmp)
    col = client.get_or_create_collection(name="test")

    col.add(ids=["1"], embeddings=[[0.1, 0.2]], documents=["versi lama"])
    got = col.get(ids=["1"])
    assert got["documents"] == ["versi lama"], f"FAIL: seed awal salah: {got}"

    col.add(ids=["1"], embeddings=[[0.9, 0.9]], documents=["versi baru"])
    got = col.get(ids=["1"])
    assert got["documents"] == ["versi lama"], (
        f"FAIL (unexpected): add() pada ID yang sama ternyata MEMPERBARUI dokumen di versi "
        f"chromadb ini ({got}) -- build_index.py TIDAK perlu diubah ke upsert(), cek ulang temuan ini."
    )
    print("PASS: dikonfirmasi -- add() pada ID yang sudah ada diam-diam TIDAK memperbarui dokumen")

    col.upsert(ids=["1"], embeddings=[[0.9, 0.9]], documents=["versi baru"])
    got = col.get(ids=["1"])
    assert got["documents"] == ["versi baru"], f"FAIL: upsert() seharusnya menimpa, dapat: {got}"
    print("PASS: upsert() pada ID yang sama BENAR-BENAR menimpa dokumen")

    print("SEMUA CEK LULUS")


if __name__ == "__main__":
    main()
