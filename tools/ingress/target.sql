-- Run once on the isolated reference target, never inside an Adapter invocation.
CREATE TABLE IF NOT EXISTS dlr_ingress_results (
    run_id varchar(80) NOT NULL,
    message_id varchar(128) NOT NULL,
    seq integer NOT NULL,
    payload_sha256 char(64) NOT NULL,
    normalized_value bigint NOT NULL,
    writes integer NOT NULL DEFAULT 1 CHECK (writes = 1),
    attempts integer NOT NULL DEFAULT 0 CHECK (attempts BETWEEN 0 AND 64),
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (run_id, message_id)
);
CREATE TABLE IF NOT EXISTS dlr_ingress_attempts (
    run_id varchar(80) NOT NULL,
    message_id varchar(128) NOT NULL,
    ordinal integer NOT NULL CHECK (ordinal BETWEEN 1 AND 64),
    outcome varchar(16) NOT NULL CHECK (outcome IN ('inserted', 'duplicate')),
    payload_sha256 char(64) NOT NULL,
    normalized_value bigint NOT NULL,
    handle_started_at timestamptz NOT NULL,
    connected_at timestamptz NOT NULL,
    recorded_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (run_id, message_id, ordinal),
    FOREIGN KEY (run_id, message_id) REFERENCES dlr_ingress_results (run_id, message_id)
);
