#!/usr/bin/env python3
"""
PolyLens WeChat Tool — run from your local machine.

Usage:
  python scripts/wechat.py setup          # Upload cover image, get thumb_media_id (run once)
  python scripts/wechat.py draft          # Create draft from today's archive
  python scripts/wechat.py draft 2026-03-21   # Create draft from specific date

Prerequisites:
  1. WeChat 公众号后台 → 开发 → 基本配置 → IP白名单 → 添加你的 IP（运行 `curl -4 ifconfig.me`）
  2. .env 文件里有 WECHAT_APPID, WECHAT_SECRET (setup 完成后加 WECHAT_THUMB_MEDIA_ID)
"""

import json
import os
import sys
import requests
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

APPID = os.environ.get("WECHAT_APPID", "")
SECRET = os.environ.get("WECHAT_SECRET", "")
THUMB_MEDIA_ID = os.environ.get("WECHAT_THUMB_MEDIA_ID", "")
SITE_URL = os.environ.get("SITE_URL", "https://www.hika.fyi")
ARCHIVE_DIR = Path(__file__).parent.parent / "output" / "archive"

WX_API = "https://api.weixin.qq.com/cgi-bin"


def get_token() -> str:
    r = requests.get(f"{WX_API}/token",
                     params={"grant_type": "client_credential", "appid": APPID, "secret": SECRET},
                     timeout=10)
    d = r.json()
    if "errcode" in d:
        raise RuntimeError(f"Token error {d['errcode']}: {d.get('errmsg')}")
    return d["access_token"]


def cmd_setup():
    """Upload cover image once -> get thumb_media_id."""
    cover = Path(__file__).parent.parent / "output" / "cover.jpg"
    if not cover.exists():
        print("  output/cover.jpg not found. Run pipeline once first.")
        sys.exit(1)

    token = get_token()
    with open(cover, "rb") as f:
        r = requests.post(
            f"{WX_API}/material/add_material",
            params={"access_token": token, "type": "thumb"},
            files={"media": ("cover.jpg", f, "image/jpeg")},
            timeout=20,
        )
    d = r.json()
    if d.get("errcode") and d["errcode"] != 0:
        print(f"Upload failed {d['errcode']}: {d.get('errmsg')}")
        sys.exit(1)

    media_id = d["media_id"]
    print(f"\nCover uploaded! thumb_media_id = {media_id}")
    print(f"\nAdd to .env:")
    print(f"  WECHAT_THUMB_MEDIA_ID={media_id}")
    print(f"\nAlso set in GitHub secrets + Vercel:")
    print(f"  gh secret set WECHAT_THUMB_MEDIA_ID --body \"{media_id}\"")
    print(f"  vercel env add WECHAT_THUMB_MEDIA_ID production <<< \"{media_id}\"")


def build_wechat_html(data: dict) -> str:
    """Build WeChat-compatible HTML with inline styles (light theme, mobile-first)."""
    topics = data.get("topics", [])
    date_str = data.get("generated_at_readable", "")

    cards = []
    for idx, item in enumerate(topics):
        m = item["market"]
        ins = item["insight"]
        zh = ins.get("zh") or ins.get("en") or {}
        en = ins.get("en") or {}
        is_up = m["change_24h"] >= 0
        prob = f"{m['probability']:.0%}"
        sign = "+" if is_up else ""
        chg = f"{m['change_24h']:.1%}"
        color = "#22c55e" if is_up else "#ef4444"
        chg_bg = "rgba(34,197,94,0.12)" if is_up else "rgba(239,68,68,0.12)"
        arrow = "\u25b2" if is_up else "\u25bc"

        drivers_html = "".join(
            f'<p style="margin:3px 0;font-size:13px;color:#555;padding-left:14px;position:relative;">'
            f'<span style="position:absolute;left:0;color:#7c6af7;">-></span>{d}</p>'
            for d in (zh.get("drivers") or [])
        )
        why = ""
        if zh.get("why_matters"):
            why = (f'<p style="margin:10px 0 0;font-size:12px;color:#666;line-height:1.65;'
                   f'padding:8px 12px;background:#f5f3ff;border-left:3px solid #7c6af7;'
                   f'border-radius:0 6px 6px 0;">{zh["why_matters"]}</p>')
        en_sum = ""
        if en.get("summary") and en.get("summary") != zh.get("summary"):
            en_sum = (f'<p style="margin:8px 0 0;font-size:12px;color:#aaa;'
                      f'font-style:italic;line-height:1.6;">{en["summary"]}</p>')

        news = item.get("news", [])[:3]
        sources = " \u00b7 ".join(
            f'<a href="{n["url"]}" style="color:#7c6af7;text-decoration:none;font-size:11px;">[{j+1}]</a>'
            for j, n in enumerate(news)
        )

        cards.append(f"""
<section style="background:#fff;border:1px solid #ebebf0;border-radius:12px;padding:18px 20px;margin-bottom:14px;">
  <section style="display:flex;align-items:flex-start;justify-content:space-between;gap:10px;margin-bottom:6px;">
    <p style="font-size:15px;font-weight:600;color:#111;line-height:1.4;margin:0;flex:1;">{idx+1}. {zh.get("title") or en.get("title") or m["question"]}</p>
    <section style="text-align:right;flex-shrink:0;">
      <p style="font-size:20px;font-weight:700;color:{color};margin:0;font-family:monospace;">{prob}</p>
      <p style="font-size:11px;font-weight:600;color:{color};background:{chg_bg};padding:2px 7px;border-radius:4px;margin:3px 0 0;">{arrow} {sign}{chg}</p>
    </section>
  </section>
  <p style="font-size:11px;color:#bbb;font-style:italic;margin:0 0 10px;">{m["question"]}</p>
  <p style="font-size:14px;color:#333;line-height:1.7;margin:0 0 8px;">{zh.get("summary", "")}</p>
  {drivers_html}
  {why}
  {en_sum}
  <p style="margin:10px 0 0;font-size:11px;color:#ccc;font-family:monospace;">
    Vol 24h: ${m["volume_24h"]:,.0f}
    \u00b7 <a href="{m["url"]}" style="color:#7c6af7;text-decoration:none;">Polymarket \u2197</a>
    {(' \u00b7 ' + sources) if sources else ''}
  </p>
</section>""")

    cards_html = "\n".join(cards)
    return f"""
<section style="background:#f5f5f7;padding:16px;font-family:-apple-system,BlinkMacSystemFont,'PingFang SC','Helvetica Neue',sans-serif;">

<section style="text-align:center;padding:24px 16px 20px;background:#fff;border-radius:14px;margin-bottom:18px;border:1px solid #ebebf0;">
  <p style="font-size:26px;font-weight:700;color:#111;margin:0 0 4px;">Poly<span style="color:#7c6af7;">Lens</span></p>
  <p style="font-size:12px;color:#aaa;margin:0 0 10px;font-family:monospace;">{date_str}</p>
  <p style="font-size:13px;color:#555;margin:0;line-height:1.6;">AI \u9a71\u52a8\u7684\u9884\u6d4b\u5e02\u573a\u6d1e\u5bdf \u00b7 {len(topics)} \u4e2a\u5e02\u573a\u5206\u6790</p>
  <p style="font-size:11px;color:#bbb;margin:8px 0 0;">Polymarket \u6570\u636e \u00b7 Gemini/GPT \u5206\u6790 \u00b7 Tavily \u65b0\u95fb</p>
</section>

{cards_html}

<section style="text-align:center;padding:20px;background:#fff;border-radius:12px;border:1px solid #ebebf0;margin-top:4px;">
  <p style="font-size:13px;color:#666;margin:0 0 14px;line-height:1.6;">\u5e02\u573a\u51b3\u5b9a\u91cd\u8981\u6027\uff0cAI \u89e3\u91ca\u539f\u56e0\u3002<br>\u6bcf 8 \u5c0f\u65f6\u66f4\u65b0 \u00b7 \u514d\u8d39\u8ba2\u9605\u90ae\u4ef6\u63d0\u9192</p>
  <a href="{SITE_URL}" style="display:inline-block;background:#7c6af7;color:#fff;padding:11px 28px;border-radius:8px;font-size:14px;font-weight:600;text-decoration:none;">\u67e5\u770b\u5b8c\u6574\u5206\u6790 \u2192</a>
</section>

</section>"""


def cmd_draft(slug: str = ""):
    if not THUMB_MEDIA_ID:
        print("WECHAT_THUMB_MEDIA_ID not set. Run: python scripts/wechat.py setup")
        sys.exit(1)

    if not slug:
        slug = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    json_path = ARCHIVE_DIR / f"{slug}.json"
    if not json_path.exists():
        fallback = Path(__file__).parent.parent / "output" / "data.json"
        if fallback.exists():
            print(f"  {json_path.name} not found, using output/data.json")
            data = json.loads(fallback.read_text())
        else:
            print(f"  {json_path} not found and no data.json fallback")
            sys.exit(1)
    else:
        data = json.loads(json_path.read_text())

    token = get_token()
    content = build_wechat_html(data)
    title = f"PolyLens \u00b7 {data['generated_at_readable']}"
    digest = f"AI \u9a71\u52a8\u7684\u9884\u6d4b\u5e02\u573a\u6d1e\u5bdf \u00b7 {len(data['topics'])} \u4e2a\u5e02\u573a\u5206\u6790"

    r = requests.post(
        f"{WX_API}/draft/add",
        params={"access_token": token},
        json={
            "articles": [{
                "title": title,
                "author": "PolyLens",
                "digest": digest,
                "content": content,
                "content_source_url": SITE_URL,
                "thumb_media_id": THUMB_MEDIA_ID,
                "need_open_comment": 1,
                "only_fans_can_comment": 0,
            }]
        },
        timeout=20,
    )
    d = r.json()
    if d.get("errcode") and d["errcode"] != 0:
        print(f"Draft error {d['errcode']}: {d.get('errmsg')}")
        print("Full response:", d)
        sys.exit(1)

    print(f"\n\u8349\u7a3f\u5df2\u4fdd\u5b58\u5230\u516c\u4f17\u53f7\uff01")
    print(f"  \u6807\u9898: {title}")
    print(f"  media_id: {d.get('media_id', '(see response)')}")
    print(f"\n\u767b\u5f55\u516c\u4f17\u53f7\u540e\u53f0 \u2192 \u8349\u7a3f\u7b8b \u2192 \u53d1\u5e03")


if __name__ == "__main__":
    if not APPID or not SECRET:
        print("WECHAT_APPID / WECHAT_SECRET not set in .env")
        sys.exit(1)

    cmd = sys.argv[1] if len(sys.argv) > 1 else "draft"
    if cmd == "setup":
        cmd_setup()
    elif cmd == "draft":
        slug_arg = sys.argv[2] if len(sys.argv) > 2 else ""
        cmd_draft(slug_arg)
    else:
        print(f"Unknown command: {cmd}. Use: setup | draft [YYYY-MM-DD]")
        sys.exit(1)
