// Vercel Edge Function — create WeChat Official Account draft from archive snapshot
export const config = { runtime: "edge" };

const SITE_URL = "https://www.hika.fyi";

const CORS = {
  "Access-Control-Allow-Origin": "*",
  "Access-Control-Allow-Methods": "POST, OPTIONS",
  "Access-Control-Allow-Headers": "Content-Type",
};

function json(data, status = 200) {
  return new Response(JSON.stringify(data), {
    status,
    headers: { ...CORS, "Content-Type": "application/json" },
  });
}

async function getAccessToken(appid, secret) {
  const url = `https://api.weixin.qq.com/cgi-bin/token?grant_type=client_credential&appid=${appid}&secret=${secret}`;
  const res = await fetch(url);
  const data = await res.json();
  if (data.errcode) throw new Error(`WeChat token error ${data.errcode}: ${data.errmsg}`);
  return data.access_token;
}

function buildWechatHtml(data) {
  const topics = data.topics || [];
  const dateStr = data.generated_at_readable || "";

  const cards = topics.map((item, idx) => {
    const m = item.market;
    const ins = item.insight;
    const zh = ins?.zh || ins?.en || {};
    const en = ins?.en || {};
    const isUp = m.change_24h >= 0;
    const prob = (m.probability * 100).toFixed(0);
    const sign = isUp ? "+" : "";
    const chgPct = (m.change_24h * 100).toFixed(1);
    const probColor = isUp ? "#22c55e" : "#ef4444";
    const chgBg = isUp ? "rgba(34,197,94,0.15)" : "rgba(239,68,68,0.15)";
    const arrow = isUp ? "▲" : "▼";

    const drivers = (zh.drivers || []).map(d =>
      `<p style="margin:4px 0;font-size:13px;color:#555;padding-left:12px;position:relative;">
        <span style="position:absolute;left:0;color:#7c6af7;">→</span>${d}
      </p>`
    ).join("");

    const why = zh.why_matters
      ? `<p style="margin:10px 0 0;font-size:12px;color:#777;line-height:1.6;padding:8px 12px;background:#f5f3ff;border-left:3px solid #7c6af7;border-radius:0 6px 6px 0;">${zh.why_matters}</p>`
      : "";

    const enSummary = en.summary
      ? `<p style="margin:8px 0 0;font-size:12px;color:#999;line-height:1.6;font-style:italic;">${en.summary}</p>`
      : "";

    const sources = (item.news || []).slice(0, 3).map((n, i) =>
      `<a href="${n.url}" style="color:#7c6af7;text-decoration:none;font-size:11px;">[${i + 1}]</a>`
    ).join(" ");

    return `
<section style="background:#fff;border:1px solid #ebebf0;border-radius:12px;padding:18px 20px;margin-bottom:14px;box-shadow:0 1px 4px rgba(0,0,0,0.06);">
  <section style="display:flex;align-items:flex-start;justify-content:space-between;gap:12px;margin-bottom:6px;">
    <p style="font-size:15px;font-weight:600;color:#111;line-height:1.4;margin:0;flex:1;">${idx + 1}. ${zh.title || en.title || m.question}</p>
    <section style="text-align:right;flex-shrink:0;">
      <p style="font-size:20px;font-weight:700;color:${probColor};margin:0;font-family:monospace;">${prob}%</p>
      <p style="font-size:11px;font-weight:600;color:${probColor};background:${chgBg};padding:2px 7px;border-radius:4px;margin:3px 0 0;display:inline-block;">${arrow} ${sign}${chgPct}%</p>
    </section>
  </section>
  <p style="font-size:11px;color:#aaa;font-style:italic;margin:0 0 10px;">${m.question}</p>
  <p style="font-size:14px;color:#333;line-height:1.7;margin:0 0 8px;">${zh.summary || ""}</p>
  ${drivers ? `<section style="margin-top:6px;">${drivers}</section>` : ""}
  ${why}
  ${enSummary}
  <p style="margin:10px 0 0;font-size:11px;color:#bbb;">
    Vol 24h: $${Number(m.volume_24h).toLocaleString()} ·
    <a href="${m.url}" style="color:#7c6af7;text-decoration:none;">Polymarket ↗</a>
    ${sources ? ` · ${sources}` : ""}
  </p>
</section>`;
  }).join("\n");

  return `
<section style="background:#fafafa;padding:20px 16px;font-family:-apple-system,BlinkMacSystemFont,'PingFang SC','Helvetica Neue',sans-serif;">

  <!-- Header -->
  <section style="text-align:center;padding:24px 16px 20px;background:#fff;border-radius:14px;margin-bottom:20px;border:1px solid #ebebf0;">
    <p style="font-size:26px;font-weight:700;color:#111;margin:0 0 4px;">
      Poly<span style="color:#7c6af7;">Lens</span>
    </p>
    <p style="font-size:12px;color:#999;margin:0 0 10px;font-family:monospace;">${dateStr}</p>
    <p style="font-size:13px;color:#555;margin:0;line-height:1.6;">AI 驱动的预测市场洞察 · ${topics.length} 个市场实时分析</p>
    <p style="font-size:12px;color:#aaa;margin:8px 0 0;">数据来源 Polymarket · AI 分析 Gemini / GPT · 新闻 Tavily</p>
  </section>

  <!-- Cards -->
  ${cards}

  <!-- Footer -->
  <section style="text-align:center;padding:20px 16px;background:#fff;border-radius:12px;border:1px solid #ebebf0;margin-top:6px;">
    <p style="font-size:13px;color:#666;margin:0 0 14px;line-height:1.6;">市场决定重要性，AI 解释原因。<br>每 8 小时更新，免费订阅。</p>
    <a href="${SITE_URL}" style="display:inline-block;background:#7c6af7;color:#fff;padding:11px 28px;border-radius:8px;font-size:14px;font-weight:600;text-decoration:none;">查看完整分析 →</a>
  </section>

</section>`;
}

export default async function handler(request) {
  if (request.method === "OPTIONS") return new Response(null, { headers: CORS });
  if (request.method !== "POST") return json({ error: "Method not allowed" }, 405);

  const APPID = process.env.WECHAT_APPID;
  const SECRET = process.env.WECHAT_SECRET;
  const THUMB_MEDIA_ID = process.env.WECHAT_THUMB_MEDIA_ID;

  if (!APPID || !SECRET) return json({ error: "WECHAT_APPID / WECHAT_SECRET not configured" }, 500);
  if (!THUMB_MEDIA_ID) return json({ error: "WECHAT_THUMB_MEDIA_ID not set. Call /api/wechat-setup first." }, 500);

  let slug;
  try {
    ({ slug } = await request.json());
  } catch {
    return json({ error: "Invalid JSON" }, 400);
  }
  if (!slug) return json({ error: "slug required" }, 400);

  try {
    // 1. Get access token
    const token = await getAccessToken(APPID, SECRET);

    // 2. Fetch archive snapshot
    const dataRes = await fetch(`${SITE_URL}/archive/${slug}.json`);
    if (!dataRes.ok) return json({ error: "Snapshot not found: " + slug }, 404);
    const data = await dataRes.json();

    // 3. Build WeChat-formatted HTML
    const content = buildWechatHtml(data);

    // 4. Create draft
    const title = `PolyLens · ${data.generated_at_readable}`;
    const digest = `AI 驱动的预测市场洞察 · ${data.topics.length} 个市场分析`;

    const draftRes = await fetch(
      `https://api.weixin.qq.com/cgi-bin/draft/add?access_token=${token}`,
      {
        method: "POST",
        headers: { "Content-Type": "application/json; charset=utf-8" },
        body: JSON.stringify({
          articles: [{
            title,
            author: "PolyLens",
            digest,
            content,
            content_source_url: SITE_URL,
            thumb_media_id: THUMB_MEDIA_ID,
            need_open_comment: 1,
            only_fans_can_comment: 0,
          }],
        }),
      }
    );

    const result = await draftRes.json();
    if (result.errcode && result.errcode !== 0) {
      return json({ error: `WeChat error ${result.errcode}: ${result.errmsg}`, raw: result }, 500);
    }

    return json({ success: true, media_id: result.media_id, title });
  } catch (err) {
    return json({ error: err.message }, 500);
  }
}
