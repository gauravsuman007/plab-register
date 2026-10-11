"""HTTP API and web UI for plab-register: email-account creation and
PornoLab account registration/activation.

This service owns the only camoufox/patchright browser in the whole
deployment. Once a PornoLab account is registered (and, where the provider
supports it, activated), its credentials are handed to pornolab-seeder via
POST {SEEDER_URL}/api/accounts/import -- this service keeps no long-term
record of finished accounts, only of in-flight email-registry entries
created but not yet turned into a PornoLab account.
"""

import base64
import logging
import os
import random
import secrets
import string
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from . import camoufox_tasks as ct
from . import gmx as _gmx
from . import mail_ru as _mru
from . import parsing as _parsing
from . import tailscale as _ts
from . import temp_email as _te
from . import tuta as _tuta
from .pornolab_register import PornolabRegistrar, PornolabRegisterError, _solve_captcha as _ocr_solve
from .store import DB, SettingsStore

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("register")

HERE = Path(__file__).parent
APP_PASSWORD = os.environ.get("APP_PASSWORD", "")
SEEDER_URL = os.environ.get("SEEDER_URL", "http://pornolab-seeder:8000")

store = SettingsStore()
db = DB()

_register_pl = PornolabRegistrar(store.settings.pornolab_url)
_pending_register_form: _parsing.RegisterForm | None = None
_active_temp_address = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.event("info", "plab-register started")
    yield


app = FastAPI(title="plab-register", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")


@app.middleware("http")
async def basic_auth(request: Request, call_next):
    """Optional: set APP_PASSWORD to require it (any username) for the whole UI."""
    if APP_PASSWORD and request.url.path != "/healthz":
        header = request.headers.get("authorization", "")
        ok = False
        if header.startswith("Basic "):
            try:
                _, _, pw = base64.b64decode(header[6:]).decode().partition(":")
                ok = secrets.compare_digest(pw, APP_PASSWORD)
            except ValueError:
                ok = False
        if not ok:
            return Response(status_code=401, headers={"WWW-Authenticate": 'Basic realm="plab-register"'})
    return await call_next(request)


async def _body(request: Request) -> dict:
    try:
        return await request.json()
    except ValueError:
        return {}


def _fail(e: Exception, status: int = 400):
    raise HTTPException(status_code=status, detail=str(e))


@app.get("/")
async def index():
    return FileResponse(HERE / "templates" / "index.html")


@app.get("/healthz")
async def healthz():
    return {"ok": True}


@app.get("/api/settings")
async def get_settings():
    from dataclasses import asdict
    return asdict(store.settings)


@app.post("/api/settings")
async def post_settings(request: Request):
    b = await _body(request)
    store.update(**{k: v for k, v in b.items() if k in store.settings.__dataclass_fields__})
    return {"ok": True}


@app.get("/api/events")
async def events():
    return db.events()


# -------------------------------------------------------------------------- #
# tailscale -- identical pattern to pornolab-seeder's own
# -------------------------------------------------------------------------- #
@app.get("/api/tailscale/status")
async def tailscale_status():
    return await _ts.status()


@app.post("/api/tailscale/login")
async def tailscale_login():
    try:
        return await _ts.login()
    except _ts.TailscaleError as e:
        _fail(e, 502)


@app.post("/api/tailscale/logout")
async def tailscale_logout():
    await _ts.logout()
    return {"ok": True}


@app.post("/api/tailscale/exit-node")
async def tailscale_exit_node(request: Request):
    b = await _body(request)
    node_id = (b.get("node_id") or "").strip() or None
    try:
        await _ts.set_exit_node(node_id)
    except _ts.TailscaleError as e:
        _fail(e, 502)
    return await _ts.status()


# -------------------------------------------------------------------------- #
# PornoLab registration (manual flow: fetch form, solve captcha, submit)
# -------------------------------------------------------------------------- #
@app.get("/api/register/form")
async def register_form():
    """Fetch the PornoLab registration page and return the image captcha + lists."""
    global _pending_register_form
    try:
        form = await _register_pl.register_form()
    except PornolabRegisterError as e:
        _fail(e, 502)
    _pending_register_form = form
    rendered = await _render_register_form(form)
    rendered.pop("captcha_bytes", None)
    return rendered


async def _render_register_form(form: _parsing.RegisterForm) -> dict:
    img_data = None
    captcha_text = None
    img_bytes = None
    if form.captcha_url:
        try:
            async with httpx.AsyncClient(timeout=15) as _cap_client:
                for c in form.browser_cookies:
                    _cap_client.cookies.set(c["name"], c["value"], domain=c.get("domain", "").lstrip("."))
                img = await _cap_client.get(
                    form.captcha_url,
                    headers={"Referer": f"{_register_pl.base}/forum/profile.php?mode=register"},
                )
            mime = img.headers.get("content-type", "image/png").split(";")[0]
            if mime.startswith("image/"):
                img_bytes = img.content
                img_data = f"data:{mime};base64,{base64.b64encode(img.content).decode()}"
                try:
                    captcha_text = _ocr_solve(img.content)
                except Exception as e:
                    log.warning("captcha OCR failed: %s", e)
        except Exception:
            pass
    return {
        "captcha": img_data,
        "captcha_text": captcha_text,
        "captcha_bytes": img_bytes,
        "countries": form.countries,
        "timezones": form.timezones,
        "turnstile_solved": bool(form.turnstile_token),
    }


@app.post("/api/register")
async def register(request: Request):
    global _pending_register_form
    b = await _body(request)
    if not _pending_register_form:
        _fail(ValueError("fetch the registration form first (/api/register/form)"))
    try:
        result = await _register_pl.register(
            username=b.get("username", "").strip(),
            password=b.get("password", ""),
            email=b.get("email", "").strip(),
            captcha_code=b.get("captcha", ""),
            form=_pending_register_form,
            country=str(b.get("country", "0")),
            timezone=str(b.get("timezone", "100")),
        )
    except PornolabRegisterError as e:
        _fail(e, 502)
    if result["ok"]:
        _pending_register_form = None
    return result


def _is_captcha_error(error: str) -> bool:
    return "код подтверждения" in (error or "")


def _rand_username(n: int = 10) -> str:
    return "pl_" + "".join(random.choices(string.ascii_lowercase + string.digits, k=n))


def _rand_password(n: int = 16) -> str:
    chars = string.ascii_letters + string.digits
    pw = list(random.choices(chars, k=n))
    pw[0] = random.choice(string.ascii_uppercase)
    pw[1] = random.choice(string.ascii_lowercase)
    pw[2] = random.choice(string.digits)
    pw[3] = random.choice(string.digits)
    random.shuffle(pw)
    return "".join(pw)


async def _import_into_seeder(*, pornolab_username: str, pornolab_password: str,
                               email_address: str, email_password: str, email_provider: str) -> dict:
    """Hand a finished registration to pornolab-seeder, which is the system
    of record for accounts. Protected by the same APP_PASSWORD Basic-auth
    scheme pornolab-seeder's own UI uses -- this service is configured with
    the same password, no separate secret needed."""
    auth = None
    if APP_PASSWORD:
        auth = ("register", APP_PASSWORD)
    async with httpx.AsyncClient(timeout=30, auth=auth) as c:
        r = await c.post(f"{SEEDER_URL.rstrip('/')}/api/accounts/import", json={
            "label": pornolab_username,
            "pornolab_username": pornolab_username,
            "pornolab_password": pornolab_password,
            "email_address": email_address,
            "email_password": email_password,
            "email_provider": email_provider,
        })
        r.raise_for_status()
        return r.json()


@app.post("/api/register/auto")
async def register_auto(request: Request):
    """Create an email, register a PornoLab account with it, and (where the
    email provider supports it) activate it automatically. Captcha/username
    failures are retried with fresh values, up to 10 attempts total."""
    b = await _body(request)
    provider = (b.get("provider") or store.settings.email_factory_provider or "tuta").lower()

    if provider == "tuta":
        try:
            email, email_password = await _tuta.TutaFactory().create()
        except _tuta.TutaError as e:
            _fail(e, 502)
    elif provider == "gmx":
        factory = _gmx.GmxFactory(provider=store.settings.gmx_provider or "web.de")
        try:
            email, email_password = await factory.create()
        except _gmx.GmxError as e:
            _fail(e, 502)
    elif provider == "mailru":
        key = store.settings.sms_activate_api_key
        if not key:
            _fail(ValueError("sms_activate_api_key not set in settings"))
        factory = _mru.MailRuFactory(sms_api_key=key, domain=store.settings.mailru_domain or "mail.ru")
        try:
            email, email_password = await factory.create()
        except (_mru.MailRuError, _mru.SmsActivateError) as e:
            _fail(e, 502)
    else:
        _fail(ValueError(f"unknown provider {provider!r}"))
        return  # unreachable, keeps type checkers happy

    return await _register_pornolab_with_email(email, email_password, provider)


async def _register_pornolab_with_email(email: str, email_password: str, provider: str) -> dict:
    """Register a new PornoLab account against an already-created inbox,
    retrying on captcha/username failures, then hand it to pornolab-seeder
    and (for providers with automated inbox reading) activate it."""
    username = _rand_username()
    password = _rand_password()
    last_error = None
    result = None
    for attempt in range(10):
        try:
            form = await _register_pl.register_form()
        except PornolabRegisterError as e:
            last_error = str(e)
            continue
        rendered = await _render_register_form(form)
        captcha_text = rendered.get("captcha_text") or ""
        try:
            result = await _register_pl.register(
                username=username, password=password, email=email,
                captcha_code=captcha_text, form=form, country="0", timezone="100",
            )
        except PornolabRegisterError as e:
            last_error = str(e)
            continue
        if result.get("ok"):
            break
        last_error = result.get("error")
        if not _is_captcha_error(last_error):
            username = _rand_username()   # likely "username taken" or similar -- try a new one too
    else:
        _fail(ValueError(f"registration failed after 10 attempts: {last_error}"), 502)

    if not (result and result.get("ok")):
        _fail(ValueError(f"registration failed: {last_error}"), 502)

    activation_url = None
    if provider == "tuta":
        try:
            activation_url = await _tuta.TutaFactory().find_activation(email, email_password, timeout=150)
            if activation_url:
                async with httpx.AsyncClient(timeout=20) as c:
                    await c.get(activation_url)
        except _tuta.TutaError as e:
            log.warning("auto-activation failed for %s: %s", username, e)

    try:
        imported = await _import_into_seeder(
            pornolab_username=username, pornolab_password=password,
            email_address=email, email_password=email_password, email_provider=provider,
        )
        account_id = imported.get("account_id")
    except httpx.HTTPError as e:
        log.error("handoff to pornolab-seeder failed for %s: %s", username, e)
        db.event("error", f"registered {username} / {email} but handoff to pornolab-seeder failed: {e}")
        _fail(ValueError(f"registered the PornoLab account but could not hand it to pornolab-seeder: {e}"), 502)

    db.event("info", f"Account registered: {username} / {email}"
             + (" (activated, imported)" if activation_url else " (activation pending -- check inbox, imported)"))
    return {
        "ok": True, "account_id": account_id, "username": username, "password": password,
        "email": email, "email_password": email_password, "provider": provider,
        "activated": bool(activation_url),
    }


# -------------------------------------------------------------------------- #
# throwaway temp-email inbox (no PornoLab account involved)
# -------------------------------------------------------------------------- #
@app.get("/api/tempemail/providers")
async def tempemail_providers():
    return {"providers": _te.provider_names()}


@app.post("/api/tempemail/new")
async def tempemail_new(request: Request):
    global _active_temp_address
    b = await _body(request)
    provider = b.get("provider") or None
    try:
        addr = await _te.new_address(provider)
    except _te.ProviderError as e:
        _fail(e, 502)
    _active_temp_address = addr
    return {"address": addr.address, "provider": addr.provider}


@app.get("/api/tempemail/inbox")
async def tempemail_inbox():
    if not _active_temp_address:
        _fail(ValueError("no active temp address - call /api/tempemail/new first"))
    try:
        msgs = await _active_temp_address.inbox()
    except _te.ProviderError as e:
        _fail(e, 502)
    return {
        "address": _active_temp_address.address,
        "provider": _active_temp_address.provider,
        "messages": [
            {"id": m.id, "from": m.from_addr, "subject": m.subject,
             "body_text": m.body_text, "body_html": m.body_html}
            for m in msgs
        ],
    }


@app.get("/api/tempemail/message/{msg_id}")
async def tempemail_message(msg_id: str):
    if not _active_temp_address:
        _fail(ValueError("no active temp address"))
    try:
        m = await _active_temp_address.message(msg_id)
    except _te.ProviderError as e:
        _fail(e, 502)
    return {"id": m.id, "from": m.from_addr, "subject": m.subject,
            "body_text": m.body_text, "body_html": m.body_html}


# -------------------------------------------------------------------------- #
# standalone email registry -- create/inspect/discard email accounts across
# all supported providers, independent of any PornoLab registration.
# -------------------------------------------------------------------------- #
_EMAIL_PROVIDERS = ["tuta", "gmx", "atomicmail"]  # mail.ru needs a phone number via sms-activate -- excluded


@app.get("/api/emails/providers")
async def emails_providers():
    return {"providers": _EMAIL_PROVIDERS}


@app.get("/api/emails")
async def emails_list():
    return db.list_emails()


@app.post("/api/emails/new")
async def emails_new(request: Request):
    """Create a new email account with the given provider and bank it in the
    emails registry. atomicmail.io needs a human to clear its hCaptcha and
    goes through /api/atomicmail/signup/* instead."""
    b = await _body(request)
    provider = (b.get("provider") or "").lower()
    if provider == "tuta":
        try:
            address, password = await _tuta.TutaFactory().create()
        except _tuta.TutaError as e:
            _fail(e, 502)
    elif provider == "gmx":
        try:
            address, password = await _gmx.GmxFactory(provider=store.settings.gmx_provider or "web.de").create()
        except _gmx.GmxError as e:
            _fail(e, 502)
    elif provider == "mailru":
        _fail(ValueError("mail.ru needs a phone number for signup - not offered here"))
        return  # unreachable
    elif provider == "atomicmail":
        _fail(ValueError("atomicmail.io needs a human to clear its hCaptcha - "
                          "use POST /api/atomicmail/signup/start instead"))
        return  # unreachable
    else:
        _fail(ValueError(f"unknown provider {provider!r}"))
        return  # unreachable

    email_id = db.create_email(provider=provider, address=address, password=password)
    return {"ok": True, "id": email_id, "provider": provider, "address": address, "password": password}


async def _list_inbox(provider: str, address: str, password: str) -> list[dict]:
    if provider == "tuta":
        return await _tuta.TutaFactory().list_inbox(address, password)
    if provider == "gmx":
        return await _gmx.gmx_list_inbox(address, password)
    if provider == "mailru":
        return await _mru.mailru_list_inbox(address, password)
    raise ValueError(f"inbox listing is not supported for provider {provider!r}")


@app.get("/api/emails/{email_id}/credentials")
async def email_credentials(email_id: int):
    row = db.get_email(email_id)
    if not row:
        _fail(ValueError(f"unknown email id {email_id}"), 404)
    return {"id": email_id, "address": row["address"], "password": row["password"], "provider": row["provider"]}


@app.get("/api/emails/{email_id}/inbox")
async def emails_inbox(email_id: int):
    row = db.get_email(email_id)
    if not row:
        _fail(ValueError(f"unknown email id {email_id}"), 404)
    try:
        messages = await _list_inbox(row["provider"], row["address"], row["password"])
    except ValueError as e:
        _fail(e, 502)
    return {"ok": True, "messages": messages}


@app.delete("/api/emails/{email_id}")
async def emails_delete(email_id: int):
    """Drop an email from the registry (e.g. the owner found it can no
    longer log in). This only forgets it locally -- it does not reach out to
    the provider to delete the remote account."""
    row = db.get_email(email_id)
    if not row:
        _fail(ValueError(f"unknown email id {email_id}"), 404)
    db.delete_email(email_id)
    return {"ok": True}


@app.post("/api/emails/{email_id}/register-pornolab")
async def emails_register_pornolab(email_id: int):
    """Take an already-created email from the registry and run it through
    the PornoLab registration pipeline, same as /api/register/auto does for
    a freshly-created one. On success the email is removed from the
    standalone registry since it now lives on the new PornoLab account."""
    row = db.get_email(email_id)
    if not row:
        _fail(ValueError(f"unknown email id {email_id}"), 404)
    result = await _register_pornolab_with_email(row["address"], row["password"], row["provider"])
    if result.get("ok"):
        db.delete_email(email_id)
    return result


# -------------------------------------------------------------------------- #
# atomicmail.io -- interactive hCaptcha relay
# -------------------------------------------------------------------------- #
@app.post("/api/atomicmail/signup/start")
async def atomicmail_signup_start():
    d = await ct.atomicmail_signup_start()
    if not d.ok:
        _fail(ValueError(d.error or "atomicmail signup failed to start"), 502)
    return d


@app.post("/api/atomicmail/signup/click")
async def atomicmail_signup_click(request: Request):
    b = await _body(request)
    session_id = b.get("session_id") or ""
    if not session_id:
        _fail(ValueError("session_id is required"))
    d = await ct.atomicmail_signup_click(ct.AtomicmailClickRequest(
        session_id=session_id, x=b.get("x"), y=b.get("y"),
    ))
    if not d.ok:
        _fail(ValueError(d.error or "click failed"), 502)
    return d


@app.post("/api/atomicmail/signup/verify")
async def atomicmail_signup_verify(request: Request):
    b = await _body(request)
    session_id = b.get("session_id") or ""
    if not session_id:
        _fail(ValueError("session_id is required"))
    d = await ct.atomicmail_signup_verify(ct.AtomicmailVerifyRequest(session_id=session_id))
    if not d.ok and d.done:
        _fail(ValueError(d.error or "verify failed"), 502)
    return d


@app.post("/api/atomicmail/signup/cancel")
async def atomicmail_signup_cancel(request: Request):
    b = await _body(request)
    session_id = b.get("session_id") or ""
    if session_id:
        await ct.atomicmail_signup_cancel(session_id)
    return {"ok": True}


@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception):
    log.exception("unhandled error on %s %s", request.method, request.url.path)
    from fastapi.responses import JSONResponse
    return JSONResponse(status_code=500, content={"detail": f"{type(exc).__name__}: {exc}"})
