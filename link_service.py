import json
import os
import re
import aiohttp
from aiohttp import web

# رابط البوت الرئيسي على Render
MAIN_BOT_URL = os.getenv("MAIN_BOT_URL", "https://abood-trader.onrender.com").rstrip("/")
# مفتاح الأمان المشترك بين SnapDeploy و Render
LINK_WEBHOOK_SECRET = os.getenv("LINK_WEBHOOK_SECRET", "abood_secure_link_2026")


def parse_smart_input(raw_input: str) -> tuple[str, str, str]:
    """
    يحلل النص المدخل تلقائيًا ويستخرج: (demo_session, live_session, uid)
    """
    demo_s = ""
    live_s = ""
    uid = ""

    frames = re.findall(r'42\["auth"\s*,\s*\{.*?\}\]', raw_input or "")
    for fr in frames:
        try:
            parsed = json.loads(fr[2:])
            if isinstance(parsed, list) and len(parsed) >= 2 and isinstance(parsed[1], dict):
                obj = parsed[1]
                s = str(obj.get("session", "")).strip()
                u = str(obj.get("uid", "")).strip()
                if u:
                    uid = u
                if int(obj.get("isDemo", 1)) == 0:
                    live_s = s
                else:
                    demo_s = s
        except Exception:
            continue

    if demo_s or live_s:
        return demo_s, live_s, uid

    clean = (raw_input or "").strip()
    sess_m = re.search(r'["\']?session["\']?\s*[:=]\s*["\']([a-zA-Z0-9_-]{16,64})["\']', clean)
    uid_m = re.search(r'["\']?uid["\']?\s*[:=]\s*["\']?(\d{5,15})["\']?', clean)
    if sess_m:
        return sess_m.group(1), "", (uid_m.group(1) if uid_m else "")

    if re.fullmatch(r"[a-zA-Z0-9_-]{16,64}", clean):
        return clean, "", ""

    return "", "", ""


def render_connect_page(prefill_name: str = "") -> str:
    safe_name = prefill_name.replace('"', "").replace("<", "").replace(">", "")
    return f"""<!DOCTYPE html>
<html lang="ar" dir="rtl">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no">
<title>بوابة الربط السريع — عبود تريدينج</title>
<style>
  * {{ box-sizing: border-box; font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; }}
  body {{ margin: 0; padding: 16px; background: #0b1120; color: #f8fafc; }}
  .card {{ max-width: 450px; margin: 16px auto; background: #131c31; border: 1px solid #1e293b; border-radius: 18px; padding: 22px; box-shadow: 0 12px 32px rgba(0,0,0,0.45); }}
  h2 {{ margin: 0 0 6px; font-size: 20px; color: #10b981; text-align: center; }}
  p.sub {{ margin: 0 0 18px; font-size: 13px; color: #94a3b8; text-align: center; }}
  .field {{ margin-bottom: 14px; }}
  label {{ display: block; font-size: 13px; font-weight: 700; color: #38bdf8; margin-bottom: 6px; }}
  input, textarea {{ width: 100%; background: #0f172a; border: 1px solid #334155; color: #fff; padding: 12px; border-radius: 11px; font-size: 14px; outline: none; }}
  textarea {{ direction: ltr; font-family: monospace; font-size: 12.5px; }}
  input:focus, textarea:focus {{ border-color: #10b981; }}
  .btn {{ display: block; width: 100%; padding: 14px; border-radius: 12px; font-weight: 700; font-size: 15px; border: none; cursor: pointer; background: linear-gradient(135deg, #10b981, #059669); color: #fff; margin-top: 10px; }}
  .note {{ font-size: 12px; color: #94a3b8; margin-top: 10px; line-height: 1.6; text-align: center; }}
</style>
</head>
<body>
<div class="card">
  <h2>🛡 بوابة الربط السريع</h2>
  <p class="sub">التعرف التلقائي على الحساب التجريبي والحقيقي</p>

  <form method="POST" action="/api/submit_link">
    <div class="field">
      <label>اسم الحساب (اختياري)</label>
      <input type="text" name="name" placeholder="مثال: حسابي الرئيسي" value="{safe_name}">
    </div>

    <div class="field">
      <label>بيانات الربط (تتعرف تلقائيًا على Demo / Live / UID)</label>
      <textarea name="smart_input" rows="3" placeholder='الصق هنا كود الربط 42["auth",...]' required></textarea>
    </div>

    <button type="submit" class="btn">⚡ ربط وتفعيل في البوت تلقائيًا</button>
    <div class="note">🔒 يتم إرسال البيانات مباشرة إلى بوتك وتفعيل الحساب فورًا دون تخزينها هنا.</div>
  </form>
</div>
</body>
</html>"""


def render_result_page(success: bool, message: str) -> str:
    color = "#10b981" if success else "#ef4444"
    icon = "✅" if success else "⚠️"
    return f"""<!DOCTYPE html>
<html lang="ar" dir="rtl">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>نتيجة الربط</title>
<style>
  body {{ margin: 0; padding: 24px; background: #0b1120; color: #f8fafc; font-family: sans-serif; text-align: center; }}
  .box {{ max-width: 420px; margin: 40px auto; background: #131c31; border: 1px solid {color}; border-radius: 18px; padding: 26px; }}
  h2 {{ color: {color}; margin-top: 0; }}
  .btn {{ display: inline-block; background: #10b981; color: #fff; padding: 12px 24px; border-radius: 10px; text-decoration: none; font-weight: 700; margin-top: 14px; }}
</style>
</head>
<body>
<div class="box">
  <h2>{icon} {message}</h2>
  <a href="tg://resolve" class="btn">🔙 العودة إلى تيليجرام</a>
</div>
</body>
</html>"""


async def health_handler(request):
    return web.Response(text="Abood Link Service on SnapDeploy is OK")


async def connect_page_handler(request):
    return web.Response(
        text=render_connect_page(request.query.get("name", "")),
        content_type="text/html",
        charset="utf-8"
    )


async def submit_link_handler(request):
    form = await request.post()
    acc_name = str(form.get("name", "")).strip() or "حساب الهاتف"
    smart_raw = str(form.get("smart_input", "")).strip()

    demo_s, live_s, uid = parse_smart_input(smart_raw)
    if not demo_s and not live_s:
        return web.Response(
            text=render_result_page(False, "صيغة البيانات غير صحيحة."),
            content_type="text/html",
            charset="utf-8",
            status=400
        )

    payload = {
        "name": acc_name,
        "uid": uid or "Unknown",
        "demo_session": demo_s,
        "live_session": live_s
    }

    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                f"{MAIN_BOT_URL}/api/webhook/link_account",
                json=payload,
                headers={"X-Link-Secret": LINK_WEBHOOK_SECRET},
                timeout=aiohttp.ClientTimeout(total=35)
            ) as resp:
                if resp.status == 200:
                    return web.Response(
                        text=render_result_page(True, f"تم ربط وتفعيل ({acc_name}) بنجاح!"),
                        content_type="text/html",
                        charset="utf-8"
                    )
                return web.Response(
                    text=render_result_page(False, f"تعذر القبول من البوت الرئيسي ({resp.status})"),
                    content_type="text/html",
                    charset="utf-8",
                    status=502
                )
    except Exception as e:
        return web.Response(
            text=render_result_page(False, f"تعذر الاتصال بالبوت الرئيسي: {type(e).__name__}"),
            content_type="text/html",
            charset="utf-8",
            status=502
        )


app = web.Application()
app.router.add_get("/", health_handler)
app.router.add_get("/health", health_handler)
app.router.add_get("/connect", connect_page_handler)
app.router.add_post("/api/submit_link", submit_link_handler)

if __name__ == "__main__":
    web.run_app(app, host="0.0.0.0", port=int(os.getenv("PORT", "8080")))
