import csv, os, random, re, string, subprocess, tempfile, time

import requests
from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

# ----------------------------------------------------------------- config
SIGNUP_URL = "https://join.pokemon.com/"
COUNTRY    = "United States"
DOB_YEAR, DOB_MONTH, DOB_DAY = 1990, 1, 1
MONTH_NAME = "January"
COUNT      = int(os.getenv("COUNT", "1"))
PAUSE      = 8
OUT        = "accounts.csv"
DBG        = "debug"

SHEETS_URL   = os.getenv("SHEETS_URL", "")
SHEETS_TOKEN = os.getenv("SHEETS_TOKEN", "")

SYMBOLS = "!@#$%^&*"
WORDS = ["ash","misty","brock","pika","gengar","eevee","snorlax","gyara",
         "dragon","mewtwo","lapras","zapdos","onix","psy","golem","machop"]

PROVIDERS = ["https://api.mail.tm", "https://api.mail.gw"]

os.makedirs(DBG, exist_ok=True)

# ------------------------------------------------------------- utilities
def snap(page, name):
    try:
        page.screenshot(path=f"{DBG}/{name}.png", full_page=True)
        with open(f"{DBG}/{name}.html", "w") as f:
            f.write(page.content())
        print(f"  [snap] {name}  url={page.url}")
    except Exception as e:
        print("  snap failed:", e)

def all_frames(page):
    try:
        return list(page.frames)
    except Exception:
        return []

def field_count(page):
    n = 0
    for fr in all_frames(page):
        try:
            n += len(fr.query_selector_all("input, select"))
        except Exception:
            pass
    return n

def find_any(page, selector):
    for fr in all_frames(page):
        try:
            el = fr.query_selector(selector)
            if el:
                return el
        except Exception:
            pass
    return None

def page_text(page):
    try:
        t = page.inner_text("body")
        t = re.sub(r"\s+", " ", t).strip()
        print("  page text:", repr(t[:400]))
    except Exception:
        pass

def dump_errors(page):
    for fr in all_frames(page):
        try:
            msgs = fr.evaluate("""() => {
                const out = [];
                document.querySelectorAll('[class*=error],[class*=invalid],[role=alert],[aria-live]').forEach(e => {
                    const t = (e.innerText || '').trim();
                    if (t && t.length < 200) out.push(t);
                });
                return out;
            }""")
            for m in msgs:
                print("    error box:", repr(m[:160]))
        except Exception:
            pass

# ------------------------------------------------------------ account gen
def random_username():
    base = random.choice(WORDS) + random.choice(WORDS)
    tail = "".join(random.choices(string.digits, k=random.randint(2, 5)))
    return (base + tail)[:16]

def random_password():
    lower = random.choice(string.ascii_lowercase)
    upper = random.choice(string.ascii_uppercase)
    digit = random.choice(string.digits)
    sym   = random.choice(SYMBOLS)
    rest  = "".join(random.choices(string.ascii_letters + string.digits, k=8))
    pw = list(lower + upper + digit + sym + rest)
    random.shuffle(pw)
    return "".join(pw)

# ------------------------------------------------------------- mailboxes
def list_mailboxes():
    """Legacy. Overridden by emailer.list_mailboxes on import."""
    out = []
    for base in PROVIDERS:
        try:
            r = requests.get(f"{base}/domains", timeout=20).json()
            ds = [d["domain"] for d in r.get("hydra:member", [])]
            print(f"  {base}: {ds}")
            for d in ds:
                out.append((base, d))
        except Exception as e:
            print(f"  {base} failed:", e)
    return out

def create_inbox(base, domain):
    addr = "".join(random.choices(string.ascii_lowercase + string.digits, k=10)) + "@" + domain
    pw = "".join(random.choices(string.ascii_letters + string.digits, k=12))
    requests.post(f"{base}/accounts", json={"address": addr, "password": pw},
                  timeout=20).raise_for_status()
    tok = requests.post(f"{base}/token", json={"address": addr, "password": pw},
                        timeout=20).json()["token"]
    return addr, tok, base

def wait_for_link(addr, tok, base, timeout=180):
    """Legacy. Overridden by emailer.wait_for_link on import."""
    h = {"Authorization": f"Bearer {tok}"}
    end = time.time() + timeout
    seen = 0
    while time.time() < end:
        try:
            r = requests.get(f"{base}/messages", headers=h, timeout=20)
        except Exception:
            time.sleep(5)
            continue
        if r.status_code == 200:
            msgs = r.json().get("hydra:member", [])
            if len(msgs) != seen:
                seen = len(msgs)
                print(f"    inbox has {seen} message(s)")
            for m in msgs:
                frm = (m.get("from") or {}).get("address", "").lower()
                subj = (m.get("subject") or "")[:60]
                print(f"    msg from={frm} subj={subj}")
                full = requests.get(f"{base}/messages/{m['id']}", headers=h,
                                    timeout=20).json()
                html = full.get("html") or []
                body = " ".join(html) if isinstance(html, list) else str(html)
                body += " " + (full.get("text") or "")
                links = re.findall(r'https?://[^\s"\'<>)]+', body)
                hit = next((l for l in links if "pokemon" in l), None)
                if hit:
                    return hit
        time.sleep(5)
    return None

# ---------------------------------------------------------------- captcha
class CaptchaFail(Exception):
    pass

def _frames(page, *frags):
    for f in all_frames(page):
        u = f.url or ""
        if all(x in u for x in frags):
            return f
    return None

def _click_iframe_at(page, frag, dx=28):
    for el in page.query_selector_all("iframe"):
        if frag in (el.get_attribute("src") or ""):
            b = el.bounding_box()
            if b:
                page.mouse.click(b["x"] + dx, b["y"] + b["height"] / 2)
                return True
    return False

def _click_first(frame, sels):
    for s in sels:
        try:
            el = frame.query_selector(s)
            if el:
                el.click()
                return True
        except Exception:
            pass
    return False

def _fill_first(frame, sels, text):
    for s in sels:
        try:
            el = frame.query_selector(s)
            if el:
                el.fill(text)
                return True
        except Exception:
            pass
    return False

def _audio_bytes(frame):
    b64 = frame.evaluate("""async () => {
        const el = document.querySelector('audio') || document.getElementById('audio-source');
        if (!el || !el.src) return null;
        try { const r = await fetch(el.src); const b = await r.arrayBuffer();
              const u = new Uint8Array(b); let s = '';
              for (let i = 0; i < u.length; i++) s += String.fromCharCode(u[i]);
              return btoa(s); } catch (e) { return null; }
    }""")
    if not b64:
        return None
    import base64
    return base64.b64decode(b64)

def _transcribe(data):
    import speech_recognition as sr
    with tempfile.NamedTemporaryFile(suffix=".mp3", delete=False) as f:
        f.write(data)
        src = f.name
    wav = src + ".wav"
    subprocess.run(["ffmpeg", "-y", "-i", src, "-ar", "16000", "-ac", "1", wav],
                   capture_output=True)
    try:
        r = sr.Recognizer()
        with sr.AudioFile(wav) as s:
            return r.recognize_google(r.record(s))
    finally:
        for p in (src, wav):
            try: os.unlink(p)
            except Exception: pass

def solve_recaptcha(page, timeout=90):
    end = time.time() + timeout
    anchor = None
    while time.time() < end and not anchor:
        anchor = _frames(page, "recaptcha", "anchor")
        time.sleep(0.5)
    if not anchor:
        raise CaptchaFail("no recaptcha")
    _click_first(anchor, ["#recaptcha-anchor"])
    time.sleep(3)
    a = anchor.query_selector("#recaptcha-anchor")
    if a and a.get_attribute("aria-checked") == "true":
        return True
    for _ in range(5):
        bf = _frames(page, "recaptcha", "bframe")
        if not bf:
            time.sleep(2)
            continue
        _click_first(bf, ["#recaptcha-audio-button"])
        time.sleep(3)
        data = _audio_bytes(bf)
        if not data:
            continue
        try:
            text = _transcribe(data)
            print("    heard:", text)
        except Exception:
            text = ""
        if text:
            _fill_first(bf, ["#audio-response"], text)
            _click_first(bf, ["#recaptcha-verify-button"])
            time.sleep(4)
        a = anchor.query_selector("#recaptcha-anchor")
        if a and a.get_attribute("aria-checked") == "true":
            return True
    raise CaptchaFail("recaptcha failed")

def solve_hcaptcha(page, timeout=180):
    end = time.time() + timeout
    for _ in range(25):
        if time.time() > end:
            raise CaptchaFail("hcaptcha box missing")
        fr = _frames(page, "hcaptcha.com", "checkbox")
        if fr:
            if _click_first(fr, ["#checkbox", ".checkbox", "[role=checkbox]"]):
                break
            if _click_iframe_at(page, "checkbox"):
                break
        time.sleep(1)
    print("    hcaptcha: clicked")
    for _ in range(15):
        fr = _frames(page, "hcaptcha.com", "checkbox")
        if fr:
            try:
                el = fr.query_selector("#checkbox")
                if el and (el.get_attribute("aria-checked") == "true"
                           or "checked" in (el.get_attribute("class") or "")):
                    print("    hcaptcha: passed on click")
                    return True
            except Exception:
                pass
        if _frames(page, "hcaptcha.com", "challenge"):
            break
        time.sleep(1)
    print("    hcaptcha: audio fallback")
    for _ in range(5):
        if time.time() > end:
            break
        ch = _frames(page, "hcaptcha.com", "challenge")
        if not ch:
            time.sleep(2)
            continue
        _click_first(ch, ["#audio-button", "[aria-label*='audio' i]", ".audio-button"])
        time.sleep(3)
        data = _audio_bytes(ch)
        if not data:
            continue
        try:
            text = _transcribe(data)
            print("    heard:", text)
        except Exception as e:
            print("    stt:", e)
            text = ""
        if text:
            _fill_first(ch, ["#audio-response", "textarea", "input[type=text]"], text)
            _click_first(ch, ["#voice-submit", ".button-submit", "button[type=submit]"])
            time.sleep(4)
        fr = _frames(page, "hcaptcha.com", "checkbox")
        if fr:
            try:
                el = fr.query_selector("#checkbox")
                if el and el.get_attribute("aria-checked") == "true":
                    return True
            except Exception:
                pass
    raise CaptchaFail("hcaptcha failed")

def solve_any(page, wait=6):
    end = time.time() + wait
    while time.time() < end:
        if _frames(page, "hcaptcha.com", "checkbox"):
            return solve_hcaptcha(page)
        if _frames(page, "recaptcha", "anchor"):
            return solve_recaptcha(page)
        time.sleep(0.5)
    raise CaptchaFail("no captcha frame found")

def wait_for_ready(page, timeout=60):
    end = time.time() + timeout
    while time.time() < end:
        if field_count(page) >= 2:
            return "form"
        if _frames(page, "hcaptcha.com", "checkbox"):
            return "captcha"
        if _frames(page, "recaptcha", "anchor"):
            return "captcha"
        time.sleep(1.5)
    return "none"

# ------------------------------------------------------------- form fill
def human_warmup(page, seconds=3.0):
    """Jiggle the mouse and scroll a bit so Imperva sees activity."""
    try:
        w = page.viewport_size or {"width": 1366, "height": 900}
        end = time.time() + seconds
        while time.time() < end:
            page.mouse.move(random.randint(60, w["width"] - 60),
                            random.randint(60, w["height"] - 60),
                            steps=random.randint(6, 18))
            time.sleep(random.uniform(0.15, 0.45))
        page.mouse.wheel(0, random.randint(80, 260))
        time.sleep(random.uniform(0.3, 0.7))
        page.mouse.wheel(0, -random.randint(40, 120))
    except Exception:
        pass

def blocked_page(page):
    """True when Imperva served its 'unusual browsing activity' page."""
    try:
        return "apologize for the interruption" in page.inner_text("body").lower()
    except Exception:
        return False

def wait_for_imperva_cookie(page, timeout=25):
    """Let Imperva's reese84 / incap challenge finish before we submit."""
    end = time.time() + timeout
    while time.time() < end:
        try:
            names = {c.get("name", "") for c in page.context.cookies()}
        except Exception:
            names = set()
        if any(n.startswith(("reese84", "visid_incap", "incap_ses", "nlbi"))
               for n in names):
            print("  imperva cookie present")
            return True
        time.sleep(1)
    print("  no imperva cookie yet")
    return False

def fill_label(page, label, value):
    """Type the value with real keystrokes - Imperva watches keydown/keyup."""
    xp = (f'contains(translate(.,"ABCDEFGHIJKLMNOPQRSTUVWXYZ",'
          f'"abcdefghijklmnopqrstuvwxyz"),"{label.lower()}")')
    for sel in [f'input[name="{label.lower()}"]',
                f'input[id*="{label.lower()}"]',
                f'xpath=//label[{xp}]/following::input[1]',
                f'input[placeholder*="{label}" i]']:
        el = find_any(page, sel)
        if not el:
            continue
        try:
            el.click()
            time.sleep(random.uniform(0.2, 0.5))
            el.fill("")
            el.press_sequentially(value, delay=random.randint(55, 135))
            time.sleep(random.uniform(0.3, 0.8))
            return True
        except Exception:
            try:
                el.fill(value)
                return True
            except Exception:
                continue
    return False

def set_select(page, label, value, index_fallback=None):
    xp = (f'contains(translate(.,"ABCDEFGHIJKLMNOPQRSTUVWXYZ",'
          f'"abcdefghijklmnopqrstuvwxyz"),"{label.lower()}")')
    sels = [f'xpath=//label[{xp}]/following::select[1]',
            f'xpath=//*[self::label or self::span or self::div][{xp}]/following::select[1]',
            f'select[name*="{label.lower()}" i]',
            f'select[id*="{label.lower()}" i]',
            f'select[aria-label*="{label}" i]']
    for sel in sels:
        el = find_any(page, sel)
        if not el:
            continue
        try:
            el.select_option(label=value)
        except Exception:
            try:
                el.select_option(value=value)
            except Exception:
                if index_fallback is None:
                    continue
                try:
                    el.select_option(index=index_fallback)
                except Exception:
                    continue
        print(f"  set {label} = {value}")
        return True
    print(f"  FAILED to set {label}")
    return False

def check_label(page, label):
    for sel in [f'input[name="{label}"]', f'input[id="{label}"]',
                f'input[name*="{label}" i]', f'input[id*="{label}" i]']:
        el = find_any(page, sel)
        if el:
            try:
                if not el.is_checked():
                    el.check()
                print(f"  checked: {label}")
                return True
            except Exception:
                pass
    xp = (f'contains(translate(.,"ABCDEFGHIJKLMNOPQRSTUVWXYZ",'
          f'"abcdefghijklmnopqrstuvwxyz"),"{label.lower()}")')
    el = find_any(page, f'xpath=//label[{xp}]/preceding::input[@type="checkbox"][1]')
    if el:
        try:
            if not el.is_checked():
                el.check()
            print(f"  checked: {label}")
            return True
        except Exception:
            pass
    return False

def click_text(page, text, pause=(3.5, 5.5)):
    for sel in [f'button:has-text("{text}")', f'a:has-text("{text}")',
                f'input[value*="{text}"]']:
        el = find_any(page, sel)
        if not el:
            continue
        human_warmup(page, 0.8)
        try:
            el.click()
        except Exception:
            pass
        time.sleep(random.uniform(*pause))
        print(f"  clicked: {text}")
        return True
    return False

# ---------------------------------------------------------------- sheets
def push(row):
    if not SHEETS_URL:
        print("  sheet: no SHEETS_URL")
        return
    try:
        r = requests.post(SHEETS_URL, json={"token": SHEETS_TOKEN, "accounts": [row]},
                          allow_redirects=True, timeout=30)
        print("  sheet:", r.text[:50])
    except Exception as e:
        print("  sheet error:", e)

# ------------------------------------------------------------------ main
def launch_browser(p):
    """Real Chrome first - Playwright's Chromium has no window.chrome.runtime."""
    args = [
        "--no-sandbox",
        "--disable-blink-features=AutomationControlled",
        "--disable-dev-shm-usage",
        "--disable-features=IsolateOrigins,site-per-process",
        "--lang=en-US",
    ]
    err = None
    for kw in ({"channel": "chrome"}, {}):
        try:
            b = p.chromium.launch(headless=False, args=args, **kw)
            print("  browser:", kw.get("channel", "bundled chromium"))
            return b
        except Exception as e:
            print("  launch failed:", kw.get("channel", "chromium"), e)
            err = e
    raise err

def one_account(page, acc):
    """Fill and submit the signup, retrying the whole thing if Imperva blocks."""
    for attempt in range(4):
        print(f"  --- attempt {attempt + 1} ---")
        try:
            page.goto(SIGNUP_URL, wait_until="domcontentloaded", timeout=90000)
        except Exception as e:
            print("  goto failed:", e)
        time.sleep(random.uniform(6, 9))
        print("  landed on:", page.url)

        for _ in range(3):
            state = wait_for_ready(page, 45)
            print("  state:", state)
            if state == "form":
                break
            if state == "captcha":
                try:
                    solve_any(page, wait=4)
                except CaptchaFail as e:
                    print("  captcha fail:", e)
                time.sleep(4)
                continue
            time.sleep(8)

        if field_count(page) == 0:
            print("  still no form")
            snap(page, f"{acc['username']}_nofields")
            return "failed"

        wait_for_imperva_cookie(page, 20)
        human_warmup(page, 2.5)

        set_select(page, "country", COUNTRY)
        set_select(page, "year", str(DOB_YEAR))
        set_select(page, "month", MONTH_NAME, index_fallback=DOB_MONTH)
        set_select(page, "day", str(DOB_DAY), index_fallback=DOB_DAY)
        time.sleep(random.uniform(1.5, 2.5))
        click_text(page, "Continue")
        time.sleep(3)

        if click_text(page, "Yes, This Is Correct"):
            print("  birthdate confirmed")
            time.sleep(random.uniform(4, 7))

        for _ in range(4):
            if find_any(page, 'input[name="username"]'):
                break
            time.sleep(4)

        human_warmup(page, 1.5)

        filled = {}
        filled["username"] = fill_label(page, "username", acc["username"])
        filled["password"] = fill_label(page, "password", acc["password"])
        filled["email"]    = fill_label(page, "email", acc["email"])
        print("  filled:", filled)
        if not all(filled.values()):
            print("  field fill failed, retrying page")
            continue

        for _ in range(3):
            try:
                page.keyboard.press("Tab")
                time.sleep(0.35)
            except Exception:
                pass

        print("  terms ticked:", check_label(page, "terms"))
        time.sleep(1)

        # wait for the invisible reCAPTCHA token to populate before submitting
        for _ in range(12):
            tok = find_any(page, 'input[name="recaptcha-token"]')
            try:
                if tok and (tok.input_value() or "").strip():
                    break
            except Exception:
                pass
            time.sleep(1)
        time.sleep(random.uniform(2.0, 3.5))

        click_text(page, "Continue", pause=(6, 8))
        print("  after submit url:", page.url)
        page_text(page)
        dump_errors(page)
        snap(page, f"{acc['username']}_submitted")

        if blocked_page(page):
            print("  >>> IMPERVA BLOCK")
            # the POST may still have landed - give the inbox a short look
            link = wait_for_link(acc["email"], acc["mail_token"],
                                 acc["mail_base"], timeout=35)
            if link:
                print("  link:", link[:80])
                page.goto(link, wait_until="domcontentloaded", timeout=90000)
                time.sleep(3)
                print("  activated")
                return "ok"
            try:
                page.context.clear_cookies()
            except Exception:
                pass
            time.sleep(random.uniform(18, 28))
            continue

        body = page.content().lower()
        if "not valid" in body or "creation failed" in body:
            print("  >>> EMAIL REJECTED BY SITE")
            return "bad_email"
        if "already taken" in body:
            print("  >>> username taken")
            return "failed"

        print("  waiting for email...")
        link = wait_for_link(acc["email"], acc["mail_token"], acc["mail_base"],
                             timeout=150)
        if not link:
            print("  no email")
            return "failed"
        print("  link:", link[:80])
        page.goto(link, wait_until="domcontentloaded", timeout=90000)
        time.sleep(3)
        print("  activated")
        return "ok"

    return "failed"

def main():
    results = []
    with sync_playwright() as p:
        browser = launch_browser(p)
        ctx = browser.new_context(
            viewport={"width": 1366, "height": 900},
            locale="en-US",
            timezone_id="America/New_York",
        )
        ctx.add_init_script("""
            Object.defineProperty(navigator,'webdriver',{get:()=>undefined});
            window.chrome = window.chrome || {};
            window.chrome.runtime = window.chrome.runtime || {};
        """)
        page = ctx.new_page()
        try:
            print("  ua:", page.evaluate("navigator.userAgent"))
        except Exception:
            pass

        boxes = list_mailboxes()
        print("mailboxes to try:", boxes)
        if not boxes:
            print("NO MAIL DOMAINS AVAILABLE")
            return

        for i in range(COUNT):
            print(f"[{i+1}/{COUNT}] start")
            for base, domain in boxes:
                acc = {"username": random_username(), "password": random_password()}
                try:
                    acc["email"], acc["mail_token"], acc["mail_base"] = create_inbox(base, domain)
                except Exception as e:
                    print("  inbox failed:", base, domain, e)
                    continue
                print("  trying email:", acc["email"])

                try:
                    status = one_account(page, acc)
                except PWTimeout as e:
                    print("  timeout:", e); status = "timeout"
                except Exception as e:
                    print("  error:", repr(e)); status = "error"

                acc["status"] = status
                results.append(acc)
                push({k: acc[k] for k in ("username", "password", "email", "status")})
                with open(OUT, "w", newline="") as f:
                    w = csv.DictWriter(f, fieldnames=["username","password","email","status"],
                                       extrasaction="ignore")
                    w.writeheader(); w.writerows(results)
                print(f"  -> {acc['username']} [{status}]")

                if status == "ok":
                    break
                if status != "bad_email":
                    break
                print("  domain rejected, trying next...")
                time.sleep(3)
            time.sleep(PAUSE)

        browser.close()
    print("done:", OUT)

from emailer import (create_inbox, fetch_messages, wait_for_link,
                     wait_for_code, wait_gmail, list_domains, list_mailboxes)
if __name__ == "__main__":
    main()
