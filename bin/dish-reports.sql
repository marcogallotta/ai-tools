-- Dish tool v1a operational reports.
--
-- Every block between ``-- report:`` and ``-- end report`` is a standalone,
-- read-only SQLite query against var/dish-tool.db. Results cover the full audit
-- history. For a bounded reporting window, add the same created_at predicate to
-- the first audit_events CTE in the selected query.

-- report: command_counts
WITH command_events AS (
    SELECT
        a.event_id,
        substr(a.event_type, length('dish.') + 1) AS command,
        coalesce(a.actor_agent, '<unknown>') AS actor_agent,
        coalesce(
            s.submission_kind,
            json_extract(a.details, '$.submission_kind')
        ) AS submission_kind,
        coalesce(
            s.change_level,
            json_extract(a.details, '$.change_level')
        ) AS change_level,
        cast(json_extract(a.details, '$.ok') AS INTEGER) AS ok
    FROM audit_events AS a
    LEFT JOIN submissions AS s
        ON s.submission_id = a.submission_id
    WHERE a.event_type LIKE 'dish.%'
)
SELECT
    command,
    actor_agent,
    submission_kind,
    change_level,
    count(*) AS command_count,
    sum(CASE WHEN ok = 1 THEN 1 ELSE 0 END) AS successful_count,
    sum(CASE WHEN ok = 1 THEN 0 ELSE 1 END) AS failed_count
FROM command_events
GROUP BY command, actor_agent, submission_kind, change_level
ORDER BY command, actor_agent, submission_kind, change_level;
-- end report

-- report: validation_failure_rates
WITH dish_commands AS (
    SELECT
        a.event_id,
        substr(a.event_type, length('dish.') + 1) AS command,
        json_extract(a.details, '$.code') AS code,
        a.details
    FROM audit_events AS a
    WHERE a.event_type LIKE 'dish.%'
),
command_totals AS (
    SELECT command, count(*) AS command_events
    FROM dish_commands
    GROUP BY command
),
distinct_failed_rules AS (
    SELECT DISTINCT
        c.event_id,
        c.command,
        json_extract(error.value, '$.rule') AS rule
    FROM dish_commands AS c
    JOIN json_each(c.details, '$.errors') AS error
    WHERE c.code = 'VALIDATION_FAILED'
      AND json_type(error.value, '$.rule') = 'text'
)
SELECT
    r.command,
    r.rule,
    count(*) AS validation_failure_events,
    t.command_events,
    round(1.0 * count(*) / nullif(t.command_events, 0), 4)
        AS validation_failure_rate
FROM distinct_failed_rules AS r
JOIN command_totals AS t USING (command)
GROUP BY r.command, r.rule, t.command_events
ORDER BY validation_failure_events DESC, r.command, r.rule;
-- end report

-- report: rejection_rates
WITH applied_decisions AS (
    SELECT
        a.event_id,
        a.task_gid,
        CASE
            WHEN a.event_type = 'dish.approve'
             AND json_extract(a.details, '$.ok') = 1
             AND json_extract(a.details, '$.state') = 'ready'
                THEN 'approve'
            WHEN a.event_type = 'dish.reject'
             AND (
                (
                    json_extract(a.details, '$.ok') = 1
                    AND json_extract(a.details, '$.state') = 'drafting'
                )
                OR (
                    json_extract(a.details, '$.code') = 'HUMAN_ACTION_REQUIRED'
                    AND json_extract(a.details, '$.state') = 'awaiting_human'
                )
             )
                THEN 'reject'
        END AS decision
    FROM audit_events AS a
    WHERE a.event_type IN ('dish.approve', 'dish.reject')
),
valid_decisions AS (
    SELECT event_id, task_gid, decision
    FROM applied_decisions
    WHERE decision IS NOT NULL
),
rejections_by_task AS (
    SELECT task_gid, count(*) AS rejection_count
    FROM valid_decisions
    WHERE decision = 'reject'
      AND task_gid IS NOT NULL
    GROUP BY task_gid
),
summary AS (
    SELECT
        count(*) AS verifier_decisions,
        coalesce(sum(CASE WHEN decision = 'approve' THEN 1 ELSE 0 END), 0)
            AS approvals,
        coalesce(sum(CASE WHEN decision = 'reject' THEN 1 ELSE 0 END), 0)
            AS rejections
    FROM valid_decisions
),
task_summary AS (
    SELECT
        count(*) AS tasks_with_rejection,
        coalesce(
            sum(CASE WHEN rejection_count >= 2 THEN 1 ELSE 0 END),
            0
        ) AS tasks_with_repeated_rejection
    FROM rejections_by_task
)
SELECT
    s.verifier_decisions,
    s.approvals,
    s.rejections,
    round(1.0 * s.rejections / nullif(s.verifier_decisions, 0), 4)
        AS rejection_rate,
    t.tasks_with_rejection,
    t.tasks_with_repeated_rejection,
    round(
        1.0 * t.tasks_with_repeated_rejection
        / nullif(t.tasks_with_rejection, 0),
        4
    ) AS repeated_rejection_task_rate
FROM summary AS s
CROSS JOIN task_summary AS t;
-- end report

-- report: human_review_rates
WITH successful_rejections AS (
    SELECT
        a.event_id,
        a.task_gid,
        CASE
            WHEN json_extract(a.details, '$.code') = 'HUMAN_ACTION_REQUIRED'
             AND json_extract(a.details, '$.state') = 'awaiting_human'
                THEN 1
            ELSE 0
        END AS escalated
    FROM audit_events AS a
    WHERE a.event_type = 'dish.reject'
      AND (
        (
            json_extract(a.details, '$.ok') = 1
            AND json_extract(a.details, '$.state') = 'drafting'
        )
        OR (
            json_extract(a.details, '$.code') = 'HUMAN_ACTION_REQUIRED'
            AND json_extract(a.details, '$.state') = 'awaiting_human'
        )
      )
),
successful_unblocks AS (
    SELECT a.event_id, a.task_gid
    FROM audit_events AS a
    WHERE a.event_type = 'dish-admin.unblock'
      AND json_extract(a.details, '$.ok') = 1
      AND json_extract(a.details, '$.state') = 'drafting'
),
rejection_summary AS (
    SELECT
        count(*) AS successful_rejections,
        coalesce(sum(escalated), 0) AS human_escalations,
        count(DISTINCT CASE WHEN escalated = 1 THEN task_gid END)
            AS tasks_escalated
    FROM successful_rejections
),
unblock_summary AS (
    SELECT
        count(*) AS successful_unblocks,
        count(DISTINCT task_gid) AS tasks_unblocked
    FROM successful_unblocks
)
SELECT
    r.successful_rejections,
    r.human_escalations,
    round(
        1.0 * r.human_escalations / nullif(r.successful_rejections, 0),
        4
    ) AS human_escalation_rate_per_rejection,
    u.successful_unblocks,
    round(
        1.0 * u.successful_unblocks / nullif(r.human_escalations, 0),
        4
    ) AS unblock_rate_per_escalation,
    r.tasks_escalated,
    u.tasks_unblocked
FROM rejection_summary AS r
CROSS JOIN unblock_summary AS u;
-- end report

-- report: submit_outcomes
WITH submit_events AS (
    SELECT
        coalesce(json_extract(a.details, '$.state'), '<unknown>') AS final_state,
        coalesce(
            json_extract(a.details, '$.write_outcome'),
            'not_attempted'
        ) AS write_outcome,
        coalesce(json_extract(a.details, '$.code'), '<unknown>') AS code,
        cast(json_extract(a.details, '$.ok') AS INTEGER) AS ok
    FROM audit_events AS a
    WHERE a.event_type = 'dish.submit'
),
total AS (
    SELECT count(*) AS submit_events
    FROM submit_events
)
SELECT
    e.final_state,
    e.write_outcome,
    e.code,
    e.ok,
    count(*) AS outcome_count,
    round(1.0 * count(*) / nullif(t.submit_events, 0), 4) AS outcome_rate
FROM submit_events AS e
CROSS JOIN total AS t
GROUP BY e.final_state, e.write_outcome, e.code, e.ok, t.submit_events
ORDER BY outcome_count DESC, e.final_state, e.write_outcome, e.code;
-- end report

-- report: change_diff_distributions
WITH successful_change_prepares AS (
    SELECT
        s.change_level,
        a.details,
        CASE
            WHEN json_type(a.details, '$.change_diff') = 'object'
                THEN 'available'
            ELSE 'unavailable'
        END AS telemetry_status,
        json_extract(
            a.details, '$.change_diff_unavailable'
        ) AS telemetry_unavailable_reason
    FROM audit_events AS a
    JOIN submissions AS s
        ON s.submission_id = a.submission_id
    WHERE a.event_type = 'dish.prepare'
      AND json_extract(a.details, '$.ok') = 1
      AND s.submission_kind = 'change'
),
status_metrics AS (
    SELECT
        change_level,
        'telemetry_status' AS metric,
        telemetry_status AS metric_value
    FROM successful_change_prepares
),
unavailable_reason_metrics AS (
    SELECT
        change_level,
        'telemetry_unavailable_reason' AS metric,
        telemetry_unavailable_reason AS metric_value
    FROM successful_change_prepares
    WHERE telemetry_unavailable_reason IS NOT NULL
),
size_metrics AS (
    SELECT
        change_level,
        metric,
        cast(values_json.value AS TEXT) AS metric_value
    FROM successful_change_prepares
    CROSS JOIN json_each(
        json_array(
            json_extract(details, '$.change_diff.characters_added'),
            json_extract(details, '$.change_diff.characters_removed'),
            json_extract(details, '$.change_diff.lines_added'),
            json_extract(details, '$.change_diff.lines_removed'),
            json_array_length(
                json_extract(details, '$.change_diff.headings_changed')
            )
        )
    ) AS values_json
    JOIN (
        SELECT 0 AS metric_index, 'characters_added' AS metric
        UNION ALL SELECT 1, 'characters_removed'
        UNION ALL SELECT 2, 'lines_added'
        UNION ALL SELECT 3, 'lines_removed'
        UNION ALL SELECT 4, 'headings_changed_count'
    ) AS metric_names
        ON metric_names.metric_index = cast(values_json.key AS INTEGER)
    WHERE json_type(details, '$.change_diff') = 'object'
),
heading_metrics AS (
    SELECT
        events.change_level,
        'heading_changed' AS metric,
        cast(headings.value AS TEXT) AS metric_value
    FROM successful_change_prepares AS events
    JOIN json_each(
        events.details, '$.change_diff.headings_changed'
    ) AS headings
    WHERE json_type(events.details, '$.change_diff') = 'object'
),
all_metrics AS (
    SELECT * FROM status_metrics
    UNION ALL
    SELECT * FROM unavailable_reason_metrics
    UNION ALL
    SELECT * FROM size_metrics
    UNION ALL
    SELECT * FROM heading_metrics
)
SELECT
    change_level,
    metric,
    metric_value,
    count(*) AS event_count
FROM all_metrics
GROUP BY change_level, metric, metric_value
ORDER BY change_level, metric, metric_value;
-- end report

-- report: advisory_bypasses
SELECT
    coalesce(a.task_gid, '<pending-create>') AS task_gid,
    coalesce(a.actor_agent, '<unknown>') AS actor_agent,
    coalesce(json_extract(a.details, '$.command'), '<unknown>') AS command,
    coalesce(json_extract(a.details, '$.resolution'), '<unknown>') AS resolution,
    count(*) AS bypass_count,
    min(a.created_at) AS first_seen_at,
    max(a.created_at) AS last_seen_at
FROM audit_events AS a
WHERE a.event_type = 'generic_note_bypass'
GROUP BY
    coalesce(a.task_gid, '<pending-create>'),
    coalesce(a.actor_agent, '<unknown>'),
    coalesce(json_extract(a.details, '$.command'), '<unknown>'),
    coalesce(json_extract(a.details, '$.resolution'), '<unknown>')
ORDER BY bypass_count DESC, task_gid, actor_agent, command, resolution;
-- end report
