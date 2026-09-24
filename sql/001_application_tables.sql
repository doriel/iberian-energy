-- Application tables in Lakebase, for the agent's write actions.
--
-- Applied by pipelines/00b_apply_lakebase_schema. Idempotent: every statement
-- is safe to run again, so this file is the schema rather than a migration that
-- happened once and drifted.
--
-- Three things every table here has, and each is a rubric line as much as good
-- practice:
--
--   * a unique constraint, so a repeated write updates rather than duplicates.
--     The agent will be asked for the same thing twice, by a user who did not
--     see the first confirmation or by a retry, and the second attempt must not
--     leave two rows behind.
--   * created_at and updated_at, the second maintained by a trigger rather than
--     by whoever remembers, because an application that sets its own updated_at
--     eventually forgets on one path.
--   * REPLICA IDENTITY FULL, without which the change feed out of Lakebase
--     carries only the primary key on an update and the analytics pipeline
--     cannot see what changed.

CREATE SCHEMA IF NOT EXISTS iberian;

-- --------------------------------------------------------------------------
-- updated_at, maintained in one place
-- --------------------------------------------------------------------------

CREATE OR REPLACE FUNCTION iberian.set_updated_at() RETURNS trigger AS $$
BEGIN
    NEW.updated_at = now();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

-- --------------------------------------------------------------------------
-- episodes: the reference table the application reads and labels point at
-- --------------------------------------------------------------------------
--
-- A copy of what gold knows about each episode, kept small: enough to list and
-- describe one in the interface without querying a warehouse on every page.
-- Written by the daily Job, never by the application.
--
-- Why a table we own rather than a synced table from gold. A foreign key needs
-- something stable to point at, and a synced table is managed by Databricks:
-- pointing a constraint at it means the sync and the constraint can disagree
-- about who is in charge. Owning this one keeps referential integrity real,
-- which is the point of having it at all. The heavier read models, the interval
-- series in particular, can still arrive as synced tables where no constraint
-- depends on them.

CREATE TABLE IF NOT EXISTS iberian.episodes (
    episode_key          TEXT        PRIMARY KEY,
    market_day           DATE        NOT NULL,
    start_utc            TIMESTAMPTZ NOT NULL,
    end_utc              TIMESTAMPTZ NOT NULL,
    premium_side         TEXT        NOT NULL CHECK (premium_side IN ('PT', 'ES')),
    peak_abs_spread      NUMERIC(10, 2) NOT NULL CHECK (peak_abs_spread >= 0),
    severity             TEXT        NOT NULL CHECK (severity IN ('minor', 'moderate', 'severe')),
    extra_cost_eur       NUMERIC(14, 2),
    min_capacity_mw      NUMERIC(10, 2),
    capacity_percentile  NUMERIC(5, 1) CHECK (capacity_percentile BETWEEN 0 AND 100),
    share_saturated      NUMERIC(4, 3) CHECK (share_saturated BETWEEN 0 AND 1),
    updated_at           TIMESTAMPTZ NOT NULL DEFAULT now(),
    CHECK (end_utc > start_utc)
);

CREATE INDEX IF NOT EXISTS episodes_market_day_idx
    ON iberian.episodes (market_day DESC);
CREATE INDEX IF NOT EXISTS episodes_severity_idx
    ON iberian.episodes (severity, peak_abs_spread DESC);

COMMENT ON TABLE iberian.episodes IS
    'Market splitting episodes, copied from gold_split_episodes by the daily Job. Read only to the application: the agent never writes here.';
COMMENT ON COLUMN iberian.episodes.capacity_percentile IS
    'Where this episode''s lowest border capacity sits among every quarter hour in the window. Below 25 the border was carrying unusually little.';

-- --------------------------------------------------------------------------
-- alerts: the manufacturer's write action
-- --------------------------------------------------------------------------
--
-- "Tell me when the Portuguese premium goes above 20 EUR/MWh." The one table
-- that exercises all three verbs the agent needs to demonstrate: an alert is
-- created, its threshold is updated, and it is deleted.

CREATE TABLE IF NOT EXISTS iberian.alerts (
    id                 BIGSERIAL   PRIMARY KEY,
    created_by         TEXT        NOT NULL CHECK (length(trim(created_by)) > 0),
    zone               TEXT        NOT NULL CHECK (zone IN ('PT', 'ES')),
    direction          TEXT        NOT NULL CHECK (direction IN ('above', 'below')),
    threshold_eur_mwh  NUMERIC(8, 2) NOT NULL
                       CHECK (threshold_eur_mwh > 0 AND threshold_eur_mwh <= 3000),
    active             BOOLEAN     NOT NULL DEFAULT true,
    created_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (created_by, zone, direction, threshold_eur_mwh)
);

CREATE INDEX IF NOT EXISTS alerts_created_by_idx
    ON iberian.alerts (created_by, created_at DESC);
CREATE INDEX IF NOT EXISTS alerts_active_idx
    ON iberian.alerts (active) WHERE active;

DROP TRIGGER IF EXISTS alerts_set_updated_at ON iberian.alerts;
CREATE TRIGGER alerts_set_updated_at
    BEFORE UPDATE ON iberian.alerts
    FOR EACH ROW EXECUTE FUNCTION iberian.set_updated_at();

ALTER TABLE iberian.alerts REPLICA IDENTITY FULL;

COMMENT ON TABLE iberian.alerts IS
    'Price alerts created through the agent. Written by the application, never by the pipeline.';
COMMENT ON COLUMN iberian.alerts.threshold_eur_mwh IS
    'Upper bound of 3000 is the market price cap, so a typo of an extra zero is rejected rather than stored.';

-- --------------------------------------------------------------------------
-- episode_labels: the analyst's write action, and the ground truth
-- --------------------------------------------------------------------------
--
-- The labelling protocol in evaluation/labelling-protocol.md, moved out of a
-- terminal script and into the application. What is written here flows back
-- into Delta through the change feed and becomes the ground truth the agent is
-- evaluated against, which is the loop closing.
--
-- Unique per episode per person, deliberately. Two people may disagree about
-- the same episode and both rows survive: inter-rater disagreement is a
-- measurement, not a conflict to resolve at write time. The evaluation picks
-- whose labels it trusts; the database keeps all of them.

CREATE TABLE IF NOT EXISTS iberian.episode_labels (
    id           BIGSERIAL   PRIMARY KEY,
    episode_key  TEXT        NOT NULL
                 REFERENCES iberian.episodes (episode_key) ON DELETE CASCADE,
    created_by   TEXT        NOT NULL CHECK (length(trim(created_by)) > 0),
    true_cause   TEXT        NOT NULL CHECK (true_cause IN (
                     'saturation_planned',
                     'saturation_unplanned',
                     'saturation_no_notice',
                     'saturation_ordinary_capacity',
                     'not_saturated',
                     'threshold_artifact',
                     'unclear')),
    confidence   TEXT        NOT NULL CHECK (confidence IN ('high', 'medium', 'low')),
    notes        TEXT,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (episode_key, created_by)
);

CREATE INDEX IF NOT EXISTS episode_labels_episode_idx
    ON iberian.episode_labels (episode_key);
CREATE INDEX IF NOT EXISTS episode_labels_author_idx
    ON iberian.episode_labels (created_by, created_at DESC);

DROP TRIGGER IF EXISTS episode_labels_set_updated_at ON iberian.episode_labels;
CREATE TRIGGER episode_labels_set_updated_at
    BEFORE UPDATE ON iberian.episode_labels
    FOR EACH ROW EXECUTE FUNCTION iberian.set_updated_at();

ALTER TABLE iberian.episode_labels REPLICA IDENTITY FULL;

COMMENT ON TABLE iberian.episode_labels IS
    'Human judgements of what caused an episode. The evaluation ground truth, written through the agent. One row per episode per author, so two authors may disagree and both are kept.';
COMMENT ON COLUMN iberian.episode_labels.true_cause IS
    'From a fixed vocabulary. A free text cause is rejected: the constraint is what stops the ground truth becoming prose nobody can aggregate.';

-- --------------------------------------------------------------------------
-- agent_actions: every tool call, successful or not
-- --------------------------------------------------------------------------
--
-- What makes the analytics pipeline about agent activity rather than about
-- market data. Append only, so no updated_at and no trigger.
--
-- `rejected` and `error` are separate states on purpose. A write the validation
-- refused is the system working; a write that blew up is not, and a tool
-- success rate that mixes them tells you nothing about either.

CREATE TABLE IF NOT EXISTS iberian.agent_actions (
    id            BIGSERIAL   PRIMARY KEY,
    session_id    TEXT        NOT NULL,
    created_by    TEXT,
    tool          TEXT        NOT NULL,
    arguments     JSONB,
    status        TEXT        NOT NULL CHECK (status IN ('ok', 'rejected', 'error')),
    detail        TEXT,
    target_table  TEXT,
    target_id     BIGINT,
    latency_ms    INTEGER     CHECK (latency_ms >= 0),
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS agent_actions_created_at_idx
    ON iberian.agent_actions (created_at DESC);
CREATE INDEX IF NOT EXISTS agent_actions_tool_idx
    ON iberian.agent_actions (tool, status);
CREATE INDEX IF NOT EXISTS agent_actions_session_idx
    ON iberian.agent_actions (session_id, created_at);

ALTER TABLE iberian.agent_actions REPLICA IDENTITY FULL;

COMMENT ON TABLE iberian.agent_actions IS
    'One row per agent tool call. Append only. The source of the usage analytics that the change feed carries back into Delta.';
COMMENT ON COLUMN iberian.agent_actions.status IS
    'ok, rejected when validation refused the write, error when it failed. Kept apart so a tool success rate means something.';
COMMENT ON COLUMN iberian.agent_actions.detail IS
    'Why it was rejected, or what failed. Shown to the user, so it is written in words rather than as a stack trace.';