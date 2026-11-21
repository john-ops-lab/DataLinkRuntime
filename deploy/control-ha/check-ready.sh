#!/bin/sh
case "${1:-}" in
    18080|18081) ;;
    *) exit 1 ;;
esac

status=$(/usr/bin/curl -q --noproxy 127.0.0.1 --fail --silent --max-time 0.5 \
    --output /dev/null --write-out '%{http_code}' "http://127.0.0.1:$1/ready") || exit 1
[ "$status" = 200 ]
