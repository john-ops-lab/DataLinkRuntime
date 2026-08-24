FROM postgres:16-alpine

COPY docker/postgres-entrypoint.sh /usr/local/bin/dlr-postgres-entrypoint.sh
RUN chmod 0755 /usr/local/bin/dlr-postgres-entrypoint.sh

ENTRYPOINT ["/usr/local/bin/dlr-postgres-entrypoint.sh"]
