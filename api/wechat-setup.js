// Vercel Edge Function — one-time setup: upload PolyLens cover image to WeChat permanent materials
// Call once: POST /api/wechat-setup  →  returns { thumb_media_id }
// Then set WECHAT_THUMB_MEDIA_ID env var with the returned value.
export const config = { runtime: "edge" };

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

export default async function handler(request) {
  if (request.method === "OPTIONS") return new Response(null, { headers: CORS });
  if (request.method !== "POST") return json({ error: "POST only" }, 405);

  const APPID = process.env.WECHAT_APPID;
  const SECRET = process.env.WECHAT_SECRET;
  if (!APPID || !SECRET) return json({ error: "WECHAT_APPID / WECHAT_SECRET not configured" }, 500);

  try {
    // 1. Get access token
    const tokenRes = await fetch(
      `https://api.weixin.qq.com/cgi-bin/token?grant_type=client_credential&appid=${APPID}&secret=${SECRET}`
    );
    const tokenData = await tokenRes.json();
    if (tokenData.errcode) throw new Error(`Token error ${tokenData.errcode}: ${tokenData.errmsg}`);
    const token = tokenData.access_token;

    // 2. Fetch cover image from our own static output
    const imgRes = await fetch("https://www.hika.fyi/cover.jpg");
    if (!imgRes.ok) throw new Error("cover.jpg not found — make sure it is deployed");
    const imgBlob = await imgRes.blob();

    // 3. Upload as permanent thumb material
    const form = new FormData();
    form.append("media", imgBlob, "cover.jpg");

    const uploadRes = await fetch(
      `https://api.weixin.qq.com/cgi-bin/material/add_material?access_token=${token}&type=thumb`,
      { method: "POST", body: form }
    );
    const uploadData = await uploadRes.json();
    if (uploadData.errcode && uploadData.errcode !== 0) {
      throw new Error(`Upload error ${uploadData.errcode}: ${uploadData.errmsg}`);
    }

    return json({
      success: true,
      thumb_media_id: uploadData.media_id,
      message: "Set WECHAT_THUMB_MEDIA_ID=" + uploadData.media_id + " in Vercel + GitHub secrets",
    });
  } catch (err) {
    return json({ error: err.message }, 500);
  }
}
