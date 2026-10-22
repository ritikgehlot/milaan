FROM python:3.14-slim

WORKDIR /app
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt
COPY . ./
RUN useradd --create-home --uid 10001 milaan && mkdir -p /data && chown -R milaan:milaan /app /data
USER milaan
ENV APP_ENV=production DATABASE_PATH=/data/milaan.db MILAAN_AUTH_REQUIRED=true
EXPOSE 8501
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 CMD python scripts/healthcheck.py
CMD ["streamlit", "run", "app.py", "--server.address=0.0.0.0", "--server.port=8501", "--server.headless=true"]
