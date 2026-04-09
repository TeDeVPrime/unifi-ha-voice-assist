ARG BUILD_FROM=ghcr.io/home-assistant/base:latest
FROM ${BUILD_FROM}

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

RUN apk add --no-cache \
    python3 \
    py3-pip \
    ffmpeg \
    ca-certificates \
    bash

COPY requirements.txt /tmp/requirements.txt

RUN pip3 install --no-cache-dir -r /tmp/requirements.txt

COPY run.sh /run.sh
COPY app /app
COPY translations /translations

RUN chmod a+x /run.sh

CMD ["/run.sh"]
