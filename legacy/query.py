import os
import psycopg2
from sentence_transformers import SentenceTransformer

query = "how do I fix my RAV4 collision sensor?"
model = SentenceTransformer("all-MiniLM-L6-v2")
query_embedding = model.encode(query).tolist()

conn = psycopg2.connect(os.environ["DATABASE_URL"])
cur = conn.cursor()

# Top 5 most relevant docs
cur.execute("""
  SELECT path, content, 1 - (embedding <=> %s::vector) as similarity
  FROM documents
  ORDER BY embedding <=> %s::vector
  LIMIT 5
""", (query_embedding, query_embedding))

for path, content, score in cur.fetchall():
    print(f"{path} (similarity: {score:.2%})")
    print(content[:500])