FROM python:3.11-slim
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 PIP_NO_CACHE_DIR=1 \
    MODEL_CACHE_DIR=/opt/models DATA_DIR=/data
WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt
ARG EMBED_MODEL=intfloat/multilingual-e5-small
ENV EMBED_MODEL=${EMBED_MODEL}
COPY hub ./hub
RUN python -m hub.embeddings --download
RUN useradd -m -u 10001 hub && mkdir -p /data /opt/models \
 && chown -R hub:hub /data /opt/models
USER hub
VOLUME ["/data"]
EXPOSE 5000
HEALTHCHECK --interval=30s --timeout=5s --start-period=60s --retries=3 \
  CMD python -c "import urllib.request as u; u.urlopen('http://127.0.0.1:5000/health', timeout=4)"
CMD ["sh","-c","exec gunicorn -b 0.0.0.0:${PORT:-5000} -w 1 --threads ${GUNICORN_THREADS:-4} --timeout 120 'hub:create_app()'"]
