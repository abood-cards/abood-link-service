import asyncio
import json
import os
import re
import sys
import aiohttp
from aiohttp import web
from playwright.async_api import async_playwright

if sys.stdout.encoding != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8")

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
ADMIN_CHAT_ID = str(os.getenv("ADMIN_CHAT_ID", ""))
MAIN_BOT_URL = os.getenv("MAIN_BOT_URL", "https://abood-trader.onrender.com").rstrip("/")
LINK_WEBHOOK_SECRET = os.getenv("LINK_WEBHOOK_SECRET", "abood_secure_link_2026")

STATE = {
    "task": None,
    "page": None,
    "cancel": False,
    "awaiting": None,
    "input_event": asyncio.Event(),
    "last_input": "",
    "acc_name": "حساب جديد",
    "method": "EMAIL"
}


def parse_auth_frame(frame: str) -> dict | None:
    try:
        clean = frame.strip()
        if not clean.startswith('42["auth",'):
            return None
        parsed = json.loads(clean[2:])
        if isinstance(parsed, list) and len(parsed) >= 2 and isinstance(parsed[1], dict):
            return parsed[1]
    except Exception:
        pass
    return None


def extract_session_and_uid(html_text: str) -> tuple[str, str]:
    if not html_text:
        return "", ""
    sess_m = re.search(r'["\']?session["\']?\s*[:=]\s*["\']([a-zA-Z0-9_-]{16,64})["\']', html_text)
    uid_m = re.search(r'["\']?uid["\']?\s*[:=]\s*["\']?(\d{5,15})["\']?', html_text)
    if not uid_m:
        uid_m = re.search(r'data-uid=["\'](\d{5,15})["\']', html_text)
    return (sess_m.group(1) if sess_m else ""), (uid_m.group(1) if uid_m else "")


async def tg_post(http: aiohttp.ClientSession, method: str, payload: dict):
    if not TELEGRAM_BOT_TOKEN:
        return None
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/{method}"
    try:
        async with http.post(url, json=payload, timeout=aiohttp.ClientTimeout(total=15)) as resp:
            return await resp.json()
    except Exception:
        return None


async def send_tg_msg(http: aiohttp.ClientSession, text: str, reply_markup: dict = None):
    payload = {"chat_id": ADMIN_CHAT_ID, "text": text, "parse_mode": "HTML"}
    if reply_markup:
        payload["reply_markup"] = reply_markup
    await tg_post(http, "sendMessage", payload)


async def send_tg_photo(http: aiohttp.ClientSession, photo_bytes: bytes, caption: str, reply_markup: dict = None):
    if not TELEGRAM_BOT_TOKEN:
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendPhoto"
    form = aiohttp.FormData()
    form.add_field("chat_id", ADMIN_CHAT_ID)
    form.add_field("caption", caption[:1000])
    form.add_field("parse_mode", "HTML")
    if reply_markup:
        form.add_field("reply_markup", json.dumps(reply_markup))
    form.add_field("photo", photo_bytes, filename="screen.png", content_type="image/png")
    try:
        async with http.post(url, data=form, timeout=aiohttp.ClientTimeout(total=25)) as resp:
            await resp.json()
    except Exception:
        pass


def cancel_active_session():
    STATE["cancel"] = True
    STATE["input_event"].set()
    STATE["awaiting"] = None
    if STATE["task"] and not STATE["task"].done():
        STATE["task"].cancel()
    STATE["task"] = None
    STATE["page"] = None


async def wait_step_input(step_name: str, timeout_sec: int = 180) -> str | None:
    STATE["input_event"].clear()
    STATE["last_input"] = ""
    STATE["awaiting"] = step_name
    try:
        await asyncio.wait_for(STATE["input_event"].wait(), timeout=timeout_sec)
    except asyncio.TimeoutError:
        return None
    finally:
        if STATE["awaiting"] == step_name:
            STATE["awaiting"] = None
    if STATE["cancel"]:
        return None
    return STATE["last_input"]


async def run_browser_automation(acc_name: str, login_method: str):
    STATE["cancel"] = False
    cancel_kb = {"inline_keyboard": [[{"text": "❌ إلغاء العملية", "callback_data": "sd_cancel"}]]}
    shot_kb = {
        "inline_keyboard": [
            [{"text": "📸 تحديث لقطة الشاشة", "callback_data": "sd_shot"}],
            [{"text": "❌ إلغاء العملية", "callback_data": "sd_cancel"}]
        ]
    }

    pw = None
    browser = None
    async with aiohttp.ClientSession() as http:
        try:
            await send_tg_msg(
                http,
                f"🌐 <b>جاري تشغيل المتصفح الآمن على Northflank لحساب ({acc_name})...</b>\n"
                f"⏳ يُرجى الانتظار بضع ثوانٍ.",
                cancel_kb
            )

            pw = await async_playwright().start()
            browser = await pw.chromium.launch(
                headless=True,
                args=[
                    "--no-sandbox",
                    "--disable-setuid-sandbox",
                    "--disable-dev-shm-usage",
                    "--disable-gpu",
                    "--disable-blink-features=AutomationControlled",
                    "--window-size=1280,800",
                ]
            )
            context = await browser.new_context(
                viewport={"width": 1280, "height": 800},
                user_agent=(
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
                ),
                locale="en-US"
            )
            await context.add_init_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined});")

            page = await context.new_page()
            STATE["page"] = page
            captured_ws = {"session": "", "uid": ""}

            def attach_ws(target_p):
                def on_ws(ws):
                    def on_frame(payload):
                        if isinstance(payload, str) and payload.startswith('42["auth",'):
                            parsed = parse_auth_frame(payload)
                            if parsed and parsed.get("session"):
                                captured_ws["session"] = str(parsed["session"])
                                if parsed.get("uid"):
                                    captured_ws["uid"] = str(parsed["uid"])
                    ws.on("framesent", on_frame)
                target_p.on("websocket", on_ws)

            attach_ws(page)
            context.on("page", attach_ws)

            loaded_ok = False
            for url in ("https://pocketoption.com/en/login/", "https://po.trade/en/login/"):
                try:
                    await page.goto(url, wait_until="domcontentloaded", timeout=30000)
                    await page.wait_for_timeout(2000)
                    if await page.locator('input[type="email"], input[name="email"], a[href*="google"], button:has-text("Google")').count() > 0:
                        loaded_ok = True
                        break
                except Exception:
                    continue

            if not loaded_ok:
                shot = await page.screenshot(type="png")
                await send_tg_photo(http, shot, "⚠️ <b>لم يظهر نموذج الدخول في الصفحة (راجع الصورة أعلاه).</b>")
                return

            if login_method == "GOOGLE":
                clicked = False
                popup_p = None
                for sel in ('a[href*="google"]', 'button:has-text("Google")', 'a:has-text("Google")', '.social-btn--google'):
                    loc = page.locator(sel).first
                    if await loc.count() > 0:
                        try:
                            async with context.expect_page(timeout=4000) as p_info:
                                await loc.click(timeout=5000)
                            popup_p = await p_info.value
                        except Exception:
                            await loc.click(timeout=5000)
                        clicked = True
                        break

                if not clicked:
                    shot = await page.screenshot(type="png")
                    await send_tg_photo(http, shot, "⚠️ تعذر العثور على زر Google في الصفحة.", cancel_kb)
                    return

                active_p = popup_p if popup_p else page
                STATE["page"] = active_p
                await active_p.wait_for_load_state("domcontentloaded", timeout=25000)
                await active_p.wait_for_timeout(1500)

                shot = await active_p.screenshot(type="png")
                await send_tg_photo(
                    http, shot,
                    "🔵 <b>تسجيل الدخول عبر Google — الخطوة 1:</b>\nأرسل الآن <b>بريد Gmail</b> الخاص بحسابك:",
                    cancel_kb
                )

                g_email = await wait_step_input("G_EMAIL")
                if not g_email:
                    return

                email_inp = active_p.locator('input[type="email"]').first
                await email_inp.wait_for(state="visible", timeout=15000)
                await email_inp.fill(g_email.strip())
                await active_p.keyboard.press("Enter")
                await active_p.wait_for_timeout(3000)

                pass_inp = active_p.locator('input[type="password"]').first
                if await pass_inp.count() > 0:
                    await send_tg_msg(
                        http,
                        "🔒 <b>تسجيل الدخول عبر Google — الخطوة 2:</b>\nأرسل الآن <b>كلمة مرور Google</b> (ستُحذف رسالتك فور استلامها):",
                        cancel_kb
                    )
                    g_pass = await wait_step_input("G_PASS")
                    if not g_pass:
                        return
                    await pass_inp.fill(g_pass)
                    await active_p.keyboard.press("Enter")
                    await active_p.wait_for_timeout(3500)

                for _ in range(20):
                    if STATE["cancel"] or captured_ws["session"]:
                        break
                    if ("pocketoption.com" in page.url or "po.trade" in page.url) and "/login" not in page.url:
                        break

                    cur_p = active_p if not active_p.is_closed() else page
                    STATE["page"] = cur_p
                    if "accounts.google.com" in cur_p.url and ("challenge" in cur_p.url or "signin" in cur_p.url):
                        shot = await cur_p.screenshot(type="png")
                        code_inp = cur_p.locator('input[type="tel"], input[name="totpPin"], input[id*="pin"]').first
                        if await code_inp.count() > 0:
                            await send_tg_photo(
                                http, shot,
                                "📲 <b>التحقق بخطوتين من Google:</b>\nأرسل الآن <b>رمز التحقق</b>:",
                                cancel_kb
                            )
                            otp = await wait_step_input("G_2FA")
                            if not otp:
                                return
                            await code_inp.fill(otp.strip())
                            await cur_p.keyboard.press("Enter")
                            await cur_p.wait_for_timeout(3000)
                        else:
                            await send_tg_photo(
                                http, shot,
                                "📱 <b>تأكيد الدخول من هاتفك:</b>\nوافق على إشعار Google في هاتفك واختر الرقم الظاهر في الصورة أعلاه.\n⏳ جاري الانتظار...",
                                shot_kb
                            )
                            await cur_p.wait_for_timeout(4500)
                    else:
                        await page.wait_for_timeout(1500)

            else:
                await send_tg_msg(
                    http,
                    "📧 <b>تسجيل الدخول بالبريد الإلكتروني — الخطوة 1:</b>\nأرسل الآن <b>البريد الإلكتروني</b> المسجل في Pocket Option:",
                    cancel_kb
                )
                po_email = await wait_step_input("PO_EMAIL")
                if not po_email:
                    return

                await send_tg_msg(
                    http,
                    "🔒 <b>تسجيل الدخول بالبريد الإلكتروني — الخطوة 2:</b>\nأرسل الآن <b>كلمة المرور</b> (ستُحذف رسالتك فور استلامها):",
                    cancel_kb
                )
                po_pass = await wait_step_input("PO_PASS")
                if not po_pass:
                    return

                email_inp = page.locator('input[name="email"], input[type="email"]').first
                pass_inp = page.locator('input[name="password"], input[type="password"]').first
                await email_inp.wait_for(state="visible", timeout=15000)
                await email_inp.fill(po_email.strip())
                await pass_inp.fill(po_pass)

                btn = page.locator('button[type="submit"], input[type="submit"]').first
                if await btn.count() > 0:
                    await btn.click()
                else:
                    await page.keyboard.press("Enter")
                await page.wait_for_timeout(3500)

                if "/login" in page.url and not captured_ws["session"]:
                    shot = await page.screenshot(type="png")
                    await send_tg_photo(
                        http, shot,
                        "⏳ <b>فحص حالة الدخول...</b>\nإذا ظهر رمز تحقق 2FA أرسله هنا الآن:",
                        shot_kb
                    )
                    for _ in range(10):
                        if STATE["cancel"] or captured_ws["session"] or "/login" not in page.url:
                            break
                        otp_try = await wait_step_input("PO_2FA", timeout_sec=4)
                        if otp_try:
                            inp_2fa = page.locator('input:visible').first
                            if await inp_2fa.count() > 0:
                                await inp_2fa.fill(otp_try.strip())
                                await page.keyboard.press("Enter")
                                await page.wait_for_timeout(3000)

            if STATE["cancel"]:
                return

            STATE["page"] = page
            await page.wait_for_timeout(2000)
            demo_s, uid_val = extract_session_and_uid(await page.content())
            live_s = ""

            if captured_ws["session"]:
                demo_s = demo_s or captured_ws["session"]
            if captured_ws["uid"]:
                uid_val = uid_val or captured_ws["uid"]

            try:
                res_both = await page.evaluate("""async () => {
                    let out = {demo: '', live: '', uid: ''};
                    const grab = (t) => {
                        let u = (t.match(/["']?uid["']?\\s*[:=]\\s*["']?(\\d{5,15})["']?/i) || [])[1] || '';
                        let s = (t.match(/["']?session["']?\\s*[:=]\\s*["']([a-zA-Z0-9_-]{16,64})["']/i) || [])[1] || '';
                        return {u, s};
                    };
                    try {
                        let r1 = await fetch('/en/cabinet/demo-quick-high-low/', {credentials: 'include'});
                        if (r1.ok) {
                            let g1 = grab(await r1.text());
                            if (g1.s) out.demo = g1.s;
                            if (g1.u) out.uid = g1.u;
                        }
                    } catch(e) {}
                    try {
                        let r2 = await fetch('/en/cabinet/quick-high-low/', {credentials: 'include'});
                        if (r2.ok) {
                            let g2 = grab(await r2.text());
                            if (g2.s) out.live = g2.s;
                            if (g2.u && !out.uid) out.uid = g2.u;
                        }
                    } catch(e) {}
                    return out;
                }""")
                if isinstance(res_both, dict):
                    if res_both.get("demo"):
                        demo_s = res_both["demo"]
                    if res_both.get("live"):
                        live_s = res_both["live"]
                    if res_both.get("uid") and not uid_val:
                        uid_val = res_both["uid"]
            except Exception:
                pass

            if not demo_s and not live_s:
                shot = await page.screenshot(type="png")
                await send_tg_photo(
                    http, shot,
                    "⚠️ <b>تعذر استخراج الجلسة بعد محاولة الدخول:</b>\nتأكد من صحة البيانات أو التحقق الأمني ثم أعد المحاولة."
                )
                return

            payload = {
                "name": acc_name,
                "uid": uid_val or "Unknown",
                "demo_session": demo_s or live_s,
                "live_session": live_s
            }
            async with http.post(
                f"{MAIN_BOT_URL}/api/webhook/link_account",
                json=payload,
                headers={"X-Link-Secret": LINK_WEBHOOK_SECRET},
                timeout=aiohttp.ClientTimeout(total=25)
            ) as resp:
                if resp.status != 200:
                    err_txt = await resp.text()
                    await send_tg_msg(http, f"⚠️ تم استخراج الجلسة لكن رفض البوت الرئيسي استلامها: <code>{err_txt[:120]}</code>")

        except asyncio.CancelledError:
            pass
        except Exception as e:
            err_msg = str(e).splitlines()[0][:180] if str(e) else type(e).__name__
            if STATE["page"] and not STATE["page"].is_closed():
                try:
                    shot = await STATE["page"].screenshot(type="png")
                    await send_tg_photo(http, shot, f"⚠️ <b>خطأ في متصفح Northflank:</b>\n<code>{err_msg}</code>")
                except Exception:
                    await send_tg_msg(http, f"⚠️ <b>خطأ في متصفح Northflank:</b>\n<code>{err_msg}</code>")
            else:
                await send_tg_msg(http, f"⚠️ <b>خطأ في متصفح Northflank:</b>\n<code>{err_msg}</code>")
        finally:
            STATE["page"] = None
            STATE["awaiting"] = None
            if browser:
                try:
                    await browser.close()
                except Exception:
                    pass
            if pw:
                try:
                    await pw.stop()
                except Exception:
                    pass


async def start_trigger_handler(request):
    secret = request.headers.get("X-Link-Secret", "")
    if secret != LINK_WEBHOOK_SECRET:
        return web.Response(text="Unauthorized", status=401)
    data = await request.json()
    acc_name = str(data.get("name", "حساب جديد")).strip() or "حساب جديد"
    method = str(data.get("method", "EMAIL")).upper()

    cancel_active_session()
    STATE["acc_name"] = acc_name
    STATE["method"] = method
    STATE["task"] = asyncio.create_task(run_browser_automation(acc_name, method))
    return web.json_response({"ok": True})


async def input_forward_handler(request):
    secret = request.headers.get("X-Link-Secret", "")
    if secret != LINK_WEBHOOK_SECRET:
        return web.Response(text="Unauthorized", status=401)
    data = await request.json()
    kind = data.get("kind", "")

    if kind == "CANCEL":
        cancel_active_session()
        async with aiohttp.ClientSession() as http:
            await send_tg_msg(http, "❌ تم إلغاء جلسة الربط وإغلاق المتصفح.")
        return web.json_response({"ok": True})

    if kind == "SHOT":
        if STATE["page"] and not STATE["page"].is_closed():
            try:
                shot = await STATE["page"].screenshot(type="png")
                async with aiohttp.ClientSession() as http:
                    await send_tg_photo(http, shot, "📸 <b>لقطة الشاشة الحالية من المتصفح:</b>")
            except Exception:
                pass
        return web.json_response({"ok": True})

    if kind == "TEXT" and STATE["awaiting"]:
        STATE["last_input"] = str(data.get("text", ""))
        STATE["input_event"].set()
        return web.json_response({"ok": True, "consumed": True})

    return web.json_response({"ok": True, "consumed": False})


async def health_handler(request):
    return web.Response(text="Northflank Browser Link Service is Ready")


app = web.Application()
app.router.add_get("/", health_handler)
app.router.add_get("/health", health_handler)
app.router.add_post("/api/start_login", start_trigger_handler)
app.router.add_post("/api/forward_input", input_forward_handler)

if __name__ == "__main__":
    web.run_app(app, host="0.0.0.0", port=int(os.getenv("PORT", "8080")))
