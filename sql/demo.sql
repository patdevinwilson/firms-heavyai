-- Detections are observed pixels, not distinct fires or verified incidents.
SELECT satellite, confidence, COUNT(*) AS detections,
       MAX(acquired_at) AS latest_observation
FROM firms_observations
WHERE acquired_at >= DATEADD('hour', -24, CURRENT_TIMESTAMP)
GROUP BY satellite, confidence;

-- UTC daily detection counts; satellite coverage is a confounder.
SELECT DATE_TRUNC('day', acquired_at) AS day_utc, satellite,
       COUNT(*) AS detections
FROM firms_observations
WHERE acquired_at >= DATEADD('day', -30, CURRENT_TIMESTAMP)
GROUP BY day_utc, satellite
ORDER BY day_utc;

-- Collection freshness is separate from observation freshness.
SELECT source, MAX(completed_at) AS last_success,
       MAX(latest_observation_at) AS latest_observation
FROM firms_collection_runs
WHERE status = 'success'
GROUP BY source;
