FROM alpine:3.20

RUN apk add --no-cache bash curl docker-cli docker-cli-compose gzip jq

COPY update.sh /usr/local/bin/update.sh
RUN chmod +x /usr/local/bin/update.sh

ENTRYPOINT ["/usr/local/bin/update.sh"]
