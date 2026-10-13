FROM postgres:18.6-bookworm@sha256:1c59e2c3c818eaa0f0628f695b36e7c9e362d6b219b36a54a32df645cbd7e1af
LABEL org.opencontainers.image.title="Wholly Crypto merchant container runtime" \
      org.opencontainers.image.source="https://github.com/whollycrypto-com/whollycrypto-docker" \
      org.opencontainers.image.version="1.1.1"
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
RUN printf '#!/bin/sh\nexit 101\n' > /usr/sbin/policy-rc.d \
    && apt-get update \
    && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
       python3 nginx certbot python3-certbot-nginx apache2-utils imagemagick \
       curl ca-certificates openssl file tar gzip util-linux iproute2 \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/* \
    && rm -f /etc/nginx/sites-enabled/default \
    && mkdir -p /etc/systemd/system /root/whollycrypto /run/whollycrypto-container \
       /var/www/html/.well-known/acme-challenge \
    && chmod 700 /root/whollycrypto /run/whollycrypto-container
RUN ln -sf /dev/stdout /var/log/nginx/access.log \
    && ln -sf /dev/stderr /var/log/nginx/error.log
COPY runtime/ /opt/whollycrypto-docker/
COPY release-signing.pub /opt/whollycrypto-docker/release-signing.pub
RUN chmod 755 /opt/whollycrypto-docker/bridge.py /opt/whollycrypto-docker/cli.py \
    && ln -sf /opt/whollycrypto-docker/bridge.py /usr/bin/systemctl \
    && ln -sf /opt/whollycrypto-docker/bridge.py /usr/bin/systemd-run \
    && ln -sf /opt/whollycrypto-docker/bridge.py /usr/bin/journalctl \
    && ln -sf /opt/whollycrypto-docker/cli.py /usr/local/sbin/whollycrypto
WORKDIR /root/whollycrypto
ENTRYPOINT ["python3", "/opt/whollycrypto-docker/supervisor.py"]
CMD []
