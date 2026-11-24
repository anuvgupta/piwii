FROM python:3.13-slim

# wit (Wiimms ISO Tools) converts ISO/CISO/WIA/... to split WBFS.
# Prebuilt Linux x86_64 release from https://wit.wiimm.de/download.html
ARG WIT_VERSION=v3.05a-r8638
RUN apt-get update \
 && apt-get install -y --no-install-recommends ca-certificates curl \
 && curl -fsSL "https://wit.wiimm.de/download/wit-${WIT_VERSION}-x86_64.tar.gz" | tar -xz -C /tmp \
 && install -m 755 /tmp/wit-${WIT_VERSION}-x86_64/bin/wit /usr/local/bin/wit \
 && rm -rf /tmp/wit-* /var/lib/apt/lists/* \
 && wit --version

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app.py .
COPY static static

EXPOSE 8790
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8790"]
