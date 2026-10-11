"""PornoLab registration: clear the registration page's Cloudflare Turnstile
widget, solve the image captcha, and submit the form.

Split out of pornolab-seeder's app/pornolab.py -- this is the only part of
that module that needs a real browser; login/profile/listing/download stay
in the seeder and talk to PornoLab over plain httpx.
"""

import logging
import random
import time

import httpx

from . import camoufox_tasks as ct
from . import parsing

log = logging.getLogger("register")

UA = "Mozilla/5.0 (X11; Linux x86_64; rv:130.0) Gecko/20100101 Firefox/130.0"


class PornolabRegisterError(Exception):
    pass


def _solve_captcha(img_bytes: bytes) -> str:
    """Solve a TorrentPier image captcha.

    Uses Claude's vision API when ANTHROPIC_API_KEY is set (more accurate).
    Falls back to ddddocr with both standard and beta models.
    """
    import os
    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if api_key:
        return _solve_captcha_claude(img_bytes, api_key)
    return _solve_captcha_ocr(img_bytes)


def _solve_captcha_claude(img_bytes: bytes, api_key: str) -> str:
    """Use Claude's vision API to read the captcha text."""
    import base64 as _b64
    import urllib.request, urllib.error, json as _json
    img_b64 = _b64.b64encode(img_bytes).decode()
    body = _json.dumps({
        "model": "claude-haiku-4-5-20251001",
        "max_tokens": 20,
        "messages": [{
            "role": "user",
            "content": [
                {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": img_b64}},
                {"type": "text", "text": "Output ONLY the alphanumeric characters shown in this captcha image, nothing else. Ignore the 'pornolab.net' watermark at the bottom."},
            ],
        }],
    }).encode()
    req = urllib.request.Request(
        "https://api.anthropic.com/v1/messages",
        data=body,
        headers={
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            result = _json.loads(resp.read())
            text = result["content"][0]["text"].strip().lower()
            import re
            return re.sub(r"[^a-z0-9]", "", text)
    except Exception:
        return _solve_captcha_ocr(img_bytes)


def _solve_captcha_ocr(img_bytes: bytes) -> str:
    """ddddocr fallback: run both models, prefer the longer result."""
    import ddddocr as _ddddocr
    ocr_std = _ddddocr.DdddOcr(show_ad=False)
    ocr_beta = _ddddocr.DdddOcr(show_ad=False, beta=True)
    r_std = ocr_std.classification(img_bytes)
    r_beta = ocr_beta.classification(img_bytes)
    return r_beta if len(r_beta) >= len(r_std) else r_std


class PornolabRegistrar:
    def __init__(self, base_url: str):
        self.base = base_url.rstrip("/")

    @staticmethod
    def _text(r: httpx.Response) -> str:
        return r.content.decode("cp1251", errors="replace")

    # -- Turnstile clearing -------------------------------------------------- #
    #
    # Two stages, tried in order: camoufox (in-process, fast, arm64-native --
    # the default and normally-sufficient path), then patchright+Xvfb as a
    # fallback safety net. On arm64 the patchright fallback does NOT clear
    # the current Turnstile ruleset (confirmed 2026-10-10: /pat/ -> 401), but
    # is kept in case camoufox itself becomes unavailable.

    async def _clear_turnstile(self) -> tuple[str, list[dict]]:
        stages = [self._stage_camoufox, self._stage_xvfb]
        last_err: Exception | None = None
        for stage in stages:
            name = stage.__name__.removeprefix("_stage_")
            try:
                log.info("Turnstile: trying stage %s", name)
                html, cookies, token = await stage()
            except Exception as e:                               # noqa: BLE001
                last_err = e
                log.warning("Turnstile stage %s raised %s: %s", name, type(e).__name__, e)
                continue
            if token:
                log.info("Turnstile: stage %s cleared the widget", name)
                return html, cookies
            log.warning("Turnstile: stage %s loaded the form but never got a token", name)
        if last_err:
            raise PornolabRegisterError(f"could not reach the registration form: {last_err}")
        raise PornolabRegisterError("Turnstile widget never populated cf-turnstile-response")

    async def _stage_camoufox(self):
        """In-process camoufox: this is the same recipe the old HTTP sidecar
        used, just called directly (see camoufox_tasks.solve). The agree POST
        is done via camoufox's own request API (inside the browser session)
        so the server-side TOS-agreed flag is set before navigating to the
        registration form -- httpx can't do this because the anonymous `plm`
        cookie has a constant value ("1") that doesn't uniquely identify the
        session, so the server-side agreement state can't be read back by a
        different process."""
        req = ct.SolveRequest(
            url=f"{self.base}/forum/profile.php?mode=register",
            agree_click='a[onclick*="go-to-reg"]',
            widget_selector=".cf-turnstile",
            max_timeout=90,
        )
        d = await ct.solve(req)
        return d.html, d.cookies, d.token

    # Launch flags for the patchright fallback. --enable-unsafe-swiftshader
    # turns on software WebGL (without it Chromium reports "No available
    # adapters", a hard Cloudflare fingerprint fail). The rest hides the
    # automation surface.
    _LAUNCH_ARGS = [
        "--no-sandbox",
        "--disable-blink-features=AutomationControlled",
        "--enable-unsafe-swiftshader",
        "--use-gl=angle",
        "--use-angle=swiftshader",
        "--disable-dev-shm-usage",
    ]
    _CTX_UA = (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/129.0.0.0 Safari/537.36"
    )
    _CTX_VIEWPORT = {"width": 1366, "height": 768}

    async def _stage_xvfb(self):
        from patchright.async_api import async_playwright
        from pyvirtualdisplay import Display
        display = Display(visible=0, size=(1920, 1080))
        display.start()
        try:
            async with async_playwright() as pw:
                browser = await pw.chromium.launch(headless=False, args=self._LAUNCH_ARGS)
                try:
                    ctx = await browser.new_context(
                        viewport=self._CTX_VIEWPORT, user_agent=self._CTX_UA,
                        locale="en-US", timezone_id="Europe/Berlin",
                    )
                    page = await ctx.new_page()
                    reg_url = f"{self.base}/forum/profile.php?mode=register"

                    async def _block(route):
                        host = route.request.url.split("/", 3)[2].lower()
                        if any(bad in host for bad in ("vpipi.com", "dynspt.com", "doubleclick", "googlesyndication")):
                            await route.abort()
                        else:
                            await route.continue_()
                    await page.route("**/*", _block)

                    await page.goto(reg_url, wait_until="load")
                    await page.click('a[onclick*="go-to-reg"]')
                    await page.wait_for_load_state("load")

                    try:
                        await page.wait_for_timeout(4000)
                        iframe = await page.query_selector('iframe[src*="challenges.cloudflare.com"]')
                        if iframe:
                            box = await iframe.bounding_box()
                            if box:
                                x = box["x"] + 30 + random.uniform(-2, 2)
                                y = box["y"] + box["height"] / 2 + random.uniform(-2, 2)
                                await page.mouse.move(x - 50, y - 10, steps=10)
                                await page.mouse.move(x, y, steps=8)
                                await page.mouse.click(x, y, delay=90)
                    except Exception:
                        pass

                    token = None
                    for _ in range(45):
                        field = await page.query_selector('input[name="cf-turnstile-response"]')
                        token = await field.input_value() if field else None
                        if token:
                            break
                        await page.wait_for_timeout(1000)
                    html = await page.content()
                    cookies = await page.context.cookies()
                    return html, cookies, token
                finally:
                    await browser.close()
        finally:
            display.stop()

    # -- registration form / submit ------------------------------------------ #

    async def register_form(self) -> parsing.RegisterForm:
        """Fetch the registration form, Turnstile token, and image captcha.

        The browser (camoufox/Xvfb) navigates to the TOS page, clicks "I
        agree", and lands on the full registration form with Turnstile
        already solved. That browser session is already in "agreed" state
        server-side, so we use its HTML and cookies directly -- no
        additional agree POST needed.
        """
        html, reg_cookies = await self._clear_turnstile()
        form = parsing.parse_register_form(html)
        if not form.turnstile_token:
            log.warning("headless browser did not produce a Turnstile token; submission may fail")
        if not form.cap_sid:
            raise PornolabRegisterError("browser registration form has no cap_sid")
        form.browser_cookies = reg_cookies
        return form

    async def register(
        self,
        username: str,
        password: str,
        email: str,
        captcha_code: str,
        form: parsing.RegisterForm,
        country: str = "0",
        timezone: str = "100",
    ) -> dict:
        """Submit the registration form. Returns {"ok": True} or {"error": "..."}."""
        if not form.cap_field or not form.cap_sid:
            raise PornolabRegisterError("registration form not initialised -- fetch the form first")
        data: dict[str, str] = {
            "mode": "register",
            "reg_agreed": "1",
            "username": username,
            "new_pass": password,
            "cfm_pass": password,
            "user_email": email,
            "cap_sid": form.cap_sid,
            form.cap_field: captcha_code,
            "user_flag_id": country,
            "user_timezone_x2": timezone,
            "submit": "Отправить (Я согласен с условиями)",
        }
        if form.turnstile_token:
            data["cf-turnstile-response"] = form.turnstile_token
        # multipart/form-data in the site's own charset (windows-1251), and a
        # fresh cookieless client so no logged-in session leaks into this
        # anonymous registration POST.
        multipart_files = {
            k: (None, v.encode("cp1251"), "text/plain; charset=windows-1251")
            for k, v in data.items()
        }
        reg_client = httpx.AsyncClient(
            headers={"User-Agent": UA, "Accept-Language": "ru,en;q=0.8"},
            timeout=30,
            follow_redirects=True,
        )
        for c in getattr(form, "browser_cookies", []):
            reg_client.cookies.set(c["name"], c["value"], domain=c.get("domain", "").lstrip("."))
        async with reg_client:
            r = await reg_client.post(
                f"{self.base}/forum/profile.php",
                files=multipart_files,
                headers={"Referer": f"{self.base}/forum/profile.php?mode=register"},
            )
        html = self._text(r)
        if parsing.register_success(html):
            return {"ok": True}
        err = parsing.parse_register_error(html)
        return {"ok": False, "error": err or "Registration failed - unknown error", "raw_html": html}
