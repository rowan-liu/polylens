-- PolyLens — Supabase schema
-- Run this in: Supabase Dashboard → SQL Editor → New Query

CREATE TABLE IF NOT EXISTS comments (
  id            uuid        DEFAULT gen_random_uuid() PRIMARY KEY,
  market_id     text        NOT NULL,
  market_question text      DEFAULT '',
  author        text        DEFAULT 'Anonymous',
  side          text        NOT NULL CHECK (side IN ('YES', 'NO', 'NEUTRAL')),
  content       text        NOT NULL CHECK (length(content) BETWEEN 3 AND 500),
  created_at    timestamptz DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_comments_market_id ON comments(market_id);
CREATE INDEX IF NOT EXISTS idx_comments_created_at ON comments(created_at DESC);
ALTER TABLE comments ENABLE ROW LEVEL SECURITY;
CREATE POLICY "Public read comments"
  ON comments FOR SELECT USING (true);
CREATE POLICY "Public insert comments"
  ON comments FOR INSERT WITH CHECK (
    length(content) BETWEEN 3 AND 500
    AND side IN ('YES', 'NO', 'NEUTRAL')
  );

-- market_appearances: deduplicate repeated markets across runs
CREATE TABLE IF NOT EXISTS market_appearances (
  id           uuid        DEFAULT gen_random_uuid() PRIMARY KEY,
  market_id    text        NOT NULL,
  question     text,
  probability  numeric,
  change_24h   numeric,
  volume_24h   numeric,
  category     text,
  run_date     date        NOT NULL DEFAULT CURRENT_DATE,
  generated_at timestamptz DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_ma_market_id ON market_appearances(market_id);
CREATE INDEX IF NOT EXISTS idx_ma_run_date  ON market_appearances(run_date DESC);
ALTER TABLE market_appearances ENABLE ROW LEVEL SECURITY;
CREATE POLICY "Public read appearances" ON market_appearances FOR SELECT USING (true);

-- insight_cache: reuse AI insight if market barely moved
CREATE TABLE IF NOT EXISTS insight_cache (
  market_id   text        PRIMARY KEY,
  question    text,
  probability numeric,
  change_24h  numeric,
  insight     jsonb       NOT NULL,
  news        jsonb,
  cached_at   timestamptz DEFAULT now()
);
ALTER TABLE insight_cache ENABLE ROW LEVEL SECURITY;
CREATE POLICY "Public read cache" ON insight_cache FOR SELECT USING (true);
