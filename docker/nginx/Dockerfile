FROM nginx:1.27-alpine

RUN apk add --no-cache openssl curl

COPY default.conf /etc/nginx/conf.d/default.conf
COPY self-signed.sh /docker-entrypoint.d/40-self-signed.sh

RUN chmod +x /docker-entrypoint.d/40-self-signed.sh

EXPOSE 80 443

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD curl -fk https://127.0.0.1/healthz || exit 1
