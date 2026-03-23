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

-- Row Level Security: public read, public insert (no auth required)
ALTER TABLE comments ENABLE ROW LEVEL SECURITY;

CREATE POLICY "Public read comments"
  ON comments FOR SELECT USING (true);

CREATE POLICY "Public insert comments"
  ON comments FOR INSERT WITH CHECK (
    length(content) BETWEEN 3 AND 500
    AND side IN ('YES', 'NO', 'NEUTRAL')
  );
