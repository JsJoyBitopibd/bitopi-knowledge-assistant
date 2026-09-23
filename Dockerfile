# Bitopi Knowledge Assistant — container image.
#
# Deliberately NOT baked in (mounted at run time instead, see README / docker run):
#   - .env            secrets: Gemini key + SQL Server credentials
#   - data/index/     the Chroma + BM25 index, so re-ingest needs no rebuild
#   - model weights   bge-m3 + bge-reranker-v2-m3 (~6.5 GB) via HF_HOME
FROM python:3.11-slim

# ODBC Driver 18 — pyodbc needs it to reach SQL Server. The driver name it
# registers matches what .env already uses: {ODBC Driver 18 for SQL Server}
RUN apt-get update && apt-get install -y --no-install-recommends curl gnupg ca-certificates \
 && curl -fsSL https://packages.microsoft.com/keys/microsoft.asc \
      | gpg --dearmor -o /usr/share/keyrings/microsoft-prod.gpg \
 && echo "deb [signed-by=/usr/share/keyrings/microsoft-prod.gpg] https://packages.microsoft.com/debian/12/prod bookworm main" \
      > /etc/apt/sources.list.d/mssql-release.list \
 && apt-get update \
 && ACCEPT_EULA=Y apt-get install -y --no-install-recommends msodbcsql18 unixodbc-dev \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# CPU-only torch first: the default PyPI wheel pulls the CUDA build and ~2.5 GB
# of NVIDIA libraries this project never uses (embeddings/reranking run on CPU).
COPY requirements.txt .
RUN pip install --no-cache-dir torch --index-url https://download.pytorch.org/whl/cpu \
 && pip install --no-cache-dir -r requirements.txt

COPY src/ ./src/
COPY scripts/ ./scripts/
COPY prompts/ ./prompts/
COPY config/ ./config/
COPY pytest.ini ./

ENV HF_HOME=/models \
    PYTHONPATH=/app/src \
    PYTHONUNBUFFERED=1

EXPOSE 8501
CMD ["streamlit", "run", "src/ragbot/app.py", \
     "--server.port", "8501", "--server.address", "0.0.0.0", \
     "--server.headless", "true", "--browser.gatherUsageStats", "false"]
