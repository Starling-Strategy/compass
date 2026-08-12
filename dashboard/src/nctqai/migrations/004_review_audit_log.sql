BEGIN;

CREATE TABLE IF NOT EXISTS silver.review_audit_log (
    id              BIGSERIAL PRIMARY KEY,
    district_id     INTEGER NOT NULL,
    ay_id           INTEGER NOT NULL,
    q_id            INTEGER NOT NULL,
    action          TEXT NOT NULL,        -- 'approved' | 'incorrect' | 'unreviewed'
    reviewed_by     TEXT NOT NULL,
    reviewed_at     TIMESTAMPTZ DEFAULT NOW(),
    rejection_reason TEXT,
    decision_note   TEXT,
    footnote        TEXT,
    page_ref        TEXT
);

CREATE INDEX IF NOT EXISTS idx_review_audit_log_question
    ON silver.review_audit_log (district_id, ay_id, q_id);

CREATE INDEX IF NOT EXISTS idx_review_audit_log_reviewer
    ON silver.review_audit_log (reviewed_by, reviewed_at DESC);

COMMIT;
