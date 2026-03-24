// Vercel Edge Function — comment CRUD via Supabase REST API
export const config = { runtime: "edge" };

const HEADERS = {
  "Content-Type": "application/json",
  "Access-Control-Allow-Origin": "*",
  "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
  "Access-Control-Allow-Headers": "Content-Type",
};

// Rate limit: max 3 comments per IP per 5 minutes
const RATE_WINDOW_MS = 5 * 60 * 1000;
const RATE_MAX = 3;

async function hashIp(ip) {
  const data = new TextEncoder().encode(ip + "polylens-2025");
  const buf = await crypto.subtle.digest("SHA-256", data);
  return Array.from(new Uint8Array(buf)).map(b => b.toString(16).padStart(2, "0")).join("").slice(0, 16);
}

export default async function handler(request) {
  if (request.method === "OPTIONS") return new Response(null, { status: 204, headers: HEADERS });

  const SB_URL = process.env.SUPABASE_URL;
  const SB_KEY = process.env.SUPABASE_ANON_KEY;

  if (!SB_URL || !SB_KEY) {
    return new Response(JSON.stringify({ error: "Supabase not configured" }), { status: 503, headers: HEADERS });
  }

  const sbHeaders = { apikey: SB_KEY, "Content-Type": "application/json" };
  const url = new URL(request.url);

  // GET /api/comment?market_id=xxx
  if (request.method === "GET") {
    const marketId = url.searchParams.get("market_id");
    if (!marketId) return new Response(JSON.stringify({ error: "market_id required" }), { status: 400, headers: HEADERS });

    // Try fetching with likes column; fall back gracefully if it doesn't exist yet
    let res = await fetch(
      `${SB_URL}/rest/v1/comments?market_id=eq.${encodeURIComponent(marketId)}&order=likes.desc,created_at.asc&select=id,author,side,content,created_at,likes`,
      { headers: sbHeaders }
    );
    if (!res.ok) {
      const errText = await res.text();
      if (errText.includes("likes")) {
        res = await fetch(
          `${SB_URL}/rest/v1/comments?market_id=eq.${encodeURIComponent(marketId)}&order=created_at.asc&select=id,author,side,content,created_at`,
          { headers: sbHeaders }
        );
      }
    }
    return new Response(await res.text(), { status: 200, headers: HEADERS });
  }

  // PATCH /api/comment?id=xxx  — increment likes
  if (request.method === "PATCH") {
    const commentId = url.searchParams.get("id");
    if (!commentId) return new Response(JSON.stringify({ error: "id required" }), { status: 400, headers: HEADERS });

    // Fetch current likes
    const getRes = await fetch(
      `${SB_URL}/rest/v1/comments?id=eq.${encodeURIComponent(commentId)}&select=id,likes`,
      { headers: sbHeaders }
    );
    if (!getRes.ok) return new Response(JSON.stringify({ likes: 1 }), { status: 200, headers: HEADERS });
    const rows = await getRes.json().catch(() => []);
    if (!Array.isArray(rows) || !rows.length) return new Response(JSON.stringify({ error: "not found" }), { status: 404, headers: HEADERS });

    const newLikes = (rows[0].likes || 0) + 1;
    const patchRes = await fetch(`${SB_URL}/rest/v1/comments?id=eq.${encodeURIComponent(commentId)}`, {
      method: "PATCH",
      headers: { ...sbHeaders, Prefer: "return=representation" },
      body: JSON.stringify({ likes: newLikes }),
    });
    if (!patchRes.ok) {
      const errText = await patchRes.text();
      if (errText.includes("likes")) {
        // likes column not yet created — return optimistic value
        return new Response(JSON.stringify({ likes: null }), { status: 200, headers: HEADERS });
      }
      return new Response(JSON.stringify({ error: "Failed to update" }), { status: 500, headers: HEADERS });
    }
    return new Response(JSON.stringify({ likes: newLikes }), { status: 200, headers: HEADERS });
  }

  // POST /api/comment
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

    // ── Rate limiting ──────────────────────────────────────────────────────
    const rawIp = request.headers.get("x-forwarded-for")?.split(",")[0]?.trim()
               || request.headers.get("x-real-ip")
               || "unknown";
    const ipHash = await hashIp(rawIp);

    const since = new Date(Date.now() - RATE_WINDOW_MS).toISOString();
    const countRes = await fetch(
      `${SB_URL}/rest/v1/comments?ip_hash=eq.${ipHash}&created_at=gte.${since}&select=id`,
      { headers: sbHeaders }
    );
    if (countRes.ok) {
      const recent = await countRes.json();
      if (Array.isArray(recent) && recent.length >= RATE_MAX) {
        return new Response(
          JSON.stringify({ error: `Rate limit: max ${RATE_MAX} comments per 5 minutes` }),
          { status: 429, headers: HEADERS }
        );
      }
    }
    // ── End rate limiting ──────────────────────────────────────────────────

    const payload = {
      market_id,
      market_question: (market_question || "").slice(0, 300),
      author: (author || "").trim().slice(0, 50) || "Anonymous",
      side,
      content: text,
      ip_hash: ipHash,
    };

    let res = await fetch(`${SB_URL}/rest/v1/comments`, {
      method: "POST",
      headers: { ...sbHeaders, Prefer: "return=representation" },
      body: JSON.stringify(payload),
    });

    // ip_hash column may not exist yet — retry without it
    if (!res.ok) {
      const errText = await res.text();
      if (errText.includes("ip_hash")) {
        const { ip_hash: _dropped, ...payloadNoHash } = payload;
        res = await fetch(`${SB_URL}/rest/v1/comments`, {
          method: "POST",
          headers: { ...sbHeaders, Prefer: "return=representation" },
          body: JSON.stringify(payloadNoHash),
        });
      }
      if (!res.ok) {
        console.error("Supabase error:", await res.text());
        return new Response(JSON.stringify({ error: "Failed to save comment" }), { status: 500, headers: HEADERS });
      }
    }
    const data = await res.json();
    return new Response(JSON.stringify({ success: true, comment: data[0] }), { status: 201, headers: HEADERS });
  }

  return new Response(JSON.stringify({ error: "Method not allowed" }), { status: 405, headers: HEADERS });
}
