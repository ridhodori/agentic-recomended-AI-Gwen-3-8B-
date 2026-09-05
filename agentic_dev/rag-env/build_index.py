import time

import chromadb
import ollama
import pandas as pd

client = chromadb.PersistentClient(path="D:/agentic/chroma_db")
collection = client.get_or_create_collection(name="products")

df = pd.read_csv("D:/agentic/chroma_db/katalog_produk.csv")  # kolom: id, nama, kategori, harga, deskripsi, terjual
df["terjual"] = df["terjual"].fillna(0)

# Satu panggilan embedding per baris (tidak di-batch), jadi ini I/O-bound dan
# lambat untuk katalog besar (~0.2-0.3 detik/baris tergantung hardware).
# Print progress di sini supaya jelas proses masih jalan, bukan macet.
total = len(df)
start = time.time()
for i, (_, row) in enumerate(df.iterrows(), start=1):
    text = f"{row['nama']} | kategori: {row['kategori']} | harga: {row['harga']} | terjual: {row['terjual']:.0f}x | {row['deskripsi']}"
    emb = ollama.embeddings(model="nomic-embed-text", prompt=text)["embedding"]
    collection.upsert(ids=[str(row["id"])], embeddings=[emb], documents=[text])

    if i % 50 == 0 or i == total:
        elapsed = time.time() - start
        rate = i / elapsed
        eta_min = (total - i) / rate / 60 if rate > 0 else 0
        print(f"[{i}/{total}] {rate:.1f} baris/detik, estimasi sisa {eta_min:.1f} menit")

print(f"\nSelesai: {total} produk di-index ke ChromaDB.")