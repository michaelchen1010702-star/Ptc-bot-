#!/usr/bin/env python3
"""Pokemon Trainer Club account creator.

join.pokemon.com 4-step wizard -> Imperva/Incapsula challenge -> submit ->
emailed 6-digit activation code -> Google Sheet row.
"""

import os
import re
import sys
import time
import random
import string
import traceback

import requests
from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

import emailer

# ---------------------------------------------------------------- config

JOIN_URL = "https://join.pokemon.com/"
API_URL = "https://api.join.pokemon.com/"
PROFILE_DIR = os.path.expanduser("~/.ptc-profile")
DEBUG_DIR = "debug"
os.makedirs(DEBUG_DIR, exist_ok=True)

COUNT = int(os.environ.get("COUNT", "1") or "1")
HEADLESS = os.environ.get("HEADLESS", "0") == "1"
SHEETS_URL = (os.environ.get("SHEETS_URL") or "").strip()
SHEETS_TOKEN = (os.environ.get("SHEETS_TOKEN") or "").strip()

UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/156.0.0.0 Safari/537.36"
)

CHROME_ARGS = [
    "--no-sandbox",
    "--disable-dev-shm-usage",
    "--disable-blink-features=AutomationControlled",
    "--disable-backgrounding-occluded-windows",
    "--disable-renderer-backgrounding",
    "--disable-background-timer-throttling",
    "--enable-unsafe-swiftshader",
    "--ignore-gpu-blocklist",
    "--window-size=1440,900",
    "--lang=en-US",
    "--no-first-run",
    "--no-default-browser-check",
]

SYMBOLS = "!@#$%^&*"
MONTH_NAMES = [
    "January", "February", "March", "April", "May", "June",
    "July", "August", "September", "October", "November", "December",
]

AGE_SELECTORS = (
    "#ageGateSubmit",
    '[name="ageGateSubmit"]',
    "#country-select",
    '[name="country-select"]',
    "#year-select",
    '[name="year-select"]',
    "#month-select",
    '[name="month-select"]',
)

NET = {"create_user": [], "validate": []}


# ---------------------------------------------------------------- utils

def log(msg):
    print(msg, flush=True)


def safe_text(page):
    try:
        return re.sub(r"\s+", " ", page.inner_text("body"))
    except Exception:
        return ""


def base_domain(host):
    parts = host.split(".")
    return ".".join(parts[-2:]) if len(parts) >= 2 else host


def snap(page, tag):
    try:
        page.screenshot(path=os.path.join(DEBUG_DIR, f"{tag}.png"), full_page=True)
    except Exception:
        pass
    try:
        with open(os.path.join(DEBUG_DIR, f"{tag}.html"), "w", encoding="utf-8") as f:
            f.write(page.content())
    except Exception:
        pass


def dump_shell(page):
    try:
        html = page.content()
        flat = re.sub(r"\s+", " ", html)
        log(f"html[{len(html)}B] {flat[:1500]}")
    except Exception as e:
        log(f"html err {e}")


def row(email, username, password, status, note=""):
    return {
        "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
        "email": email,
        "username": username,
        "password": password,
        "status": status,
        "note": note,
    }


def sheet_push(r):
    if not SHEETS_URL:
        log("sheet: SHEETS_URL unset")
        return
    for _ in range(4):
        try:
            resp = requests.post(
                SHEETS_URL, json={"token": SHEETS_TOKEN, "row": r}, timeout=30
            )
            if resp.ok:
                log(f"sheet: ok:1 ({r['status']})")
                return
            log(f"sheet: {resp.status_code} {resp.text[:120]}")
        except Exception as e:
            log(f"sheet err: {e}")
        time.sleep(2)
    log("sheet: FAILED")


def rand_user():
    return "ptc" + "".join(
        random.choice(string.ascii_lowercase + string.digits) for _ in range(9)
    )


def rand_pass():
    while True:
        p = (
            random.choice(string.ascii_lowercase)
            + random.choice(string.ascii_uppercase)
            + random.choice(string.digits)
            + random.choice(SYMBOLS)
            + "".join(random.choice(string.ascii_letters + string.digits) for _ in range(6))
        )
        if 8 <= len(p) <= 16:
            return p


def _prep(s):
    s = re.sub(r"#[0-9a-fA-F]{3,8}\b", " ", s)
    s = re.sub(r"rgba?\([^)]*\)", " ", s)
    s = re.sub(r"<[^>]+>", " ", s)
    s = re.sub(r"&[a-zA-Z]+;", " ", s)
    return s


def extract_code(body):
    t = _prep(body)
    for pat in (r"(?i)code[^0-9]{0,25}(\d{6})", r"(?<!\d)(\d{6})(?!\d)"):
        m = re.search(pat, t)
        if m:
            return m.group(1)
    return None


# ---------------------------------------------------------------- diagnostics

def hook_diag(page):
    def on_console(msg):
        if msg.type in ("error", "warning"):
            loc = msg.location or {}
            log(f"[console:{msg.type}] {msg.text[:180]} @ {loc.get('url', '')[:130]}")

    def on_pageerror(err):
        log(f"[pageerror] {str(err)[:220]}")

    def on_request(req):
        u = req.url
        if "api.join.pokemon.com" in u:
            try:
                body = req.post_data or ""
            except Exception:
                body = ""
            log(f"[req] {req.method} {u[:110]} {body[:320]}")

    def on_response(resp):
        u = resp.url
        if "create-user" in u:
            body = ""
            try:
                body = (resp.text() or "")[:400]
            except Exception:
                pass
            NET["create_user"].append({"status": resp.status, "body": body})
            log(f"[resp {resp.status}] create-user body={body}")
        elif "validate-user-fields" in u:
            body = ""
            try:
                body = (resp.text() or "")[:300]
            except Exception:
                pass
            NET["validate"].append({"status": resp.status, "body": body})
            log(f"[resp {resp.status}] validate body={body}")
        elif "start-activation" in u:
            body = ""
            try:
                body = (resp.text() or "")[:300]
            except Exception:
                pass
            log(f"[resp {resp.status}] start-activation body={body}")
        elif "api.join.pokemon.com" in u:
            log(f"[resp {resp.status}] api {u[:110]}")
        elif "_Incapsula" in u or "recaptcha" in u or "hcaptcha" in u:
            log(f"[resp {resp.status}] {u[:170]}")

    def on_failed(req):
        log(f"[reqfail] {req.failure} {req.url[:150]}")

    page.on("console", on_console)
    page.on("pageerror", on_pageerror)
    page.on("request", on_request)
    page.on("response", on_response)
    page.on("requestfailed", on_failed)


# ---------------------------------------------------------------- navigation

def goto(page, url, timeout=60000):
    for wu in ("load", "domcontentloaded", "commit"):
        try:
            page.goto(url, wait_until=wu, timeout=timeout)
            return True
        except PWTimeout:
            continue
        except Exception:
            continue
    return False


# ---------------------------------------------------------------- step detection

def form_present(page):
    for s in ('#email-field', '[name="email-field"]'):
        try:
            l = page.locator(s).first
            if l.count() and l.is_visible():
                return True
        except Exception:
            pass
    return False


def age_gate_present(page):
    for s in AGE_SELECTORS:
        try:
            l = page.locator(s).first
            if l.count() and l.is_visible():
                return True
        except Exception:
            pass
    t = safe_text(page).lower()
    return "age verification" in t or "date of birth" in t


def page_ready(page):
    return form_present(page) or age_gate_present(page)


def wait_for_ready(page, patience=45):
    end = time.time() + patience
    while time.time() < end:
        if page_ready(page):
            return True
        time.sleep(1)
    return False


def bust_challenge(page):
    for fr in page.frames:
        if "_Incapsula_Resource" in fr.url:
            log("challenge bust: visiting Incapsula resource")
            goto(page, fr.url)
            time.sleep(2)
            goto(page, JOIN_URL)
            return True
    return False


def get_ready(page, tries=2, patience=45):
    for t in range(tries):
        log(f"get_ready attempt {t + 1}/{tries}")
        goto(page, JOIN_URL)
        if wait_for_ready(page, patience):
            log(
                f"page ready | age_gate={age_gate_present(page)} "
                f"account_form={form_present(page)}"
            )
            return True

        log(f"blank page url={page.url} text='{safe_text(page)[:70]}'")
        dump_shell(page)
        log(f"frames: {[f.url[:75] for f in page.frames]}")

        if bust_challenge(page) and wait_for_ready(page, patience):
            log("challenge bust worked")
            return True
    return False


# ---------------------------------------------------------------- form helpers

def click_text(page, text, timeout=15000):
    end = time.time() + timeout / 1000.0
    while time.time() < end:
        for role in ("button", "link"):
            try:
                loc = page.get_by_role(role, name=re.compile(text, re.I)).first
                if loc.count() and loc.is_visible():
                    loc.click()
                    return True
            except Exception:
                pass
        try:
            loc = page.get_by_text(re.compile(text, re.I)).first
            if loc.count() and loc.is_visible():
                loc.click()
                return True
        except Exception:
            pass
        time.sleep(0.4)
    return False


def type_field(page, name, value, human=True):
    loc = None
    for s in (f'#{name}', f'[name="{name}"]'):
        try:
            l = page.locator(s).first
            if l.count() and l.is_visible():
                loc = l
                break
        except Exception:
            pass
    if loc is None:
        return False
    loc.click()
    try:
        loc.fill("")
    except Exception:
        pass
    if human and value:
        page.keyboard.type(value, delay=random.randint(30, 70))
    elif value:
        loc.fill(value)
    return True


def field_value(page, name):
    for s in (f'#{name}', f'[name="{name}"]'):
        try:
            l = page.locator(s).first
            if l.count():
                return l.input_value()
        except Exception:
            pass
    return ""


def check_label(page, name):
    for s in (f'input[name="{name}"]', f'input[id="{name}"]'):
        try:
            l = page.locator(s).first
            if l.count():
                if not l.is_checked():
                    l.check(force=True)
                return True
        except Exception:
            pass
    for txt in ("Terms", "I agree", "agree"):
        try:
            l = page.get_by_label(re.compile(txt, re.I)).first
            if l.count():
                if not l.is_checked():
                    l.check(force=True)
                return True
        except Exception:
            pass
    return False


def select_any(page, name, values=(), labels=(), index=None):
    for s in (f'[name="{name}"]', f'#{name}'):
        try:
            l = page.locator(s).first
            if not l.count():
                continue
            for v in values:
                try:
                    l.select_option(value=v)
                    return True
                except Exception:
                    pass
            for lb in labels:
                try:
                    l.select_option(label=lb)
                    return True
                except Exception:
                    pass
            if index is not None:
                try:
                    l.select_option(index=index)
                    return True
                except Exception:
                    pass
        except Exception:
            pass
    return False


def do_age_gate(page):
    if form_present(page):
        return True

    log("age gate: filling")
    select_any(page, "country-select", values=["US"], labels=["United States"])

    y = random.randint(1990, 2004)
    m = random.randint(1, 12)
    d = random.randint(1, 28)

    if not select_any(page, "year-select", values=[str(y)], labels=[str(y)]):
        log("age gate: year select failed")
    if not select_any(
        page,
        "month-select",
        values=[f"{m:02d}", str(m)],
        labels=[MONTH_NAMES[m - 1], f"{m:02d}", str(m)],
    ):
        log("age gate: month select failed")
    if not select_any(page, "day-select", values=[f"{d:02d}", str(d)], labels=[str(d)]):
        log("age gate: day select failed")

    clicked = False
    for s in ('#ageGateSubmit', '[name="ageGateSubmit"]', 'button[type="submit"]'):
        try:
            l = page.locator(s).first
            if l.count() and l.is_visible():
                l.click()
                clicked = True
                break
        except Exception:
            pass
    if not clicked:
        click_text(page, "Continue")
    log("age gate: submitted")

    time.sleep(2)
    if click_text(page, "Yes, This Is Correct", timeout=8000):
        log("age gate: confirmed")
    time.sleep(2)
    return True


# ---------------------------------------------------------------- captcha

def recaptcha_info(page):
    try:
        return page.evaluate(
            "() => (window.grecaptcha && grecaptcha.enterprise) ? 'enterprise' : "
            "(window.grecaptcha ? 'v2/v3' : 'none')"
        )
    except Exception:
        return "eval-fail"


def ensure_recaptcha(page, patience=25):
    if recaptcha_info(page) != "none":
        log("grecaptcha: already loaded")
        return True

    log("grecaptcha: missing - injecting enterprise.js")
    try:
        page.add_script_tag(url="https://www.google.com/recaptcha/enterprise.js")
    except Exception as e:
        log(f"recaptcha inject fail: {e}")
        return False

    end = time.time() + patience
    while time.time() < end:
        info = recaptcha_info(page)
        if info != "none":
            log(f"grecaptcha ready: {info}")
            return True
        time.sleep(1)
    log("grecaptcha: still missing after inject")
    return False


def prewarm_api(page):
    """Rebuild Imperva cookies for the api host."""
    try:
        p2 = page.context.new_page()
        p2.goto(API_URL, wait_until="domcontentloaded", timeout=30000)
        time.sleep(5)
        p2.close()
    except Exception as e:
        log(f"api prewarm err: {e}")

    try:
        names = [c["name"] for c in page.context.cookies(API_URL)]
        log(f"api cookies: {names}")
        return "reese84" in names
    except Exception:
        return False


# ---------------------------------------------------------------- submit

def submit_form(page):
    for s in ("#submit", 'button[type="submit"]', '[name="submit"]'):
        try:
            l = page.locator(s).first
            if l.count() and l.is_visible():
                l.click()
                return True
        except Exception:
            pass
    for t in ("Create Account", "Sign Up", "Continue", "Submit", "Create"):
        if click_text(page, t, timeout=4000):
            return True
    return False


def blocked(page):
    t = safe_text(page).lower()
    return "apologize for the interruption" in t or "unusual browsing activity" in t


def submit_and_wait(page, timeout=25):
    before = len(NET["create_user"])
    submit_form(page)
    end = time.time() + timeout
    while time.time() < end:
        if len(NET["create_user"]) > before:
            return "posted"
        if blocked(page):
            return "blocked"
        t = safe_text(page).lower()
        if "activation code" in t or "verify your email" in t:
            return "activation"
        time.sleep(1)
    return "no_post"


def classify(resp):
    """403/errorCode = Imperva block. 429 = throttle (its body also says
    "invalid", which is why 429 must be checked before the body)."""
    status = resp.get("status", 0)
    body = (resp.get("body") or "").lower()
    if status == 403 or "errorcode" in body:
        return "imperva_block"
    if status == 429:
        return "rate_limited"
    if status == 200 and "guid" in body:
        return "activation"
    if "not_allowed" in body:
        return "domain_blocked"
    if "invalid" in body or "exists" in body:
        return "bad_email"
    return "unknown"


def back_to_form(page):
    """Full reload so no stale error state survives."""
    goto(page, JOIN_URL)
    if not wait_for_ready(page, 45):
        return False
    if not form_present(page):
        do_age_gate(page)
        wait_for_ready(page, 30)
    if form_present(page):
        ensure_recaptcha(page)
        return True
    return False


def fill_activation(page, code):
    for s in ('#code', '[name="code"]', '[name="activation-code"]', 'input[inputmode="numeric"]'):
        try:
            l = page.locator(s).first
            if l.count() and l.is_visible():
                l.click()
                page.keyboard.type(code, delay=60)
                break
        except Exception:
            pass
    for t in ("Activate", "Submit", "Continue", "Verify"):
        if click_text(page, t, timeout=4000):
            return True
    return False


def read_code_from_mail(inbox, token, page=None, timeout=150):
    end = time.time() + timeout
    poll = 0
    resent = False
    while time.time() < end:
        poll += 1
        try:
            msgs = emailer.fetch_messages(inbox, token)
        except Exception as e:
            log(f"  mail fetch err: {e}")
            msgs = []

        log(f"  poll {poll}: {emailer.LAST_INFO} got={len(msgs)}")

        for m in msgs:
            subj = (m.get("subject", "") or "")[:70]
            log(f"    mail: {subj}")
            blob = (
                (m.get("subject", "") or "")
                + " "
                + (m.get("body", "") or "")
                + " "
                + (m.get("html", "") or "")
            )
            code = extract_code(blob)
            if code:
                return code

        if not resent and page is not None and poll >= 10:
            log("  no mail yet - clicking resend")
            if click_text(page, "Resend Activation Code", timeout=5000):
                log("  resend clicked")
            resent = True

        time.sleep(8)
    return None


# ---------------------------------------------------------------- one account

def create_account(page, idx):
    log(f"[{idx}] --- start ---")

    if not get_ready(page):
        log(f"[{idx}] page never rendered (imperva challenge)")
        snap(page, f"blank_{idx}")
        sheet_push(row("", "", "", "blank", "imperva challenge loop"))
        return "blank"

    if not form_present(page):
        do_age_gate(page)

    if not wait_for_ready(page, 30):
        click_text(page, "Yes, This Is Correct", timeout=5000)
        wait_for_ready(page, 30)

    if not form_present(page):
        log(f"[{idx}] account form never appeared")
        snap(page, f"nofields_{idx}")
        sheet_push(row("", "", "", "nofields", "age gate did not advance"))
        return "nofields"

    ensure_recaptcha(page)
    if not prewarm_api(page):
        log(f"[{idx}] api host has no reese84 - waiting 30s and retrying prewarm")
        time.sleep(30)
        prewarm_api(page)

    user = rand_user()
    pwd = rand_pass()
    inbox = None
    token = None
    state = "unknown"
    tried = []
    entry = None
    same_tries = 0
    imperva_hits = 0

    for attempt in range(1, 11):
        if entry is None:
            entry = emailer.next_domain()
            if entry is None:
                log(f"[{idx}] no domains left")
                break
            same_tries = 0

        if attempt > 1:
            if not back_to_form(page):
                log(f"[{idx}] lost the form")
                break
            time.sleep(10)

        try:
            inbox, token = emailer.create_inbox(entry)
        except Exception as e:
            log(f"[{idx}] inbox fail: {e}")
            entry = None
            continue

        got_dom = inbox.split("@", 1)[1]
        tried.append(got_dom)
        log(f"[{idx}] attempt {attempt} email={inbox} user={user}")

        for name, val in (
            ("email-field", inbox),
            ("username-field", user),
            ("password-field", pwd),
        ):
            log(f"  fill {name}: {type_field(page, name, val)}")
        log(f"  terms: {check_label(page, 'terms')}")

        outcome = submit_and_wait(page, 25)
        log(f"  outcome: {outcome}")

        if outcome == "blocked":
            state = "blocked"
            log(f"[{idx}] imperva page block - cooling 40s")
            time.sleep(40)
            continue

        if outcome == "activation":
            state = "activation"
            break

        if outcome == "no_post":
            v = (NET["validate"] or [{}])[-1]
            vbody = (v.get("body") or "").lower()
            log(f"  validate body: {vbody[:160]}")
            if "errorcode" in vbody or "blocked by our security" in vbody:
                state = "imperva_block"
                imperva_hits += 1
                log(f"[{idx}] imperva 403 at validate - cooling 75s, re-warming")
                time.sleep(75)
                try:
                    page.context.clear_cookies()
                except Exception:
                    pass
                if not back_to_form(page):
                    break
                prewarm_api(page)
                if imperva_hits >= 4:
                    log(f"[{idx}] 4 imperva blocks - stopping")
                    break
                continue
            if "not_allowed" in vbody:
                state = "domain_blocked"
                log(f"[{idx}] {got_dom} blocklisted")
                emailer.mark_blocked(base_domain(got_dom))
                entry = None
                time.sleep(5)
                continue
            if "invalid" in vbody or "exists" in vbody:
                state = "bad_email"
                log(f"[{idx}] {got_dom} rejected at validate")
                emailer.mark_blocked(base_domain(got_dom))
                entry = None
                time.sleep(5)
                continue
            state = "no_post"
            log(f"[{idx}] no POST and no verdict - reloading")
            time.sleep(8)
            continue

        resp = NET["create_user"][-1]
        state = classify(resp)
        log(
            f"  state: {state} status={resp.get('status')} "
            f"body={(resp.get('body') or '')[:160]}"
        )
        snap(page, f"a{attempt}_{idx}")

        if state == "activation":
            break

        if state == "imperva_block":
            imperva_hits += 1
            log(f"[{idx}] imperva 403 - cooling 75s, clearing cookies, re-warming")
            time.sleep(75)
            try:
                page.context.clear_cookies()
            except Exception:
                pass
            if not back_to_form(page):
                break
            prewarm_api(page)
            if imperva_hits >= 4:
                log(f"[{idx}] 4 imperva blocks - stopping")
                break
            continue

        if state == "rate_limited":
            same_tries += 1
            if same_tries >= 3:
                log(f"[{idx}] 429 x{same_tries} - trying a different address")
                entry = None
                time.sleep(30)
                continue
            wait = 40 + same_tries * 20
            log(f"[{idx}] 429 - cooling {wait}s")
            time.sleep(wait)
            continue

        if state == "domain_blocked":
            log(f"[{idx}] {got_dom} blocklisted")
            emailer.mark_blocked(base_domain(got_dom))
            entry = None
            time.sleep(5)
            continue

        if state == "bad_email":
            log(f"[{idx}] {got_dom} rejected")
            emailer.mark_blocked(base_domain(got_dom))
            entry = None
            time.sleep(5)
            continue

        log(f"[{idx}] unknown outcome - cooling 20s")
        time.sleep(20)

    if not inbox:
        sheet_push(row("", "", "", "error", "no inbox"))
        return "error"

    if state == "imperva_block":
        sheet_push(row(inbox, user, pwd, "blocked", f"imperva 403 | {','.join(tried)}"))
        return "blocked"

    if state == "rate_limited":
        sheet_push(row(inbox, user, pwd, "rate_limited", f
