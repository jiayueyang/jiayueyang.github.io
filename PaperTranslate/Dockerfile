FROM python:3.12-slim-bookworm
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 PT_DATA_DIR=/var/lib/papertranslate MPLCONFIGDIR=/tmp/mpl
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg fonts-noto-cjk ca-certificates && rm -rf /var/lib/apt/lists/* \
    && useradd --create-home --uid 10001 paper && install -d -o paper -g paper /var/lib/papertranslate
WORKDIR /app
COPY requirements*.txt ./
RUN pip install --no-cache-dir -r requirements.txt
COPY papertranslate/ papertranslate/
COPY web/ web/
USER paper
EXPOSE 8000
CMD ["uvicorn", "papertranslate.api:app", "--host", "0.0.0.0", "--port", "8000", "--no-access-log", "--no-proxy-headers"]
