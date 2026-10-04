FROM python:3.11-slim
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 PIP_NO_CACHE_DIR=1
WORKDIR /app
COPY hub_mcp/requirements.txt .
RUN pip install -r requirements.txt
COPY hub_mcp ./hub_mcp
RUN useradd -m -u 10002 mcp
USER mcp
EXPOSE 8000
CMD ["python", "-m", "hub_mcp.server"]
