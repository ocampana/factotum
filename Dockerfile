# syntax=docker/dockerfile:1
#
# factotum — immagine multi-stage basata su Alpine.
# Stage 1 compila pjproject (pjsip) e i binding Python pjsua2;
# stage 2 contiene solo l'interprete, il virtualenv e le librerie runtime.

ARG PYTHON_VERSION=3.12
ARG ALPINE_VERSION=3.22
ARG PJPROJECT_VERSION=2.17

# ---------------------------------------------------------------- build
FROM python:${PYTHON_VERSION}-alpine${ALPINE_VERSION} AS build
ARG PJPROJECT_VERSION

RUN apk add --no-cache build-base linux-headers swig openssl-dev

WORKDIR /src
RUN wget -qO- https://github.com/pjsip/pjproject/archive/refs/tags/${PJPROJECT_VERSION}.tar.gz | tar xz \
 && mv pjproject-${PJPROJECT_VERSION} pjproject

# Solo l'indispensabile: niente audio di sistema, video e codec non usati.
# Restano G.711 (PCMA/PCMU), G.722, il resampler e TLS/SRTP.
WORKDIR /src/pjproject
RUN printf '#define PJ_HAS_IPV6 1\n#define PJMEDIA_HAS_VIDEO 0\n#define PJSUA_MAX_CALLS 4\n#define PJ_LOG_MAX_LEVEL 4\n' \
      > pjlib/include/pj/config_site.h \
 && ./configure \
      CFLAGS="-Os -fPIC -DNDEBUG" CXXFLAGS="-Os -fPIC -DNDEBUG" \
      --disable-sound --disable-video --disable-speex-aec --disable-speex-codec \
      --disable-gsm-codec --disable-ilbc-codec --disable-l16-codec --disable-g7221-codec \
      --disable-opencore-amr --disable-silk --disable-opus --disable-bcg729 --disable-lyra \
      --disable-sdl --disable-ffmpeg --disable-v4l2 --disable-openh264 --disable-vpx \
      --disable-libyuv --disable-libwebrtc --disable-upnp --disable-android-mediacodec \
 && make dep \
 && make -j"$(nproc)"

# Virtualenv con dipendenze Python e binding pjsua2
RUN python -m venv /opt/venv
ENV PATH=/opt/venv/bin:$PATH
COPY requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir --upgrade pip setuptools wheel \
 && pip install --no-cache-dir -r /tmp/requirements.txt

WORKDIR /src/pjproject/pjsip-apps/src/swig/python
RUN make \
 && pip install --no-cache-dir . \
 && python -c "import pjsua2"

# Riduzione dimensioni: simboli di debug, bytecode, test e strumenti di build
RUN find /opt/venv -name '*.so' -exec strip --strip-unneeded {} + \
 && pip uninstall -y pip setuptools wheel \
 && find /opt/venv -depth \( -name '__pycache__' -o -name 'tests' -o -name 'test' \) -type d -exec rm -rf {} + \
 && find /opt/venv -name '*.pyc' -delete

# ---------------------------------------------------------------- runtime
FROM python:${PYTHON_VERSION}-alpine${ALPINE_VERSION}

RUN apk add --no-cache libstdc++ libgcc openssl ca-certificates tzdata \
 && pip uninstall -y pip setuptools wheel 2>/dev/null || true \
 && find /usr/local/lib -depth -name '__pycache__' -type d -exec rm -rf {} + \
 && rm -rf /usr/local/lib/python3*/ensurepip /usr/local/lib/python3*/idlelib \
           /usr/local/lib/python3*/tkinter /usr/local/lib/python3*/turtledemo \
           /usr/local/lib/python3*/test /root/.cache \
 && adduser -D -H -u 1000 factotum \
 && mkdir -p /data && chown factotum /data

COPY --from=build /opt/venv /opt/venv

WORKDIR /app
COPY app.py ./
COPY factotum ./factotum
COPY config ./config

ENV PATH=/opt/venv/bin:$PATH \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    MALLOC_ARENA_MAX=2 \
    FACTOTUM_DATA_DIR=/data \
    TZ=Europe/Rome

USER factotum
VOLUME ["/data"]
EXPOSE 8080 5060/udp 5060/tcp

HEALTHCHECK --interval=60s --timeout=5s --start-period=30s \
  CMD wget -qO- http://127.0.0.1:${FACTOTUM_WEB_PORT:-8080}/healthz >/dev/null || exit 1

CMD ["python", "app.py"]
