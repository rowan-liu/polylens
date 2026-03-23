// Vercel Edge Function — comment CRUD via Supabase REST API
export const config = { runtime: "edge" };

const HEADERS = {
  "Content-Type": "application/json",
  "Access-Control-Allow-Origin": "*",
  "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
  "Access-Control-Allow-Headers": "Content-Type",
};

export default async function handler(request) {
  if (request.method === "OPTIONS") return new Response(null, { status: 204, headers: HEADERS });

  const SB_URL = process.env.SUPABASE_URL;
  const SB_KEY = process.env.SUPABASE_ANON_KEY;

  if (!SB_URL || !SB_KEY) {
    return new Response(JSON.stringify({ error: "Supabase not configured" }), { status: 503, headers: HEADERS });
  }

  const sbHeaders = {
    apikey: SB_KEY,
    "Content-Type": "application/json",
  };

  const url = new URL(request.url);

  // GET /api/comment?market_id=xxx — fetch comments for a market
  if (request.method === "GET") {
    const marketId = url.searchParams.get("market_id");
    if (!marketId) return new Response(JSON.stringify({ error: "market_id required" }), { status: 400, headers: HEADERS });

    const res = await fetch(
      `${SB_URL}/rest/v1/comments?market_id=eq.${encodeURIComponent(marketId)}&order=created_at.asc&select=id,author,side,content,created_at`,
      { headers: sbHeaders }
    );
    const data = await res.json();
    return new Response(JSON.stringify(data), { status: 200, headers: HEADERS });
  }

  // POST /api/comment — add a comment
  if (request.method === "POST") {
    let body;
    try { body = await request.json(); } catch {
      return new Response(JSON.stringify({ error: "Invalid JSON" }), { status: 400, headers: HEADERS });
    }

    const { market_id, market_question, author, side, content } = body;
    if (!market_id || !side || !content?.trim()) {
      return new Response(JSON.stringify({ error: "market_id, side, content required" }), { status: 400, headers: HEADERS });
    }
    if (!["YES", "NO", "NEUTRAL"].includes(side)) {
      return new Response(JSON.stringify({ error: "side must be YES / NO / NEUTRAL" }), { status: 400, headers: HEADERS });
    }
    const text = content.trim().slice(0, 500);
    if (text.length < 3) {
      return new Response(JSON.stringify({ error: "Comment too short" }), { status: 400, headers: HEADERS });
    }

    const res = await fetch(`${SB_URL}/rest/v1/comments`, {
      method: "POST",
      headers: { ...sbHeaders, Prefer: "return=representation" },
      body: JSON.stringify({
        market_id,
        market_question: (market_question || "").slice(0, 300),
        author: (author || "").trim().slice(0, 50) || "Anonymous",
        side,
        content: text,
      }),
    });

    if (!res.ok) {
      const err = await res.text();
      console.error("Supabase error:", err);
      return new Response(JSON.stringify({ error: "Failed to save comment" }), { status: 500, headers: HEADERS });
    }
    const data = await res.json();
    return new Response(JSON.stringify({ success: true, comment: data[0] }), { status: 201, headers: HEADERS });
  }

  return new Response(JSON.stringify({ error: "Method not allowed" }), { status: 405, headers: HEADERS });
}
