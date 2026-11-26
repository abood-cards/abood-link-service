import asyncio
import base64
import json
import os
import re
import sys
import aiohttp
from aiohttp import web

try:
    from playwright.async_api import async_playwright
except ImportError:
    async_playwright = None

if sys.stdout.encoding != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8")

# رابط البوت الرئيسي على Render
MAIN_BOT_URL = os.getenv("MAIN_BOT_URL", "https://abood-trader.onrender.com").rstrip("/")
# مفتاح الأمان المشترك بين السيرفرين (يجب أن يطابق ما في Render)
LINK_WEBHOOK_SECRET = os.getenv("LINK_WEBHOOK_SECRET", "abood_secure_link_2026")
# اختياري: بروكسي في حال احتجت لتمرير الاتصال عبر IP محدد
PO_PROXY_URL = os.getenv("PO_PROXY_URL", "").strip()


def log(tag: str, msg: str):
    print(f"[{tag}] {msg}", flush=True)


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


class RemotePlaywrightLinker:
    def __init__(self):
        self.task: asyncio.Task | None = None
        self.active_page = None
        self.input_event = asyncio.Event()
        self.last_input = ""
        self.cancelled = False

    def cancel_current(self):
        self.cancelled = True
        self.input_event.set()
        if self.task and not self.task.done():
            self.task.cancel()
        self.task = None
        self.active_page = None

    async def push_event_to_main_bot(
        self,
        event_type: str,
        text: str,
        next_state: str = "",
        expect_password: bool = False,
        show_controls: bool = True,
        image_bytes: bytes | None = None
    ):
        payload = {
            "event_type": event_type,
            "text": text,
            "next_state": next_state,
            "expect_password": expect_password,
            "show_controls": show_controls,
            "image_b64": base64.b64encode(image_bytes).decode("ascii") if image_bytes else ""
        }
        headers = {"X-Link-Secret": LINK_WEBHOOK_SECRET}
        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    f"{MAIN_BOT_URL}/api/webhook/link_event",
                    json=payload,
                    headers=headers,
                    timeout=aiohttp.ClientTimeout(total=25)
                ) as resp:
                    await resp.text()
        except Exception as e:
            log("EVENT_ERR", f"تعذر إرسال الحدث للبوت الرئيسي: {e}")

    async def send_final_account_to_main_bot(self, name: str, uid: str, demo_s: str, live_s: str) -> bool:
        payload = {
            "name": name,
            "uid": uid or "Unknown",
            "demo_session": demo_s,
            "live_session": live_s
        }
        headers = {"X-Link-Secret": LINK_WEBHOOK_SECRET}
        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    f"{MAIN_BOT_URL}/api/webhook/link_account",
                    json=payload,
                    headers=headers,
                    timeout=aiohttp.ClientTimeout(total=30)
                ) as resp:
                    return resp.status == 200
        except Exception as e:
            log("FINAL_ERR", f"تعذر إرسال الحساب النهائي للبوت الرئيسي: {e}")
            return False

    async def wait_user_input(self, timeout_sec: int = 180) -> str | None:
        self.input_event.clear()
        self.last_input = ""
        try:
            await asyncio.wait_for(self.input_event.wait(), timeout=timeout_sec)
        except asyncio.TimeoutError:
            return None
        if self.cancelled:
            return None
        return self.last_input

    async def take_manual_screenshot(self):
        if self.active_page and not self.active_page.is_closed():
            try:
                shot = await self.active_page.screenshot(type="png", timeout=10000)
                await self.push_event_to_main_bot(
                    "PHOTO",
                    "📸 <b>لقطة الشاشة الحالية من المتصفح:</b>",
                    next_state="REMOTE_INPUT",
                    show_controls=True,
                    image_bytes=shot
                )
            except Exception as e:
                log("SHOT_ERR", f"فشل التقاط الصورة اليدوية: {e}")

    async def run_login_flow(self, acc_name: str, method: str):
        if async_playwright is None:
            await self.push_event_to_main_bot(
                "ERROR",
                "⚠️ مكتبة <code>playwright</code> غير مثبتة في حاوية الاستخراج."
            )
            return

        self.cancelled = False
        pw = None
        browser = None
        try:
            await self.push_event_to_main_bot(
                "STATUS",
                f"🌐 <b>جاري تشغيل المتصفح الآمن لحساب ({acc_name})...</b>\n⏳ يُرجى الانتظار بضع ثوانٍ.",
                show_controls=False
            )

            pw = await async_playwright().start()
            launch_args = [
                "--no-sandbox",
                "--disable-setuid-sandbox",
                "--disable-dev-shm-usage",
                "--disable-gpu",
                "--disable-extensions",
                "--disable-software-rasterizer",
                "--disable-background-networking",
                "--mute-audio",
                "--no-first-run",
                "--renderer-process-limit=1",
                "--js-flags=--max-old-space-size=192",
                "--disable-blink-features=AutomationControlled",
                "--window-size=1024,720",
            ]
            launch_kwargs = {"headless": True, "args": launch_args}
            if PO_PROXY_URL:
                launch_kwargs["proxy"] = {"server": PO_PROXY_URL}

            browser = await pw.chromium.launch(**launch_kwargs)
            context = await browser.new_context(
                viewport={"width": 1024, "height": 720},
                user_agent=(
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
                ),
                locale="en-US"
            )
            await context.add_init_script(
                "Object.defineProperty(navigator, 'webdriver', {get: () => undefined});"
            )

            async def route_filter(route):
                r_type = route.request.resource_type
                r_url = route.request.url.lower()
                if r_type in ("media", "font"):
                    await route.abort()
                elif any(x in r_url for x in ("google-analytics", "googletagmanager", "facebook", "doubleclick", "hotjar")):
                    await route.abort()
                else:
                    await route.continue_()

            await context.route("**/*", route_filter)

            page = await context.new_page()
            self.active_page = page

            captured_ws = {"demo": "", "live": "", "uid": ""}

            def attach_ws_listener(target_page):
                def on_ws(ws):
                    def on_frame(payload):
                        if isinstance(payload, str) and payload.startswith('42["auth",'):
                            parsed = parse_auth_frame(payload)
                            if parsed and parsed.get("session"):
                                s_val = str(parsed["session"])
                                if int(parsed.get("isDemo", 1)) == 0:
                                    captured_ws["live"] = s_val
                                else:
                                    captured_ws["demo"] = s_val
                                if parsed.get("uid"):
                                    captured_ws["uid"] = str(parsed["uid"])
                    ws.on("framesent", on_frame)
                target_page.on("websocket", on_ws)

            attach_ws_listener(page)
            context.on("page", attach_ws_listener)

            login_urls = [
                "https://pocketoption.com/en/login/",
                "https://po.trade/en/login/",
            ]
            loaded_ok = False
            last_err = ""
            for url in login_urls:
                try:
                    await page.goto(url, wait_until="domcontentloaded", timeout=30000)
                    await page.wait_for_selector(
                        'input[type="email"], input[name="email"], a[href*="google"], button:has-text("Google")',
                        timeout=18000
                    )
                    loaded_ok = True
                    break
                except Exception as e:
                    last_err = str(e).splitlines()[0][:150]
                    continue

            if not loaded_ok:
                shot = None
                try:
                    shot = await page.screenshot(type="png", timeout=8000)
                except Exception:
                    pass
                await self.push_event_to_main_bot(
                    "PHOTO" if shot else "ERROR",
                    f"⚠️ <b>تعذر فتح نموذج تسجيل الدخول:</b>\n<code>{last_err}</code>",
                    image_bytes=shot
                )
                return

            # ==========================================
            # المسار أ: تسجيل الدخول عبر Google
            # ==========================================
            if method == "GOOGLE":
                google_selectors = [
                    'a[href*="google"]',
                    'button:has-text("Google")',
                    'a:has-text("Google")',
                    '.social-btn--google',
                    '[data-provider="google"]'
                ]
                clicked_google = False
                popup_page = None

                for sel in google_selectors:
                    loc = page.locator(sel).first
                    if await loc.count() > 0:
                        try:
                            async with context.expect_page(timeout=4500) as popup_info:
                                await loc.click(timeout=5000)
                            popup_page = await popup_info.value
                        except Exception:
                            try:
                                await loc.click(timeout=5000)
                            except Exception:
                                continue
                        clicked_google = True
                        break

                if not clicked_google:
                    shot = await page.screenshot(type="png")
                    await self.push_event_to_main_bot(
                        "PHOTO",
                        "⚠️ لم يظهر زر Google في الصفحة. أعد المحاولة أو استخدم البريد الإلكتروني.",
                        image_bytes=shot
                    )
                    return

                active_p = popup_page if popup_page else page
                self.active_page = active_p
                await active_p.wait_for_load_state("domcontentloaded", timeout=25000)
                await active_p.wait_for_timeout(1500)

                shot = await active_p.screenshot(type="png")
                await self.push_event_to_main_bot(
                    "PHOTO",
                    "🔵 <b>تسجيل الدخول عبر Google — الخطوة 1:</b>\nأرسل الآن <b>بريد Gmail</b> الخاص بحسابك:",
                    next_state="REMOTE_INPUT",
                    show_controls=True,
                    image_bytes=shot
                )

                g_email = await self.wait_user_input(timeout_sec=180)
                if not g_email:
                    return

                email_inp = active_p.locator('input[type="email"]').first
                await email_inp.wait_for(state="visible", timeout=15000)
                await email_inp.fill(g_email.strip())
                await active_p.keyboard.press("Enter")
                await active_p.wait_for_timeout(3000)

                pass_inp = active_p.locator('input[type="password"]').first
                if await pass_inp.count() == 0:
                    shot = await active_p.screenshot(type="png")
                    await self.push_event_to_main_bot(
                        "PHOTO",
                        "⚠️ <b>تنبيه من Google:</b>\nلم يظهر حقل كلمة المرور بعد (ربما يوجد تحقق أمني). أرسل النص المطلوب في الصورة:",
                        next_state="REMOTE_INPUT",
                        show_controls=True,
                        image_bytes=shot
                    )
                    retry_txt = await self.wait_user_input(timeout_sec=180)
                    if not retry_txt:
                        return
                    any_inp = active_p.locator("input:visible").first
                    if await any_inp.count() > 0:
                        await any_inp.fill(retry_txt.strip())
                        await active_p.keyboard.press("Enter")
                        await active_p.wait_for_timeout(3000)

                pass_inp = active_p.locator('input[type="password"]').first
                if await pass_inp.count() > 0:
                    await self.push_event_to_main_bot(
                        "PROMPT",
                        "🔒 <b>تسجيل الدخول عبر Google — الخطوة 2:</b>\nأرسل الآن <b>كلمة مرور حساب Google</b> (ستُحذف رسالتك فور استلامها للحماية):",
                        next_state="REMOTE_INPUT",
                        expect_password=True,
                        show_controls=False
                    )
                    g_pass = await self.wait_user_input(timeout_sec=180)
                    if not g_pass:
                        return

                    await pass_inp.fill(g_pass)
                    await active_p.keyboard.press("Enter")
                    await active_p.wait_for_timeout(3500)

                # انتظار التحقق بخطوتين (2FA / تأكيد الهاتف / رمز SMS)
                prompt_sent = False
                for _ in range(25):
                    if self.cancelled or captured_ws["demo"] or captured_ws["live"]:
                        break
                    if ("pocketoption.com" in page.url or "po.trade" in page.url) and "/login" not in page.url:
                        break

                    cur_p = active_p if not active_p.is_closed() else page
                    self.active_page = cur_p
                    cur_url = cur_p.url

                    if "accounts.google.com" in cur_url and ("challenge" in cur_url or "signin" in cur_url):
                        code_inp = cur_p.locator('input[type="tel"], input[name="totpPin"], input[id*="pin"], input[type="text"]:visible').first
                        if await code_inp.count() > 0:
                            shot = await cur_p.screenshot(type="png")
                            await self.push_event_to_main_bot(
                                "PHOTO",
                                "📲 <b>التحقق بخطوتين من Google:</b>\nأرسل الآن <b>رمز التحقق</b> الظاهر في رسالتك أو تطبيق المصادقة:",
                                next_state="REMOTE_INPUT",
                                show_controls=True,
                                image_bytes=shot
                            )
                            otp_code = await self.wait_user_input(timeout_sec=180)
                            if not otp_code:
                                return
                            await code_inp.fill(otp_code.strip())
                            await cur_p.keyboard.press("Enter")
                            await cur_p.wait_for_timeout(3000)
                            prompt_sent = False
                        else:
                            if not prompt_sent:
                                shot = await cur_p.screenshot(type="png")
                                await self.push_event_to_main_bot(
                                    "PHOTO",
                                    "📱 <b>تأكيد الدخول من هاتفك:</b>\nافتح إشعار Google على هاتفك واضغط <b>نعم (Yes)</b> ثم اختر الرقم الظاهر في الصورة أعلاه.\n⏳ البوت ينتظر موافقتك تلقائيًا...",
                                    next_state="REMOTE_INPUT",
                                    show_controls=True,
                                    image_bytes=shot
                                )
                                prompt_sent = True
                            await cur_p.wait_for_timeout(3500)
                    else:
                        await page.wait_for_timeout(1500)

            # ==========================================
            # المسار ب: تسجيل الدخول بالبريد الإلكتروني وكلمة المرور
            # ==========================================
            else:
                await self.push_event_to_main_bot(
                    "PROMPT",
                    "📧 <b>تسجيل الدخول بالبريد الإلكتروني — الخطوة 1:</b>\nأرسل الآن <b>البريد الإلكتروني</b> المسجل في Pocket Option:",
                    next_state="REMOTE_INPUT",
                    show_controls=False
                )
                po_email = await self.wait_user_input(timeout_sec=180)
                if not po_email:
                    return

                await self.push_event_to_main_bot(
                    "PROMPT",
                    "🔒 <b>تسجيل الدخول بالبريد الإلكتروني — الخطوة 2:</b>\nأرسل الآن <b>كلمة المرور</b> (ستُحذف رسالتك فور استلامها للحماية):",
                    next_state="REMOTE_INPUT",
                    expect_password=True,
                    show_controls=False
                )
                po_pass = await self.wait_user_input(timeout_sec=180)
                if not po_pass:
                    return

                email_field = page.locator('input[name="email"], input[type="email"]').first
                pass_field = page.locator('input[name="password"], input[type="password"]').first
                await email_field.wait_for(state="visible", timeout=15000)
                await email_field.fill(po_email.strip())
                await pass_field.fill(po_pass)

                submit_btn = page.locator('button[type="submit"], input[type="submit"]').first
                if await submit_btn.count() > 0:
                    await submit_btn.click()
                else:
                    await page.keyboard.press("Enter")

                await page.wait_for_timeout(3500)

                if "/login" in page.url and not captured_ws["demo"] and not captured_ws["live"]:
                    shot = await page.screenshot(type="png")
                    await self.push_event_to_main_bot(
                        "PHOTO",
                        "📲 <b>التحقق من الدخول:</b>\nإذا ظهر حقل رمز تحقق (2FA) أرسل الرمز الآن، أو انتظر لحظات لإتمام الدخول:",
                        next_state="REMOTE_INPUT",
                        show_controls=True,
                        image_bytes=shot
                    )
                    for _ in range(12):
                        if self.cancelled or captured_ws["demo"] or captured_ws["live"] or "/login" not in page.url:
                            break
                        if self.last_input:
                            code_val = self.last_input.strip()
                            self.last_input = ""
                            inp_2fa = page.locator('input[type="text"]:visible, input[type="tel"]:visible, input[name*="code"]:visible').first
                            if await inp_2fa.count() > 0:
                                await inp_2fa.fill(code_val)
                                await page.keyboard.press("Enter")
                        await page.wait_for_timeout(2000)

            if self.cancelled:
                return

            # ==========================================
            # استخراج جلسة الديمو + الحقيقي + الآيدي وإرسالها إلى Render
            # ==========================================
            self.active_page = page
            await page.wait_for_timeout(2000)
            html_content = await page.content()
            page_sess, uid_val = extract_session_and_uid(html_content)

            demo_sess = captured_ws["demo"] or page_sess
            live_sess = captured_ws["live"]
            if captured_ws["uid"]:
                uid_val = uid_val or captured_ws["uid"]

            try:
                both_res = await page.evaluate("""async () => {
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
                if isinstance(both_res, dict):
                    if both_res.get("demo"):
                        demo_sess = both_res["demo"]
                    if both_res.get("live"):
                        live_sess = both_res["live"]
                    if both_res.get("uid") and not uid_val:
                        uid_val = both_res["uid"]
            except Exception:
                pass

            if not demo_sess and not live_sess:
                shot = await page.screenshot(type="png")
                await self.push_event_to_main_bot(
                    "PHOTO",
                    "⚠️ <b>تعذر استخراج الجلسة بعد محاولة الدخول:</b>\nتأكد من صحة البيانات أو رمز التحقق ثم أعد المحاولة.",
                    image_bytes=shot
                )
                return

            sent_ok = await self.send_final_account_to_main_bot(
                acc_name,
                uid_val or "Unknown",
                demo_sess or live_sess,
                live_sess
            )
            if not sent_ok:
                await self.push_event_to_main_bot(
                    "ERROR",
                    "⚠️ تم تسجيل الدخول بنجاح، ولكن تعذر الوصول إلى رابط البوت الرئيسي على Render لإرسال الجلسة."
                )

        except asyncio.CancelledError:
            log("CANCEL", "تم إلغاء عملية الربط.")
        except Exception as e:
            err_msg = str(e).splitlines()[0][:180] if str(e) else type(e).__name__
            log("FLOW_ERR", f"خطأ أثناء الربط: {err_msg}")
            shot = None
            try:
                if self.active_page and not self.active_page.is_closed():
                    shot = await self.active_page.screenshot(type="png", timeout=8000)
            except Exception:
                pass
            await self.push_event_to_main_bot(
                "PHOTO" if shot else "ERROR",
                f"⚠️ <b>حدث خطأ أثناء الاتصال بالمنصة:</b>\n<code>{err_msg}</code>",
                image_bytes=shot
            )
        finally:
            self.active_page = None
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


linker = RemotePlaywrightLinker()


def check_auth(request) -> bool:
    secret = request.headers.get("X-Link-Secret", "")
    return bool(LINK_WEBHOOK_SECRET and secret == LINK_WEBHOOK_SECRET)


async def health_handler(request):
    return web.Response(text="Abood Playwright Link Microservice is alive!")


async def start_session_handler(request):
    if not check_auth(request):
        return web.Response(text="Unauthorized", status=401)
    data = await request.json()
    acc_name = str(data.get("acc_name", "حساب الهاتف")).strip() or "حساب الهاتف"
    method = str(data.get("method", "EMAIL")).strip().upper()
    linker.cancel_current()
    linker.task = asyncio.create_task(linker.run_login_flow(acc_name, method))
    return web.json_response({"ok": True})


async def input_session_handler(request):
    if not check_auth(request):
        return web.Response(text="Unauthorized", status=401)
    data = await request.json()
    linker.last_input = str(data.get("value", ""))
    linker.input_event.set()
    return web.json_response({"ok": True})


async def screenshot_session_handler(request):
    if not check_auth(request):
        return web.Response(text="Unauthorized", status=401)
    asyncio.create_task(linker.take_manual_screenshot())
    return web.json_response({"ok": True})


async def cancel_session_handler(request):
    if not check_auth(request):
        return web.Response(text="Unauthorized", status=401)
    linker.cancel_current()
    return web.json_response({"ok": True})


app = web.Application()
app.router.add_get("/", health_handler)
app.router.add_get("/health", health_handler)
app.router.add_post("/api/session/start", start_session_handler)
app.router.add_post("/api/session/input", input_session_handler)
app.router.add_post("/api/session/screenshot", screenshot_session_handler)
app.router.add_post("/api/session/cancel", cancel_session_handler)

if __name__ == "__main__":
    web.run_app(app, host="0.0.0.0", port=int(os.getenv("PORT", "8080")))
