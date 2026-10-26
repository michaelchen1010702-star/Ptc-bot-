#!/usr/bin/env python3
"""Pokemon Trainer Club account creator.

join.pokemon.com 4-step wizard -> Imperva/Incapsula challenge -> captcha ->
submit -> emailed 6-digit activation code -> Google Sheet row.
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

NET = {"create_user": []}


# ---------------------------------------------------------------- utils

def log(msg):
    print(msg, flush=True)


def safe_text(page):
    try:
        return re.sub(r"\s+", " ", page.inner_text("body"))
    except Exception:
        return ""


def error_text(page):
    for s in (
        '[role="alert"]',
        '.error',
        '.error-message',
        '[class*="error" i]',
        '[class*="invalid" i]',
    ):
        try:
            l = page.locator(s).first
            if l.count() and l.is_visible():
                t = (l.inner_text() or "").strip()
                if t:
                    return re.sub(r"\s+", " ", t)[:200]
        except Exception:
            pass
    return ""


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

    def on_response(resp):
        u = resp.url
        if "create-user" in u:
            NET["create_user"].append(resp.status)
            log(f"[resp {resp.status}] {u[:150]}")
        elif "_Incapsula" in u or "recaptcha" in u or "hcaptcha" in u:
            log(f"[resp {resp.status}] {u[:170]}")

    def on_failed(req):
        log(f"[reqfail] {req.failure} {req.url[:150]}")

    page.on("console", on_console)
    page.on("pageerror", on_pageerror)
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
    if human:
        page.keyboard.type(value, delay=random.randint(30, 70))
    else:
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

def token_ready(page):
    for s in ('#recaptcha-token', '[name="g-recaptcha-response"]', '[name="h-captcha-response"]'):
        try:
            v = page.eval_on_selector(s, "el => el.value")
            if v:
                return True
        except Exception:
            pass
    return False


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


def hcaptcha_audio(page):
    try:
        import speech_recognition as sr
    except Exception:
        return False

    for fr in page.frames:
        if "hcaptcha" not in fr.url:
            continue
        try:
            btn = fr.locator('#audio-button, button[title*="audio" i], .audio-button').first
            if btn.count():
                btn.click()
                time.sleep(2)

            src = None
            for s in ("#audio-source", "audio"):
                try:
                    el = fr.locator(s).first
                    if el.count():
                        src = el.get_attribute("src")
                        if src:
                            break
                except Exception:
                    pass
            if not src:
                continue

            data = requests.get(src, timeout=30).content
            with open(os.path.join(DEBUG_DIR, "hc.mp3"), "wb") as f:
                f.write(data)

            rec = sr.Recognizer()
            with sr.AudioFile(os.path.join(DEBUG_DIR, "hc.mp3")) as srcf:
                audio = rec.record(srcf)
            text = rec.recognize_google(audio)
            log(f"captcha: audio -> {text}")

            box = fr.locator('#audio-response, [name="audio-response"]').first
            box.click()
            fr.page.keyboard.type(text.lower())
            sub = fr.locator("#audio-submit, .audio-submit").first
            if sub.count():
                sub.click()
            time.sleep(3)
            return True
        except Exception as e:
            log(f"captcha audio err: {e}")
    return False


def solve_captcha(page, timeout=20):
    end = time.time() + timeout
    clicked = False
    while time.time() < end:
        for fr in page.frames:
            if not any(k in fr.url for k in ("hcaptcha", "recaptcha", "hcap")):
                continue
            if not clicked:
                try:
                    box = fr.locator("#checkbox, .checkbox").first
                    if box.count() and box.is_visible():
                        box.click()
                        clicked = True
                        log("captcha: clicked checkbox")
                except Exception:
                    pass
        time.sleep(0.5)
    log(f"captcha: no checkbox (clicked={clicked}) - submitting anyway")
    return token_ready(page)


def prewarm_api(page):
    """api.join.pokemon.com is a separate host with its own Imperva cookies."""
    try:
        cookies = page.context.cookies(API_URL)
        log(f"api cookies before: {[c['name'] for c in cookies]}")
    except Exception:
        pass

    try:
        p2 = page.context.new_page()
        p2.goto(API_URL, wait_until="domcontentloaded", timeout=30000)
        time.sleep(4)
        log(f"api prewarm text: {safe_text(p2)[:120]}")
        p2.close()
    except Exception as e:
        log(f"api prewarm err: {e}")

    try:
        cookies = page.context.cookies(API_URL)
        log(f"api cookies after: {[c['name'] for c in cookies]}")
    except Exception:
        pass


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


def after_submit_state(page, timeout=45):
    end = time.time() + timeout
    while time.time() < end:
        if blocked(page):
            return "blocked"
        if NET["create_user"] and NET["create_user"][-1] == 429:
            return "rate_limited"
        t = safe_text(page).lower()
        if "activation code" in t or "verify your email" in t:
            return "activation"
        e = error_text(page).lower()
        if "already exists" in e or "not valid" in e:
            return "bad_email"
        time.sleep(1)
    return "still_form" if form_present(page) else "unknown"


def back_to_form(page):
    """Get back to the account form after an error page."""
    click_text(page, "Use a Different Email", timeout=5000)
    if wait_for_ready(page, 20) and form_present(page):
        return True

    goto(page, JOIN_URL)
    if not wait_for_ready(page, 45):
        return False
    if not form_present(page):
        do_age_gate(page)
        wait_for_ready(page, 30)
    return form_present(page)


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


def read_code_from_mail(inbox, token, timeout=240):
    end = time.time() + timeout
    while time.time() < end:
        try:
            msgs = emailer.fetch_messages(inbox, token)
        except Exception as e:
            log(f"mail fetch err: {e}")
            msgs = []
        for m in msgs:
            blob = (m.get("subject", "") + " " + m.get("body", "") + " " + m.get("html", ""))
            code = extract_code(blob)
            if code:
                return code
        time.sleep(5)
    return None


# ---------------------------------------------------------------- one account

def create_account(page, idx):
    log(f"[{idx}] --- start ---")

    if not get_ready(page):
        log(f"[{idx}] page never rendered (imperva challenge)")
        snap(page, f"blank_{idx}")
        sheet_push(row("", "", "", "blank", "imperva challenge loop"))
        return False

    if not form_present(page):
        do_age_gate(page)

    if not wait_for_ready(page, 30):
        click_text(page, "Yes, This Is Correct", timeout=5000)
        wait_for_ready(page, 30)

    if not form_present(page):
        log(f"[{idx}] account form never appeared")
        snap(page, f"nofields_{idx}")
        sheet_push(row("", "", "", "nofields", "age gate did not advance"))
        return False

    ensure_recaptcha(page)
    prewarm_api(page)

    user = rand_user()
    pwd = rand_pass()
    inbox = None
    token = None
    state = "unknown"
    tried = []

    for attempt in range(1, 6):
        dom = emailer.next_domain()
        try:
            inbox, token = emailer.create_inbox(domain=dom)
        except Exception as e:
            log(f"[{idx}] inbox fail: {e}")
            break

        got_dom = inbox.split("@", 1)[1]
        tried.append(got_dom)
        log(f"[{idx}] attempt {attempt} email={inbox} user={user}")
        NET["create_user"] = []

        for name, val in (
            ("email-field", inbox),
            ("username-field", user),
            ("password-field", pwd),
        ):
            log(f"  fill {name}: {type_field(page, name, val)}")
        log(f"  email value read back: {field_value(page, 'email-field')}")
        log(f"  terms: {check_label(page, 'terms')}")

        solve_captcha(page, 15)

        for sub in range(1, 4):
            submit_form(page)
            state = after_submit_state(page, 30)
            log(f"  after post {sub}: {state} | url={page.url}")
            if state != "rate_limited":
                break
            log(f"  429 on post {sub} - retrying in 10s")
            time.sleep(10)

        log(f"  page text: {safe_text(page)[:400]}")
        log(f"  error text: {error_text(page)[:180]}")
        snap(page, f"a{attempt}_{idx}")

        if state == "activation":
            break

        if state == "bad_email":
            log(f"[{idx}] {got_dom} rejected - rotating domain")
            if not back_to_form(page):
                log(f"[{idx}] lost the form")
                break
            ensure_recaptcha(page)
            continue

        if state == "rate_limited":
            wait = 30 * attempt
            log(f"[{idx}] still 429 - cooling {wait}s, clearing cookies")
            time.sleep(wait)
            try:
                page.context.clear_cookies()
            except Exception:
                pass
            if not back_to_form(page):
                break
            ensure_recaptcha(page)
            continue

        if state == "blocked":
            log(f"[{idx}] imperva block - cooling 25s")
            time.sleep(25)
            if not back_to_form(page):
                break
            continue

        if state == "still_form":
            log(f"[{idx}] still on form - refilling")
            time.sleep(6)
            continue

        break

    if not inbox:
        sheet_push(row("", "", "", "error", "no inbox"))
        return False

    if state == "rate_limited":
        sheet_push(row(inbox, user, pwd, "rate_limited", f"domains={','.join(tried)}"))
        return False

    if state == "blocked":
        sheet_push(row(inbox, user, pwd, "blocked", f"domains={','.join(tried)}"))
        return False

    if state == "bad_email":
        log(f"[{idx}] all domains rejected: {tried}")
        sheet_push(row(inbox, user, pwd, "bad_email", f"domains={','.join(tried)}"))
        return False

    if state != "activation":
        snap(page, f"submitfail_{idx}")
        sheet_push(row(inbox, user, pwd, "submit_failed", f"state={state}"))
        return False

    log(f"[{idx}] activation page reached")
    code = read_code_from_mail(inbox, token, 240)
    log(f"[{idx}] activation code: {code}")
    if not code:
        sheet_push(row(inbox, user, pwd, "created", "no code in mail"))
        return False

    fill_activation(page, code)
    time.sleep(4)
    t = safe_text(page).lower()
    if "thank you" in t or "activated" in t:
        log(f"[{idx}] -> {user} [ok]")
        sheet_push(row(inbox, user, pwd, "ok", ""))
        return True

    log(f"[{idx}] code rejected")
    snap(page, f"coderej_{idx}")
    sheet_push(row(inbox, user, pwd, "created", "code rejected"))
    return False


# ---------------------------------------------------------------- main

def launch(pw):
    ctx = pw.chromium.launch_persistent_context(
        user_data_dir=PROFILE_DIR,
        channel="chrome",
        headless=HEADLESS,
        ignore_default_args=["--enable-automation"],
        args=CHROME_ARGS,
        locale="en-US",
        timezone_id="America/Los_Angeles",
        viewport={"width": 1440, "height": 900},
        user_agent=UA,
    )
    page = ctx.pages[0] if ctx.pages else ctx.new_page()
    hook_diag(page)
    return ctx, page


def main():
    log(f"count={COUNT} headless={HEADLESS}")
    emailer.prime_domains()

    ok = 0

    for rnd in (1, 2):
        if ok >= COUNT:
            break
        log(f"=== round {rnd} ===")

        with sync_playwright() as pw:
            ctx, page = launch(pw)
            for i in range(1, COUNT + 1):
                try:
                    if create_account(page, i):
                        ok += 1
                except Exception as e:
                    log(f"[{i}] exception: {e}")
                    traceback.print_exc()
                    snap(page, f"exc_{i}")
                time.sleep(2)
            ctx.close()

        if ok == 0 and rnd == 1:
            log("no accounts - cooling 90s then a fresh browser round")
            time.sleep(90)

    log(f"done: {ok}/{COUNT} accounts created")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
