CREATE TABLE IF NOT EXISTS firms_observation_batches (
  observation_id TEXT ENCODING DICT(32),
  source TEXT ENCODING DICT(8),
  acquired_at TIMESTAMP,
  latitude DOUBLE,
  longitude DOUBLE,
  location GEOMETRY(POINT, 4326),
  satellite TEXT ENCODING DICT(8),
  confidence TEXT ENCODING DICT(8),
  frp DOUBLE,
  bright_ti4 DOUBLE,
  bright_ti5 DOUBLE,
  scan DOUBLE,
  track DOUBLE,
  daynight TEXT ENCODING DICT(8),
  product_version TEXT ENCODING DICT(16),
  ingested_at TIMESTAMP,
  raw_object_key TEXT ENCODING DICT(32),
  batch_id TEXT ENCODING DICT(32)
);
CREATE TABLE IF NOT EXISTS firms_published_windows (
  source TEXT ENCODING DICT(8),
  window_day DATE,
  batch_id TEXT ENCODING DICT(32),
  publication_order BIGINT
);
CREATE VIEW firms_observations AS
WITH latest AS (
  SELECT source, window_day, MAX(publication_order) AS publication_order
  FROM firms_published_windows GROUP BY source, window_day
)
SELECT o.observation_id, o.source, o.acquired_at, o.latitude, o.longitude,
       o.location, o.satellite, o.confidence, o.frp, o.bright_ti4, o.bright_ti5,
       o.scan, o.track, o.daynight, o.product_version, o.ingested_at,
       o.raw_object_key
FROM firms_observation_batches o
JOIN firms_published_windows w ON o.source=w.source
             AND CAST(o.acquired_at AS DATE)=w.window_day AND o.batch_id=w.batch_id
JOIN latest p ON w.source=p.source AND w.window_day=p.window_day
             AND w.publication_order=p.publication_order;
CREATE TABLE IF NOT EXISTS firms_collection_runs (
  run_id TEXT ENCODING DICT(32),
  source TEXT ENCODING DICT(8),
  started_at TIMESTAMP,
  completed_at TIMESTAMP,
  status TEXT ENCODING DICT(8),
  received_rows BIGINT,
  latest_observation_at TIMESTAMP,
  raw_object_key TEXT ENCODING DICT(32)
);
