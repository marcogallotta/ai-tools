-- Dish Steps 1–10 operational reports.
-- Each block between -- report: and -- end report is a standalone read-only
-- SQLite query against the current local dish database.

-- report: compatibility_failures
SELECT
    event_type,
    coalesce(result_code, json_extract(details, '$.code')) AS result_code,
    json_extract(details, '$.errors[0].rule') AS first_rule,
    count(*) AS event_count,
    min(created_at) AS first_seen_at,
    max(created_at) AS last_seen_at
FROM audit_events
WHERE coalesce(result_code, json_extract(details, '$.code')) IN
      ('INTERNAL_ERROR','VALIDATION_FAILED')
  AND (
      details LIKE '%compatib%'
      OR details LIKE '%protocol_version%'
      OR details LIKE '%schema_version%'
      OR details LIKE '%DISH_VERSION%'
  )
GROUP BY event_type, result_code, first_rule
ORDER BY event_count DESC, event_type;
-- end report

-- report: schema_migrations_and_failures
WITH migration_commands AS (
    SELECT
        created_at,
        task_gid,
        coalesce(result_code, json_extract(details, '$.code')) AS result_code,
        coalesce(result_ok, json_extract(details, '$.ok')) AS result_ok,
        json_extract(details, '$.errors[0].rule') AS first_rule
    FROM audit_events
    WHERE event_type = 'dish-admin.migrate'
)
SELECT
    coalesce(result_code, '<unknown>') AS result_code,
    coalesce(first_rule, '<none>') AS first_rule,
    count(*) AS attempts,
    count(DISTINCT task_gid) AS tasks,
    min(created_at) AS first_seen_at,
    max(created_at) AS last_seen_at
FROM migration_commands
GROUP BY result_code, first_rule
ORDER BY attempts DESC, result_code, first_rule;
-- end report

-- report: drift_and_stale_baselines
SELECT
    event_type,
    coalesce(json_extract(details, '$.errors[0].rule'), '<none>') AS rule,
    CASE
        WHEN operation_id IS NULL THEN 'no_open_operation_recorded'
        ELSE 'operation_recorded'
    END AS operation_context,
    count(*) AS event_count,
    count(DISTINCT task_gid) AS task_count
FROM audit_events
WHERE details LIKE '%drift%'
   OR details LIKE '%stale%'
   OR json_extract(details, '$.errors[0].rule') LIKE '%identity%'
GROUP BY event_type, rule, operation_context
ORDER BY event_count DESC, event_type, rule;
-- end report

-- report: write_outcomes_and_uncertain_recovery
WITH attempts AS (
    SELECT
        w.outcome,
        w.operation_id,
        o.task_gid,
        w.started_at,
        w.finished_at
    FROM write_attempts AS w
    JOIN operations AS o USING (operation_id)
), recoveries AS (
    SELECT operation_id, count(*) AS recovery_events
    FROM audit_events
    WHERE event_type = 'dish-admin.recover'
    GROUP BY operation_id
)
SELECT
    a.outcome,
    count(*) AS attempts,
    count(DISTINCT a.task_gid) AS tasks,
    sum(CASE WHEN coalesce(r.recovery_events, 0) > 0 THEN 1 ELSE 0 END)
        AS attempts_with_recovery_event
FROM attempts AS a
LEFT JOIN recoveries AS r USING (operation_id)
GROUP BY a.outcome
ORDER BY attempts DESC, a.outcome;
-- end report

-- report: verification_cycles
SELECT
    coalesce(c.correction_class, '<none>') AS correction_class,
    coalesce(c.route, '<none>') AS route,
    coalesce(c.outcome, '<open>') AS outcome,
    count(*) AS cycles,
    count(DISTINCT c.task_gid) AS tasks,
    count(DISTINCT c.verifier_agent) AS verifier_agents
FROM verification_cycles AS c
GROUP BY correction_class, route, outcome
ORDER BY cycles DESC, correction_class, route, outcome;
-- end report

-- report: verification_routes
SELECT
    CASE
        WHEN route = 'evidence' THEN 'Evidence'
        WHEN route = 'human_review' THEN 'Human'
        WHEN correction_class = 'small' THEN 'Small'
        WHEN correction_class = 'large' THEN 'Large'
        ELSE 'unclassified'
    END AS verification_route,
    count(*) AS cycles,
    count(DISTINCT task_gid) AS tasks
FROM verification_cycles
GROUP BY verification_route
ORDER BY cycles DESC, verification_route;
-- end report

-- report: post_signoff_invalidations
SELECT
    a.event_type,
    coalesce(json_extract(a.details, '$.errors[0].rule'), '<none>') AS rule,
    count(*) AS events,
    count(DISTINCT a.task_gid) AS tasks
FROM audit_events AS a
JOIN operations AS o ON o.operation_id = a.operation_id
WHERE o.signoff_completed_at IS NOT NULL
  AND (
      a.details LIKE '%signoff%'
      OR a.details LIKE '%material%'
      OR a.details LIKE '%identity%'
  )
  AND coalesce(a.result_ok, json_extract(a.details, '$.ok'), 0) = 0
GROUP BY a.event_type, rule
ORDER BY events DESC, a.event_type, rule;
-- end report

-- report: signoff_vs_movement
SELECT
    CASE WHEN signoff_completed_at IS NULL THEN 0 ELSE 1 END AS signoff_complete,
    CASE WHEN movement_completed_at IS NULL THEN 0 ELSE 1 END AS movement_complete,
    status,
    count(*) AS operations,
    count(DISTINCT task_gid) AS tasks
FROM operations
GROUP BY signoff_complete, movement_complete, status
ORDER BY signoff_complete DESC, movement_complete DESC, status;
-- end report

-- report: tool_protocol_disagreements
SELECT
    event_type,
    coalesce(result_code, json_extract(details, '$.code')) AS result_code,
    coalesce(json_extract(details, '$.errors[0].rule'), '<none>') AS rule,
    count(*) AS events,
    count(DISTINCT task_gid) AS tasks,
    max(created_at) AS latest_at
FROM audit_events
WHERE details LIKE '%protocol%disagreement%'
   OR details LIKE '%conformance%defect%'
   OR json_extract(details, '$.errors[0].rule') LIKE '%protocol%'
GROUP BY event_type, result_code, rule
ORDER BY events DESC, latest_at DESC;
-- end report

-- report: movement_outcomes_by_purpose
SELECT
    m.purpose,
    m.outcome,
    count(*) AS attempts,
    count(DISTINCT o.task_gid) AS tasks,
    sum(CASE WHEN m.finished_at IS NULL THEN 1 ELSE 0 END) AS unfinished_attempts,
    sum(CASE WHEN m.purpose = 'destination_submission'
              AND o.movement_completed_at IS NOT NULL THEN 1 ELSE 0 END)
        AS final_submission_movements
FROM movement_attempts AS m
JOIN operations AS o USING (operation_id)
GROUP BY m.purpose, m.outcome
ORDER BY m.purpose, attempts DESC, m.outcome;
-- end report

-- report: recovery_reconciliations
SELECT
    event_type,
    json_extract(details, '$.purpose') AS purpose,
    json_extract(details, '$.outcome') AS reconciled_outcome,
    count(*) AS reconciliations,
    count(DISTINCT task_gid) AS tasks,
    min(created_at) AS first_seen_at,
    max(created_at) AS last_seen_at
FROM audit_events
WHERE event_type IN ('write_attempt.reconciled', 'movement_attempt.reconciled')
GROUP BY event_type, purpose, reconciled_outcome
ORDER BY event_type, purpose, reconciled_outcome;
-- end report

-- report: invalid_final_movement_semantics
SELECT
    o.operation_id,
    o.task_gid,
    o.status,
    o.movement_completed_at,
    max(CASE WHEN m.purpose = 'destination_submission'
              AND m.outcome = 'confirmed' THEN 1 ELSE 0 END)
        AS has_confirmed_destination_submission
FROM operations AS o
LEFT JOIN movement_attempts AS m USING (operation_id)
WHERE o.movement_completed_at IS NOT NULL
GROUP BY o.operation_id, o.task_gid, o.status, o.movement_completed_at
HAVING has_confirmed_destination_submission = 0
ORDER BY o.movement_completed_at DESC;
-- end report
