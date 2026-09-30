import os
from pathlib import Path
import psycopg2
from sentence_transformers import SentenceTransformer
import json

# Load markdown files from rclone sync
notes_dir = Path("/data/notes")
model = SentenceTransformer("all-MiniLM-L6-v2")  # 384-dim, runs fast on CPU

conn = psycopg2.connect(os.environ["DATABASE_URL"])
cur = conn.cursor()

# Create simple table
cur.execute("""
  CREATE TABLE IF NOT EXISTS documents (
    id SERIAL PRIMARY KEY,
    path TEXT UNIQUE,
    content TEXT,
    embedding vector(384)
  )
""")

# Index all markdown files
for md_file in notes_dir.rglob("*.md"):
    content = md_file.read_text()
    embedding = model.encode(content).tolist()
    
    cur.execute(
        "INSERT INTO documents (path, content, embedding) VALUES (%s, %s, %s) ON CONFLICT (path) DO UPDATE SET embedding = EXCLUDED.embedding",
        (str(md_file.relative_to(notes_dir)), content, embedding)
    )

conn.commit()
conn.close()
print(f"Indexed {len(list(notes_dir.rglob('*.md')))} documents")