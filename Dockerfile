FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates curl \
 && rm -rf /var/lib/apt/lists/*

# Allow-list ONLY the runtime files we need
COPY requirements.txt /app/requirements.txt
COPY entrypoint.sh /app/entrypoint.sh
COPY dpmp /app/dpmp
COPY gui_nice /app/gui_nice

RUN pip install --no-cache-dir -U pip \
 && pip install --no-cache-dir -r /app/requirements.txt

RUN mkdir -p /data
EXPOSE 3351 9210 8855
RUN chmod +x /app/entrypoint.sh

CMD ["/app/entrypoint.sh"]
