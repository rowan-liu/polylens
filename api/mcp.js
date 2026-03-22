// Vercel Edge Function — MCP (Model Context Protocol) Streamable HTTP endpoint
// Tools: get_insights, get_market_summary
export const config = { runtime: "edge" };

const SITE_URL = "https://www.hika.fyi";

const CORS = {
  "Access-Control-Allow-Origin": "*",
  "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
  "Access-Control-Allow-Headers": "Content-Type, Authorization, mcp-session-id",
};

const TOOLS = [
  {
    name: "get_insights",
    description:
      "Get the latest AI-analyzed prediction market insights from PolyLens. Each insight includes a market question, current YES probability, 24h price change, AI-generated summary, key drivers, and source links.",
    inputSchema: {
      type: "object",
      properties: {
        category: {
          type: "string",
          enum: ["all", "politics", "ai_tech", "economy", "business", "world", "sports", "crypto", "other"],
          description: "Filter by category. Defaults to 'all'.",
        },
        limit: {
          type: "number",
          description: "Max number of insights to return (1–20). Defaults to 5.",
        },
        lang: {
          type: "string",
          enum: ["en", "zh"],
          description: "Language for insight text. Defaults to 'en'.",
        },
      },
    },
  },
  {
    name: "get_market_summary",
    description:
      "Get a brief text summary of current Polymarket prediction market conditions — top story, category distribution, and update timestamp.",
    inputSchema: {
      type: "object",
      properties: {},
    },
  },
];

async function getData() {
  const res = await fetch(`${SITE_URL}/data.json`, { cache: "no-store" });
  if (!res.ok) throw new Error("Failed to fetch market data");
  return res.json();
}

async function callTool(name, args = {}) {
  const data = await getData();
  const topics = data.topics || [];
  const generatedAt = data.generated_at_readable || "";

  if (name === "get_insights") {
    const { category = "all", limit = 5, lang = "en" } = args;
    const safeLimit = Math.min(Math.max(1, Number(limit) || 5), 20);
    const filtered =
      category === "all" ? topics : topics.filter((t) => t.insight?.category === category);

    return filtered.slice(0, safeLimit).map((t) => {
      const m = t.market;
      const ins = t.insight?.[lang] || t.insight?.en || {};
      return {
        title: ins.title,
        summary: ins.summary,
        key_drivers: ins.drivers,
        why_matters: ins.why_matters,
        category: t.insight?.category,
        probability_yes: m.probability,
        change_24h: m.change_24h,
        volume_24h: m.volume_24h,
        market_question: m.question,
        polymarket_url: m.url,
        news_sources: (t.news || []).slice(0, 3).map((n) => ({ title: n.title, url: n.url })),
      };
    });
  }

  if (name === "get_market_summary") {
    if (!topics.length) return "No market data available yet.";
    const cats = {};
    topics.forEach((t) => {
      const c = t.insight?.category || "other";
      cats[c] = (cats[c] || 0) + 1;
    });
    const catLine = Object.entries(cats)
      .sort((a, b) => b[1] - a[1])
      .map(([k, v]) => `${k}(${v})`)
      .join(", ");

    const top = topics[0];
    const ins = top?.insight?.en || {};
    const m = top?.market || {};
    const pct = (m.probability * 100).toFixed(0);
    const chg = `${m.change_24h >= 0 ? "+" : ""}${(m.change_24h * 100).toFixed(1)}%`;

    return (
      `PolyLens Market Summary — ${generatedAt}\n\n` +
      `Top story: "${ins.title}"\n` +
      `Probability: ${pct}%  (${chg} today)\n\n` +
      `${ins.summary}\n\n` +
      `Why it matters: ${ins.why_matters}\n\n` +
      `Coverage: ${catLine}\n` +
      `Total insights: ${topics.length}\n\n` +
      `Full analysis: ${SITE_URL}`
    );
  }

  throw new Error(`Unknown tool: ${name}`);
}

function rpcError(code, message, id) {
  return new Response(
    JSON.stringify({ jsonrpc: "2.0", id: id ?? null, error: { code, message } }),
    { status: 400, headers: { ...CORS, "Content-Type": "application/json" } }
  );
}

function rpcOk(id, result) {
  return new Response(JSON.stringify({ jsonrpc: "2.0", id, result }), {
    headers: { ...CORS, "Content-Type": "application/json" },
  });
}

export default async function handler(request) {
  if (request.method === "OPTIONS") return new Response(null, { headers: CORS });

  // GET — capability discovery
  if (request.method === "GET") {
    return new Response(
      JSON.stringify({
        name: "polylens",
        version: "1.0.0",
        description:
          "PolyLens — AI-powered prediction market intelligence. Real-time analysis of trending Polymarket markets.",
        tools: TOOLS,
        data_url: `${SITE_URL}/data.json`,
      }),
      { headers: { ...CORS, "Content-Type": "application/json" } }
    );
  }

  if (request.method !== "POST") {
    return new Response("Method not allowed", { status: 405, headers: CORS });
  }

  let body;
  try {
    body = await request.json();
  } catch {
    return rpcError(-32700, "Parse error", null);
  }

  const { jsonrpc, id, method, params } = body;
  if (jsonrpc !== "2.0") return rpcError(-32600, "Invalid Request", id);

  try {
    if (method === "initialize") {
      return rpcOk(id, {
        protocolVersion: "2024-11-05",
        capabilities: { tools: {} },
        serverInfo: { name: "polylens", version: "1.0.0" },
      });
    }

    if (method === "notifications/initialized") {
      return new Response(null, { status: 204, headers: CORS });
    }

    if (method === "tools/list") {
      return rpcOk(id, { tools: TOOLS });
    }

    if (method === "tools/call") {
      const { name, arguments: args } = params || {};
      if (!name) return rpcError(-32602, "Missing tool name", id);
      const result = await callTool(name, args);
      return rpcOk(id, {
        content: [
          {
            type: "text",
            text: typeof result === "string" ? result : JSON.stringify(result, null, 2),
          },
        ],
      });
    }

    return rpcError(-32601, "Method not found", id);
  } catch (err) {
    return rpcError(-32000, err.message, id);
  }
}
