# web.py
from flask import Flask, request, jsonify, render_template_string, send_file
import io
from sentence_transformers import SentenceTransformer
import psycopg2
from psycopg2.extras import RealDictCursor
import os
from pathlib import Path
import logging
from datetime import datetime
import json

app = Flask(__name__)
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Global model (loaded once on startup)
model = SentenceTransformer("all-MiniLM-L6-v2")

def get_db():
    """Get database connection"""
    return psycopg2.connect(os.environ["DATABASE_URL"])

def ensure_table():
    """Create table if it doesn't exist"""
    conn = get_db()
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS documents (
            id SERIAL PRIMARY KEY,
            path TEXT UNIQUE,
            content TEXT,
            embedding vector(384),
            created_at TIMESTAMP DEFAULT NOW(),
            updated_at TIMESTAMP DEFAULT NOW(),
            source TEXT DEFAULT 'manual'
        )
    """)
    conn.commit()
    conn.close()

@app.route("/health", methods=["GET"])
def health():
    """Health check"""
    try:
        conn = get_db()
        conn.close()
        return jsonify({"status": "ok"}), 200
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500

@app.route("/query", methods=["POST"])
def query():
    """
    Semantic search over indexed documents
    
    POST /query
    {
        "q": "how do I fix my RAV4 collision sensor?",
        "limit": 5,
        "threshold": 0.3
    }
    
    Returns: [{path, content, similarity}, ...]
    """
    try:
        data = request.get_json()
        query_text = data.get("q", "").strip()
        limit = int(data.get("limit", 5))
        threshold = float(data.get("threshold", 0.0))
        
        if not query_text:
            return jsonify({"error": "Empty query"}), 400
        
        if limit > 50:
            limit = 50
        
        query_embedding = model.encode(query_text).tolist()
        
        conn = get_db()
        cur = conn.cursor(cursor_factory=RealDictCursor)
        
        cur.execute("""
            SELECT 
                id,
                path, 
                content, 
                (1 - (embedding <=> %s::vector)) as similarity,
                source,
                updated_at
            FROM documents
            WHERE (1 - (embedding <=> %s::vector)) > %s
            ORDER BY embedding <=> %s::vector
            LIMIT %s
        """, (query_embedding, query_embedding, threshold, query_embedding, limit))
        
        results = cur.fetchall()
        conn.close()
        
        return jsonify({
            "query": query_text,
            "count": len(results),
            "results": [
                {
                    "id": r["id"],
                    "path": r["path"],
                    "similarity": float(r["similarity"]),
                    "content": r["content"][:500],  # Preview
                    "content_full": r["content"],
                    "source": r["source"],
                    "updated_at": r["updated_at"].isoformat() if r["updated_at"] else None
                }
                for r in results
            ]
        }), 200
    
    except Exception as e:
        logger.error(f"Query error: {e}")
        return jsonify({"error": str(e)}), 500

@app.route("/upload", methods=["POST"])
def upload():
    """
    Upload and index a document
    
    POST /upload
    {
        "content": "...",
        "path": "optional/path.md",
        "source": "manual"  # or "api", "import", etc.
    }
    
    Or multipart/form-data:
    - file: <file upload>
    - source: "upload"
    """
    try:
        if "file" in request.files:
            # File upload
            file = request.files["file"]
            source = request.form.get("source", "file_upload")
            
            if not file.filename:
                return jsonify({"error": "No filename"}), 400
            
            content = file.read().decode("utf-8")
            path = file.filename
        else:
            # JSON payload
            data = request.get_json()
            content = data.get("content", "").strip()
            path = data.get("path", f"doc-{datetime.now().isoformat()}.md")
            source = data.get("source", "api")
        
        if not content:
            return jsonify({"error": "Empty content"}), 400
        
        # Generate embedding
        embedding = model.encode(content).tolist()
        
        # Index into database
        conn = get_db()
        cur = conn.cursor(cursor_factory=RealDictCursor)
        
        cur.execute("""
            INSERT INTO documents (path, content, embedding, source, updated_at)
            VALUES (%s, %s, %s, %s, NOW())
            ON CONFLICT (path) DO UPDATE 
                SET content = EXCLUDED.content, 
                    embedding = EXCLUDED.embedding,
                    source = EXCLUDED.source,
                    updated_at = NOW()
            RETURNING id, path, created_at, updated_at
        """, (path, content, embedding, source))
        
        result = cur.fetchone()
        conn.commit()
        conn.close()
        
        return jsonify({
            "status": "indexed",
            "id": result["id"],
            "path": result["path"],
            "size": len(content),
            "source": source,
            "created_at": result["created_at"].isoformat(),
            "updated_at": result["updated_at"].isoformat()
        }), 201
    
    except Exception as e:
        logger.error(f"Upload error: {e}")
        return jsonify({"error": str(e)}), 500

@app.route("/documents", methods=["GET"])
def list_documents():
    """
    List all indexed documents
    
    GET /documents?limit=50&offset=0
    """
    try:
        limit = int(request.args.get("limit", 50))
        offset = int(request.args.get("offset", 0))
        
        if limit > 100:
            limit = 100
        
        conn = get_db()
        cur = conn.cursor(cursor_factory=RealDictCursor)
        
        # Total count
        cur.execute("SELECT COUNT(*) as count FROM documents")
        total = cur.fetchone()["count"]
        
        # Get paginated results
        cur.execute("""
            SELECT id, path, source, created_at, updated_at,
                   LENGTH(content) as size_bytes
            FROM documents
            ORDER BY updated_at DESC
            LIMIT %s OFFSET %s
        """, (limit, offset))
        
        docs = cur.fetchall()
        conn.close()
        
        return jsonify({
            "total": total,
            "limit": limit,
            "offset": offset,
            "count": len(docs),
            "documents": [
                {
                    "id": d["id"],
                    "path": d["path"],
                    "source": d["source"],
                    "size_bytes": d["size_bytes"],
                    "created_at": d["created_at"].isoformat(),
                    "updated_at": d["updated_at"].isoformat()
                }
                for d in docs
            ]
        }), 200
    
    except Exception as e:
        logger.error(f"List error: {e}")
        return jsonify({"error": str(e)}), 500

@app.route("/documents/<int:doc_id>", methods=["GET"])
def get_document(doc_id):
    """Get full document by ID"""
    try:
        conn = get_db()
        cur = conn.cursor(cursor_factory=RealDictCursor)
        
        cur.execute("""
            SELECT id, path, content, source, created_at, updated_at
            FROM documents
            WHERE id = %s
        """, (doc_id,))
        
        doc = cur.fetchone()
        conn.close()
        
        if not doc:
            return jsonify({"error": "Not found"}), 404
        
        return jsonify({
            "id": doc["id"],
            "path": doc["path"],
            "content": doc["content"],
            "source": doc["source"],
            "created_at": doc["created_at"].isoformat(),
            "updated_at": doc["updated_at"].isoformat()
        }), 200
    
    except Exception as e:
        logger.error(f"Get document error: {e}")
        return jsonify({"error": str(e)}), 500

@app.route("/documents/<int:doc_id>", methods=["DELETE"])
def delete_document(doc_id):
    """Delete a document"""
    try:
        conn = get_db()
        cur = conn.cursor()
        
        cur.execute("DELETE FROM documents WHERE id = %s RETURNING path", (doc_id,))
        result = cur.fetchone()
        
        if not result:
            conn.close()
            return jsonify({"error": "Not found"}), 404
        
        conn.commit()
        conn.close()
        
        return jsonify({
            "status": "deleted",
            "path": result[0]
        }), 200
    
    except Exception as e:
        logger.error(f"Delete error: {e}")
        return jsonify({"error": str(e)}), 500

@app.route("/stats", methods=["GET"])
def stats():
    """Get indexing stats"""
    try:
        conn = get_db()
        cur = conn.cursor(cursor_factory=RealDictCursor)
        
        cur.execute("""
            SELECT 
                COUNT(*) as total_docs,
                SUM(LENGTH(content)) as total_bytes,
                COUNT(DISTINCT source) as sources,
                MIN(created_at) as oldest,
                MAX(updated_at) as newest
            FROM documents
        """)
        
        stats = cur.fetchone()
        
        cur.execute("SELECT source, COUNT(*) as count FROM documents GROUP BY source")
        by_source = cur.fetchall()
        
        conn.close()
        
        return jsonify({
            "total_documents": stats["total_docs"],
            "total_size_bytes": stats["total_bytes"],
            "total_size_mb": round((stats["total_bytes"] or 0) / (1024*1024), 2),
            "distinct_sources": stats["sources"],
            "oldest": stats["oldest"].isoformat() if stats["oldest"] else None,
            "newest": stats["newest"].isoformat() if stats["newest"] else None,
            "by_source": {s["source"]: s["count"] for s in by_source}
        }), 200
    
    except Exception as e:
        logger.error(f"Stats error: {e}")
        return jsonify({"error": str(e)}), 500

@app.route("/bulk-upload", methods=["POST"])
def bulk_upload():
    """
    Bulk upload from a directory (for rclone-synced folder)
    
    POST /bulk-upload
    {
        "directory": "/data/notes",
        "pattern": "*.md",
        "source": "sync"
    }
    """
    try:
        data = request.get_json()
        directory = data.get("directory", "/data/notes")
        pattern = data.get("pattern", "*.md")
        source = data.get("source", "bulk_upload")
        
        if not os.path.isdir(directory):
            return jsonify({"error": f"Directory not found: {directory}"}), 400
        
        indexed = 0
        errors = []
        
        base_path = Path(directory)
        for file_path in base_path.rglob(pattern):
            try:
                content = file_path.read_text(encoding="utf-8")
                relative_path = str(file_path.relative_to(base_path))
                
                embedding = model.encode(content).tolist()
                
                conn = get_db()
                cur = conn.cursor()
                cur.execute("""
                    INSERT INTO documents (path, content, embedding, source, updated_at)
                    VALUES (%s, %s, %s, %s, NOW())
                    ON CONFLICT (path) DO UPDATE 
                        SET content = EXCLUDED.content, embedding = EXCLUDED.embedding,
                            updated_at = NOW()
                """, (relative_path, content, embedding, source))
                conn.commit()
                conn.close()
                
                indexed += 1
            except Exception as e:
                errors.append({"file": str(file_path), "error": str(e)})
        
        return jsonify({
            "status": "bulk_indexed",
            "indexed": indexed,
            "errors": errors,
            "directory": directory,
            "pattern": pattern
        }), 201
    
    except Exception as e:
        logger.error(f"Bulk upload error: {e}")
        return jsonify({"error": str(e)}), 500

@app.route("/", methods=["GET"])
def index():
    """Serve the web app"""
    return render_template_string(HTML_TEMPLATE)

HTML_TEMPLATE = """
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Knowledge Hub</title>
    <style>
        * {
            margin: 0;
            padding: 0;
            box-sizing: border-box;
        }
        
        body {
            font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
            background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
            min-height: 100vh;
            padding: 20px;
        }
        
        .container {
            max-width: 900px;
            margin: 0 auto;
            background: white;
            border-radius: 12px;
            box-shadow: 0 20px 60px rgba(0,0,0,0.3);
            overflow: hidden;
        }
        
        header {
            background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
            color: white;
            padding: 40px 30px;
            text-align: center;
        }
        
        header h1 {
            font-size: 2em;
            margin-bottom: 5px;
        }
        
        header p {
            opacity: 0.9;
            font-size: 0.9em;
        }
        
        .tabs {
            display: flex;
            border-bottom: 1px solid #e0e0e0;
            background: #f5f5f5;
        }
        
        .tab-button {
            flex: 1;
            padding: 15px;
            border: none;
            background: none;
            cursor: pointer;
            font-size: 1em;
            transition: all 0.3s;
            border-bottom: 3px solid transparent;
        }
        
        .tab-button.active {
            border-bottom-color: #667eea;
            color: #667eea;
            font-weight: 600;
        }
        
        .tab-button:hover {
            background: white;
        }
        
        .content {
            padding: 30px;
        }
        
        .tab-content {
            display: none;
        }
        
        .tab-content.active {
            display: block;
        }
        
        /* Search Tab */
        .search-box {
            display: flex;
            gap: 10px;
            margin-bottom: 20px;
        }
        
        .search-box input {
            flex: 1;
            padding: 12px 15px;
            border: 2px solid #e0e0e0;
            border-radius: 8px;
            font-size: 1em;
            transition: border-color 0.3s;
        }
        
        .search-box input:focus {
            outline: none;
            border-color: #667eea;
        }
        
        .search-box button {
            padding: 12px 30px;
            background: #667eea;
            color: white;
            border: none;
            border-radius: 8px;
            cursor: pointer;
            font-weight: 600;
            transition: background 0.3s;
        }
        
        .search-box button:hover {
            background: #5568d3;
        }
        
        .search-box button:disabled {
            background: #ccc;
            cursor: not-allowed;
        }
        
        .filters {
            display: flex;
            gap: 15px;
            margin-bottom: 20px;
            font-size: 0.9em;
        }
        
        .filter-group {
            display: flex;
            gap: 5px;
            align-items: center;
        }
        
        .filter-group input {
            padding: 6px 10px;
            border: 1px solid #e0e0e0;
            border-radius: 4px;
            font-size: 0.9em;
        }
        
        .results {
            margin-top: 20px;
        }
        
        .result-item {
            background: #f9f9f9;
            padding: 20px;
            margin-bottom: 15px;
            border-radius: 8px;
            border-left: 4px solid #667eea;
            transition: all 0.3s;
        }
        
        .result-item:hover {
            box-shadow: 0 4px 12px rgba(102, 126, 234, 0.15);
        }
        
        .result-header {
            display: flex;
            justify-content: space-between;
            align-items: start;
            margin-bottom: 10px;
        }
        
        .result-path {
            font-weight: 600;
            color: #333;
            word-break: break-all;
        }
        
        .result-similarity {
            background: #667eea;
            color: white;
            padding: 4px 12px;
            border-radius: 20px;
            font-size: 0.85em;
            font-weight: 600;
            white-space: nowrap;
        }
        
        .result-meta {
            font-size: 0.85em;
            color: #666;
            margin-bottom: 10px;
        }
        
        .result-preview {
            color: #555;
            line-height: 1.6;
            white-space: pre-wrap;
            word-wrap: break-word;
            max-height: 200px;
            overflow: hidden;
            position: relative;
        }
        
        .result-preview.expanded {
            max-height: none;
        }
        
        .result-preview::after {
            content: '';
            position: absolute;
            bottom: 0;
            left: 0;
            right: 0;
            height: 40px;
            background: linear-gradient(transparent, #f9f9f9);
        }
        
        .result-preview.expanded::after {
            display: none;
        }
        
        .result-actions {
            margin-top: 10px;
            display: flex;
            gap: 10px;
        }
        
        .result-actions button {
            padding: 6px 12px;
            border: 1px solid #ddd;
            background: white;
            border-radius: 4px;
            cursor: pointer;
            font-size: 0.85em;
            transition: all 0.3s;
        }
        
        .result-actions button:hover {
            border-color: #667eea;
            color: #667eea;
        }
        
        /* Upload Tab */
        .upload-section {
            display: grid;
            grid-template-columns: 1fr 1fr;
            gap: 30px;
        }
        
        .upload-box {
            border: 2px dashed #ddd;
            border-radius: 8px;
            padding: 30px;
            text-align: center;
            cursor: pointer;
            transition: all 0.3s;
        }
        
        .upload-box:hover {
            border-color: #667eea;
            background: #f0f4ff;
        }
        
        .upload-box input[type="file"] {
            display: none;
        }
        
        .upload-box h3 {
            margin-bottom: 10px;
            color: #333;
        }
        
        .upload-box p {
            color: #666;
            font-size: 0.9em;
        }
        
        .form-group {
            margin-bottom: 15px;
        }
        
        .form-group label {
            display: block;
            margin-bottom: 5px;
            font-weight: 500;
            color: #333;
        }
        
        .form-group textarea {
            width: 100%;
            padding: 12px;
            border: 2px solid #e0e0e0;
            border-radius: 8px;
            font-family: monospace;
            font-size: 0.9em;
            resize: vertical;
            min-height: 150px;
        }
        
        .form-group textarea:focus {
            outline: none;
            border-color: #667eea;
        }
        
        .upload-button {
            width: 100%;
            padding: 12px;
            background: #667eea;
            color: white;
            border: none;
            border-radius: 8px;
            cursor: pointer;
            font-weight: 600;
            transition: background 0.3s;
        }
        
        .upload-button:hover {
            background: #5568d3;
        }
        
        .upload-button:disabled {
            background: #ccc;
            cursor: not-allowed;
        }
        
        /* Stats Tab */
        .stats-grid {
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(200px, 1fr));
            gap: 20px;
            margin-bottom: 30px;
        }
        
        .stat-card {
            background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
            color: white;
            padding: 20px;
            border-radius: 8px;
            text-align: center;
        }
        
        .stat-card .value {
            font-size: 2em;
            font-weight: bold;
            margin-bottom: 5px;
        }
        
        .stat-card .label {
            font-size: 0.9em;
            opacity: 0.9;
        }
        
        .sources-list {
            background: #f9f9f9;
            padding: 20px;
            border-radius: 8px;
        }
        
        .sources-list h3 {
            margin-bottom: 15px;
            color: #333;
        }
        
        .source-item {
            display: flex;
            justify-content: space-between;
            padding: 10px 0;
            border-bottom: 1px solid #e0e0e0;
        }
        
        .source-item:last-child {
            border-bottom: none;
        }
        
        .source-item .name {
            font-weight: 500;
        }
        
        .source-item .count {
            color: #667eea;
            font-weight: 600;
        }
        
        /* Documents Tab */
        .documents-table {
            width: 100%;
            border-collapse: collapse;
        }
        
        .documents-table thead {
            background: #f5f5f5;
        }
        
        .documents-table th {
            padding: 12px;
            text-align: left;
            font-weight: 600;
            color: #333;
            border-bottom: 2px solid #e0e0e0;
        }
        
        .documents-table td {
            padding: 12px;
            border-bottom: 1px solid #e0e0e0;
        }
        
        .documents-table tr:hover {
            background: #f9f9f9;
        }
        
        .doc-path {
            word-break: break-all;
            max-width: 400px;
        }
        
        .doc-actions button {
            padding: 6px 10px;
            margin-right: 5px;
            border: 1px solid #ddd;
            background: white;
            border-radius: 4px;
            cursor: pointer;
            font-size: 0.85em;
            transition: all 0.3s;
        }
        
        .doc-actions button:hover {
            border-color: #667eea;
            color: #667eea;
        }
        
        .doc-actions button.delete:hover {
            border-color: #e74c3c;
            color: #e74c3c;
        }
        
        .pagination {
            display: flex;
            justify-content: center;
            gap: 10px;
            margin-top: 20px;
        }
        
        .pagination button {
            padding: 8px 12px;
            border: 1px solid #ddd;
            background: white;
            border-radius: 4px;
            cursor: pointer;
            transition: all 0.3s;
        }
        
        .pagination button:hover {
            border-color: #667eea;
            color: #667eea;
        }
        
        .pagination button:disabled {
            opacity: 0.5;
            cursor: not-allowed;
        }
        
        /* Loading/Messages */
        .loading {
            display: inline-block;
            width: 20px;
            height: 20px;
            border: 3px solid #e0e0e0;
            border-top: 3px solid #667eea;
            border-radius: 50%;
            animation: spin 0.8s linear infinite;
        }
        
        @keyframes spin {
            0% { transform: rotate(0deg); }
            100% { transform: rotate(360deg); }
        }
        
        .message {
            padding: 15px;
            border-radius: 8px;
            margin-bottom: 15px;
            display: none;
        }
        
        .message.show {
            display: block;
        }
        
        .message.success {
            background: #d4edda;
            color: #155724;
            border: 1px solid #c3e6cb;
        }
        
        .message.error {
            background: #f8d7da;
            color: #721c24;
            border: 1px solid #f5c6cb;
        }
        
        .message.info {
            background: #d1ecf1;
            color: #0c5460;
            border: 1px solid #bee5eb;
        }
        
        .empty-state {
            text-align: center;
            padding: 40px 20px;
            color: #999;
        }
        
        .empty-state svg {
            width: 60px;
            height: 60px;
            margin-bottom: 15px;
            opacity: 0.5;
        }
    </style>
</head>
<body>
    <div class="container">
        <header>
            <h1>📚 Knowledge Hub</h1>
            <p>Search and manage your personal knowledge base</p>
        </header>
        
        <div class="tabs">
            <button class="tab-button active" data-tab="search">Search</button>
            <button class="tab-button" data-tab="upload">Upload</button>
            <button class="tab-button" data-tab="documents">Documents</button>
            <button class="tab-button" data-tab="stats">Stats</button>
        </div>
        
        <div class="content">
            <div id="message" class="message"></div>
            
            <!-- Search Tab -->
            <div id="search" class="tab-content active">
                <div class="search-box">
                    <input 
                        type="text" 
                        id="query" 
                        placeholder="Ask anything about your knowledge base..."
                        autocomplete="off"
                    >
                    <button onclick="performSearch()">Search</button>
                </div>
                
                <div class="filters">
                    <div class="filter-group">
                        <label>Results:</label>
                        <input type="number" id="limit" value="5" min="1" max="50" style="width: 60px;">
                    </div>
                    <div class="filter-group">
                        <label>Min similarity:</label>
                        <input type="number" id="threshold" value="0" min="0" max="1" step="0.1" style="width: 60px;">
                    </div>
                </div>
                
                <div class="results" id="results"></div>
            </div>
            
            <!-- Upload Tab -->
            <div id="upload" class="tab-content">
                <div class="upload-section">
                    <div>
                        <h3>📄 Upload File</h3>
                        <div class="upload-box" onclick="document.getElementById('fileInput').click()">
                            <input type="file" id="fileInput" accept=".md,.txt,.pdf">
                            <h3>📁</h3>
                            <p>Click to upload or drag and drop</p>
                            <p style="font-size: 0.8em; margin-top: 5px;">Supports .md, .txt, .pdf</p>
                        </div>
                        <div style="margin-top: 10px;">
                            <label>Source (optional):</label>
                            <input type="text" id="fileSource" placeholder="e.g., upload, import" style="width: 100%; padding: 8px; border: 1px solid #e0e0e0; border-radius: 4px;">
                        </div>
                        <button class="upload-button" style="margin-top: 10px;" onclick="uploadFile()">Upload</button>
                    </div>
                    
                    <div>
                        <h3>✏️ Paste Content</h3>
                        <div class="form-group">
                            <label>Path (optional):</label>
                            <input 
                                type="text" 
                                id="docPath" 
                                placeholder="e.g., notes/my-note.md"
                                style="width: 100%; padding: 8px; border: 1px solid #e0e0e0; border-radius: 4px;"
                            >
                        </div>
                        <div class="form-group">
                            <label>Content:</label>
                            <textarea id="docContent" placeholder="Paste your content here..."></textarea>
                        </div>
                        <button class="upload-button" onclick="uploadContent()">Save</button>
                    </div>
                </div>
            </div>
            
            <!-- Documents Tab -->
            <div id="documents" class="tab-content">
                <div style="margin-bottom: 20px;">
                    <button class="upload-button" style="width: auto;" onclick="refreshDocuments()">Refresh</button>
                </div>
                <div id="documentsContainer"></div>
            </div>
            
            <!-- Stats Tab -->
            <div id="stats" class="tab-content">
                <div class="stats-grid" id="statsGrid"></div>
                <div class="sources-list" id="sourcesList"></div>
            </div>
        </div>
    </div>
    
    <script>
        // Tab switching
        document.querySelectorAll('.tab-button').forEach(button => {
            button.addEventListener('click', (e) => {
                const tabName = e.target.dataset.tab;
                
                document.querySelectorAll('.tab-content').forEach(content => {
                    content.classList.remove('active');
                });
                document.querySelectorAll('.tab-button').forEach(btn => {
                    btn.classList.remove('active');
                });
                
                document.getElementById(tabName).classList.add('active');
                e.target.classList.add('active');
                
                if (tabName === 'stats') loadStats();
                if (tabName === 'documents') loadDocuments();
            });
        });
        
        // Enter key to search
        document.getElementById('query').addEventListener('keypress', (e) => {
            if (e.key === 'Enter') performSearch();
        });
        
        // File drag and drop
        const fileInput = document.getElementById('fileInput');
        const uploadBox = document.querySelector('.upload-box');
        
        uploadBox.addEventListener('dragover', (e) => {
            e.preventDefault();
            uploadBox.style.borderColor = '#667eea';
            uploadBox.style.background = '#f0f4ff';
        });
        
        uploadBox.addEventListener('dragleave', () => {
            uploadBox.style.borderColor = '#ddd';
            uploadBox.style.background = 'white';
        });
        
        uploadBox.addEventListener('drop', (e) => {
            e.preventDefault();
            uploadBox.style.borderColor = '#ddd';
            uploadBox.style.background = 'white';
            fileInput.files = e.dataTransfer.files;
        });
        
        // Helper functions
        function showMessage(text, type = 'info') {
            const msg = document.getElementById('message');
            msg.textContent = text;
            msg.className = `message show ${type}`;
            setTimeout(() => msg.classList.remove('show'), 5000);
        }
        
        async function performSearch() {
            const query = document.getElementById('query').value.trim();
            if (!query) {
                showMessage('Please enter a search query', 'error');
                return;
            }
            
            const limit = document.getElementById('limit').value;
            const threshold = document.getElementById('threshold').value;
            
            const resultsDiv = document.getElementById('results');
            resultsDiv.innerHTML = '<div class="loading"></div> Searching...';
            
            try {
                const response = await fetch('/query', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({q: query, limit: parseInt(limit), threshold: parseFloat(threshold)})
                });
                
                const data = await response.json();
                
                if (!response.ok) {
                    showMessage(data.error || 'Search failed', 'error');
                    resultsDiv.innerHTML = '';
                    return;
                }
                
                if (data.results.length === 0) {
                    resultsDiv.innerHTML = '<div class="empty-state"><p>No results found</p></div>';
                    return;
                }
                
                resultsDiv.innerHTML = data.results.map((r, i) => `
                    <div class="result-item">
                        <div class="result-header">
                            <div class="result-path">${r.path}</div>
                            <div class="result-similarity">${(r.similarity * 100).toFixed(0)}%</div>
                        </div>
                        <div class="result-meta">
                            Source: ${r.source} • Updated: ${new Date(r.updated_at).toLocaleDateString()}
                        </div>
                        <div class="result-preview" id="preview-${i}">${escapeHtml(r.content_full)}</div>
                        <div class="result-actions">
                            <button onclick="togglePreview(${i})">View Full</button>
                        </div>
                    </div>
                `).join('');
                
            } catch (err) {
                showMessage(`Error: ${err.message}`, 'error');
                resultsDiv.innerHTML = '';
            }
        }
        
        async function uploadFile() {
            const file = document.getElementById('fileInput').files[0];
            if (!file) {
                showMessage('Please select a file', 'error');
                return;
            }
            
            const formData = new FormData();
            formData.append('file', file);
            formData.append('source', document.getElementById('fileSource').value || 'file_upload');
            
            try {
                const response = await fetch('/upload', {method: 'POST', body: formData});
                const data = await response.json();
                
                if (!response.ok) {
                    showMessage(data.error || 'Upload failed', 'error');
                    return;
                }
                
                showMessage(`✓ Indexed: ${data.path}`, 'success');
                document.getElementById('fileInput').value = '';
                document.getElementById('fileSource').value = '';
                
            } catch (err) {
                showMessage(`Error: ${err.message}`, 'error');
            }
        }
        
        async function uploadContent() {
            const content = document.getElementById('docContent').value.trim();
            if (!content) {
                showMessage('Please enter some content', 'error');
                return;
            }
            
            try {
                const response = await fetch('/upload', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({
                        content,
                        path: document.getElementById('docPath').value || `doc-${Date.now()}.md`,
                        source: 'manual'
                    })
                });
                
                const data = await response.json();
                
                if (!response.ok) {
                    showMessage(data.error || 'Upload failed', 'error');
                    return;
                }
                
                showMessage(`✓ Saved: ${data.path}`, 'success');
                document.getElementById('docContent').value = '';
                document.getElementById('docPath').value = '';
                
            } catch (err) {
                showMessage(`Error: ${err.message}`, 'error');
            }
        }
        
        async function loadStats() {
            try {
                const response = await fetch('/stats');
                const stats = await response.json();
                
                document.getElementById('statsGrid').innerHTML = `
                    <div class="stat-card">
                        <div class="value">${stats.total_documents}</div>
                        <div class="label">Documents</div>
                    </div>
                    <div class="stat-card">
                        <div class="value">${stats.total_size_mb}MB</div>
                        <div class="label">Total Size</div>
                    </div>
                    <div class="stat-card">
                        <div class="value">${stats.distinct_sources}</div>
                        <div class="label">Sources</div>
                    </div>
                `;
                
                const sourceHtml = Object.entries(stats.by_source)
                    .map(([source, count]) => `
                        <div class="source-item">
                            <div class="name">${source}</div>
                            <div class="count">${count}</div>
                        </div>
                    `).join('');
                
                document.getElementById('sourcesList').innerHTML = `
                    <h3>By Source</h3>
                    ${sourceHtml || '<p>No sources yet</p>'}
                `;
                
            } catch (err) {
                showMessage(`Error loading stats: ${err.message}`, 'error');
            }
        }
        
        let currentDocPage = 0;
        async function loadDocuments(offset = 0) {
            currentDocPage = offset;
            try {
                const response = await fetch(`/documents?limit=10&offset=${offset}`);
                const data = await response.json();
                
                const tableHtml = `
                    <table class="documents-table">
                        <thead>
                            <tr>
                                <th>Path</th>
                                <th>Source</th>
                                <th>Size</th>
                                <th>Updated</th>
                                <th>Actions</th>
                            </tr>
                        </thead>
                        <tbody>
                            ${data.documents.map(d => `
                                <tr>
                                    <td class="doc-path">${d.path}</td>
                                    <td>${d.source}</td>
                                    <td>${(d.size_bytes / 1024).toFixed(1)}KB</td>
                                    <td>${new Date(d.updated_at).toLocaleDateString()}</td>
                                    <td class="doc-actions">
                                        <button onclick="viewDocument(${d.id})">View</button>
                                        <button class="delete" onclick="deleteDocument(${d.id})">Delete</button>
                                    </td>
                                </tr>
                            `).join('')}
                        </tbody>
                    </table>
                `;
                
                const pagination = `
                    <div class="pagination">
                        <button onclick="loadDocuments(0)" ${offset === 0 ? 'disabled' : ''}>« First</button>
                        <button onclick="loadDocuments(${Math.max(0, offset - 10)})" ${offset === 0 ? 'disabled' : ''}>‹ Prev</button>
                        <span style="padding: 8px;">Page ${Math.floor(offset / 10) + 1}</span>
                        <button onclick="loadDocuments(${offset + 10})" ${data.documents.length < 10 ? 'disabled' : ''}>Next ›</button>
                    </div>
                `;
                
                document.getElementById('documentsContainer').innerHTML = tableHtml + pagination;
                
            } catch (err) {
                showMessage(`Error loading documents: ${err.message}`, 'error');
            }
        }
        
        async function viewDocument(id) {
            try {
                const response = await fetch(`/documents/${id}`);
                const doc = await response.json();
                
                const preview = `
                    <div style="background: white; padding: 20px; border-radius: 8px; margin-top: 15px;">
                        <h3>${doc.path}</h3>
                        <p style="color: #666; margin-bottom: 10px;">
                            Source: ${doc.source} • Updated: ${new Date(doc.updated_at).toLocaleString()}
                        </p>
                        <pre style="background: #f5f5f5; padding: 15px; border-radius: 4px; overflow: auto; max-height: 300px;">${escapeHtml(doc.content)}</pre>
                    </div>
                `;
                
                const container = document.getElementById('documentsContainer');
                const existing = container.querySelector('[style*="background: white"]');
                if (existing) existing.remove();
                container.insertAdjacentHTML('beforeend', preview);
                
            } catch (err) {
                showMessage(`Error: ${err.message}`, 'error');
            }
        }
        
        async function deleteDocument(id) {
            if (!confirm('Delete this document?')) return;
            
            try {
                const response = await fetch(`/documents/${id}`, {method: 'DELETE'});
                const data = await response.json();
                
                if (!response.ok) {
                    showMessage(data.error || 'Delete failed', 'error');
                    return;
                }
                
                showMessage(`✓ Deleted: ${data.path}`, 'success');
                loadDocuments(currentDocPage);
                
            } catch (err) {
                showMessage(`Error: ${err.message}`, 'error');
            }
        }
        
        function togglePreview(index) {
            const preview = document.getElementById(`preview-${index}`);
            preview.classList.toggle('expanded');
        }
        
        function refreshDocuments() {
            loadDocuments(0);
        }
        
        function escapeHtml(text) {
            const map = {'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#039;'};
            return text.replace(/[&<>"']/g, m => map[m]);
        }
    </script>
</body>
</html>
"""

if __name__ == "__main__":
    ensure_table()
    app.run(host="0.0.0.0", port=5000, debug=False)