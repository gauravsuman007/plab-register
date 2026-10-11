"""PornoLab registration-form parsing. Pure functions over HTML, testable
offline. Split out of pornolab-seeder's app/parsing.py -- this repo only
needs the registration-specific pieces; profile/listing parsing stays with
the seeder, which is the only thing that reads those pages.

PornoLab is a TorrentPier forum served as windows-1251.
"""

from dataclasses import dataclass, field

from bs4 import BeautifulSoup


def site_message(html: str) -> str | None:
    """The text of a TorrentPier message box ('download limit reached', ...)."""
    soup = BeautifulSoup(html, "html.parser")
    if el := soup.select_one("table.message .mrg_16") or soup.select_one("table.message td"):
        return el.get_text(" ", strip=True)
    return None


@dataclass
class RegisterForm:
    captcha_url: str | None = None
    cap_sid: str | None = None
    cap_field: str | None = None          # per-session "cap_code_<x>" input name
    turnstile_token: str | None = None    # cf-turnstile-response, filled by the headless browser
    countries: list[tuple[str, str]] = field(default_factory=list)   # (value, label)
    timezones: list[tuple[str, str]] = field(default_factory=list)   # (value, label)
    # Cookies from the browser session that fetched the form -- kept separate
    # from any logged-in session so the register POST goes out as anonymous.
    browser_cookies: list[dict] = field(default_factory=list)


def parse_register_form(html: str) -> RegisterForm:
    soup = BeautifulSoup(html, "html.parser")
    form = RegisterForm()
    if img := soup.select_one('img[src*="/captcha/"]'):
        src = img.get("src", "")
        form.captcha_url = "https:" + src if src.startswith("//") else src
    if sid := soup.select_one('input[name="cap_sid"]'):
        form.cap_sid = sid.get("value")
    if code := soup.select_one('input[name^="cap_code_"]'):
        form.cap_field = code.get("name")
    if ts := soup.select_one('input[name="cf-turnstile-response"]'):
        v = ts.get("value", "")
        if v:
            form.turnstile_token = v
    if sel := soup.select_one('select[name="user_flag_id"]'):
        form.countries = [(o.get("value", ""), o.get_text(strip=True)) for o in sel.select("option")]
    if sel := soup.select_one('select[name="user_timezone_x2"]'):
        form.timezones = [(o.get("value", ""), o.get_text(strip=True)) for o in sel.select("option")]
    return form


def parse_register_error(html: str) -> str | None:
    """Return the site's registration error message, if any."""
    soup = BeautifulSoup(html, "html.parser")
    # div.msg is used for field-level validation errors (e.g. "wrong captcha").
    if el := soup.select_one("div.msg"):
        return el.get_text(" ", strip=True)
    # .warnColor1 can carry errors, but skip legend elements -- they are TOS
    # section headers on the registration form, not error indicators.
    for sel in ("h4.warnColor1", "span.warnColor1", "div.warnColor1", "p.warnColor1"):
        if el := soup.select_one(sel):
            return el.get_text(" ", strip=True)
    for el in soup.select(".warnColor1"):
        if el.name != "legend":
            return el.get_text(" ", strip=True)
    # Generic site message box (download-limit page, etc.)
    if msg := site_message(html):
        return msg
    return None


def register_success(html: str) -> bool:
    """True if the registration success page is shown."""
    return "Письмо с инструкцией по активации" in html or "activation" in html.lower()
