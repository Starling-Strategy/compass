-- 005_answer_holds.sql — Quality HOLD system for suggested answers
--
-- Allows power users to freeze specific answers from analyst review
-- while keeping them visible. Holds are an administrative overlay,
-- entirely separate from suggested_answers to avoid interfering with
-- Nathan's pipeline ON CONFLICT logic.

BEGIN;

CREATE TABLE IF NOT EXISTS silver.answer_holds (
    id              BIGSERIAL PRIMARY KEY,
    district_id     INTEGER NOT NULL,
    ay_id           INTEGER NOT NULL,
    q_id            INTEGER NOT NULL,
    held_by         TEXT NOT NULL,
    hold_reason     TEXT NOT NULL,
    held_at         TIMESTAMPTZ DEFAULT NOW(),
    released_by     TEXT,
    released_at     TIMESTAMPTZ,
    is_active       BOOLEAN DEFAULT TRUE
);

-- Only one active hold per answer at a time
CREATE UNIQUE INDEX IF NOT EXISTS idx_answer_holds_active_unique
    ON silver.answer_holds (district_id, ay_id, q_id)
    WHERE is_active = TRUE;

-- Fast lookup for LEFT JOINs from question lists
CREATE INDEX IF NOT EXISTS idx_answer_holds_lookup
    ON silver.answer_holds (district_id, ay_id, q_id, is_active);

COMMIT;
