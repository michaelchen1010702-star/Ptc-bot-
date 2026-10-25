import csv, os, random, re, string, subprocess, tempfile, time

import requests
from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

# ----------------------------------------------------------------- config
SIGNUP_URL = "https://join.pokemon.com/"
WARMUP_URL = "https://www.pokemon.com/us/"
COUNTRY    = "United States"
DOB_YEAR, DOB_MONTH, DOB_DAY = 1990, 1, 1
MONTH_NAME = "January"
COUNT      = int(os.getenv("COUNT", "1"))
PAUSE      = 8
OUT        = "accounts.csv"
DBG        = "debug"
PROFILE    = "/tmp/ptcprofile"

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

def cookie_names(page):
    try:
        return sorted({c.get("name", "") for c in page.context.cookies()})
    except Exception:
        return []

def page_text(page):
    try:
        t = page.inner_text("body")
        t = re.sub(r"\s+", " ", t).strip()
        print("  page text:", repr(t[:400]))
    except Exception:
        pass

def incapsula_frame(page):
    for f in all_frames(page):
        u = (f.url or "").lower()
        if "_incapsula_resource" in u or "incapsula" in u:
            return f
    return None

def dump_html(page, n=1500):
    try:
        h = re.sub(r"\s+", " ", page.content())
        print("  html[%d]: %s" % (len(h), h[:n]))
    except Exception as e:
        print("  html dump failed:", e)

def blank_report(page):
    try:
        t = re.sub(r"\s+", " ", page.inner_text("body")).strip()
    except Exception:
        t = ""
    try:
        h = len(page.content())
    except Exception:
        h = 0
    print("  blank: url=%s  visible=%r  html=%dB" % (page.url, t[:110], h))
    print("  challenge frame:", "yes" if incapsula_frame(page) else "no")
    print("  cookies:", cookie_names(page))

def hook_diag(page):
    """Watch the console, Incapsula requests, and any failed request."""
    def on_console(msg):
        try:
            if msg.type in ("error", "warning"):
                t = (msg.text or "")[:220]
                if t:
                    print("    [console:%s] %s" % (msg.type, t))
        except Exception:
            pass

    def on_response(r):
        try:
            u = r.url or ""
            if "incapsula" in u.lower():
                print("    [resp %s] %s" % (r.status, u[:110]))
        except Exception:
            pass

    def on_failed(req):
        try:
            print("    [reqfail] %s %s" % (req.failure, (req.url or "")[:100]))
        except Exception:
            pass

    try:
        page.on("console", on_console)
        page.on("response", on_response)
        page.on("requestfailed", on_failed)
    except Exception as e:
        print("  hook failed:", e)

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

# ------------------------------------------------------------- form fill
def human_warmup(page, seconds=1.2):
    try:
        w = page.viewport_size or {"width": 1366, "height": 900}
        end = time.time() + seconds
        while time.time() < end:
            page.mouse.move(random.randint(60, w["width"] - 60),
                            random.randint(60, w["height"] - 60),
                            steps=random.randint(4, 10))
            time.sleep(random.uniform(0.08, 0.2))
    except Exception:
        pass

def blocked_page(page):
    try:
        return "apologize for the interruption" in page.inner_text("body").lower()
    except Exception:
        return False

def reese_value(page):
    for c in page.context.cookies():
        if (c.get("name") or "").startswith("reese84"):
            return c.get("value") or ""
    return ""

def wait_for_reese(page, timeout=20):
    end = time.time() + timeout
    while time.time() < end:
        if reese_value(page):
            print("  reese84 present")
            return True
        time.sleep(0.5)
    print("  no reese84")
    return False

def type_text(page, el, text, lo=20, hi=60):
    try:
        el.click()
    except Exception:
        pass
    time.sleep(random.uniform(0.1, 0.25))
    try:
        el.fill("")
    except Exception:
        pass
    try:
        page.keyboard.type(text, delay=random.randint(lo, hi))
    except Exception:
        el.fill(text)
    time.sleep(random.uniform(0.1, 0.3))

def fill_label(page, label, value):
    xp = ('contains(translate(.,"ABCDEFGHIJKLMNOPQRSTUVWXYZ",'
          '"abcdefghijklmnopqrstuvwxyz"),"%s")' % label.lower())
    for sel in ['input[name="%s"]' % label.lower(),
                'input[id*="%s"]' % label.lower(),
                'xpath=//label[%s]/following::input[1]' % xp,
                'input[placeholder*="%s" i]' % label]:
        el = find_any(page, sel)
        if not el:
            continue
        try:
            type_text(page, el, value)
            return True
        except Exception:
            continue
    return False

def set_select(page, label, value, index_fallback=None):
    xp = ('contains(translate(.,"ABCDEFGHIJKLMNOPQRSTUVWXYZ",'
          '"abcdefghijklmnopqrstuvwxyz"),"%s")' % label.lower())
    sels = ['xpath=//label[%s]/following::select[1]' % xp,
            'xpath=//*[self::label or self::span or self::div][%s]/following::select[1]' % xp,
            'select[name*="%s" i]' % label.lower(),
            'select[id*="%s" i]' % label.lower(),
            'select[aria-label*="%s" i]' % label]
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
    xp = ('contains(translate(.,"ABCDEFGHIJKLMNOPQRSTUVWXYZ",'
          '"abcdefghijklmnopqrstuvwxyz"),"%s")' % label.lower())
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

def click_text(page, text, pause=(2.0, 3.2)):
    for sel in [f'button:has-text("{text}")', f'a:has-text("{text}")',
                f'input[value*="{text}"]']:
        el = find_any(page, sel)
        if not el:
            continue
        try:
            el.click()
        except Exception:
            pass
        time.sleep(random.uniform(*pause))
        print(f"  clicked: {text}")
        return True
    return False

def code_input(page):
    for sel in ['input[name*="code" i]', 'input[id*="code" i]',
                'input[placeholder*="code" i]', 'input[maxlength="6"]',
                'input[autocomplete="one-time-code"]']:
        el = find_any(page, sel)
        if el:
            return el
    for fr in all_frames(page):
        try:
            for inp in fr.query_selector_all("input"):
                t = (inp.get_attribute("type") or "text").lower()
                n = ((inp.get_attribute("name") or "")
                     + (inp.get_attribute("id") or "")).lower()
                if t in ("text", "tel", "number", "search") and not any(
                        k in n for k in ("username", "password", "email")):
                    return inp
        except Exception:
            pass
    return None

def enter_code(page, code):
    el = code_input(page)
    if not el:
        print("  no code input found")
        return False
    try:
        type_text(page, el, code, lo=40, hi=90)
    except Exception as e:
        print("  code fill failed:", e)
        return False
    print("  code entered")
    for txt in ("Continue", "Activate", "Submit", "Verify", "Next"):
        if click_text(page, txt, pause=(3, 4)):
            return True
    try:
        el.press("Enter")
        time.sleep(3)
        print("  pressed Enter")
        return True
    except Exception:
        pass
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
def launch(p):
    args = [
        "--no-sandbox",
        "--disable-blink-features=AutomationControlled",
        "--disable-dev-shm-usage",
        "--disable-background-timer-throttling",
        "--disable-backgrounding-occluded-windows",
        "--disable-renderer-backgrounding",
        "--window-size=1366,900",
        "--lang=en-US",
    ]
    common = dict(headless=False, args=args,
                  viewport={"width": 1366, "height": 900},
                  locale="en-US")
    for kw in ({"channel": "chrome", "ignore_default_args": ["--enable-automation"]},
               {"channel": "chrome"},
               {}):
        label = (kw.get("channel", "bundled chromium")
                 + ("+noautomation" if "ignore_default_args" in kw else ""))
        try:
            ctx = p.chromium.launch_persistent_context(PROFILE, **common, **kw)
            print("  browser:", label)
            return ctx
        except Exception as e:
            print("  launch failed:", label, e)
    raise RuntimeError("no browser would launch")

def get_form(page, tries=2, patience=100):
    """Imperva's reese84 challenge needs TIME. Do not reload it early."""
    for rnd in range(tries):
        print("  waiting up to %ds for the challenge to clear..." % patience)
        end = time.time() + patience
        tried_captcha = False
        while time.time() < end:
            if field_count(page) >= 2:
                print("  state: form")
                return True
            if not tried_captcha and (_frames(page, "hcaptcha.com", "checkbox")
                                      or _frames(page, "recaptcha", "anchor")):
                print("  state: captcha")
                try:
                    solve_any(page, wait=4)
                except CaptchaFail as e:
                    print("  captcha fail:", e)
                tried_captcha = True
                continue
            # poke the challenge frame occasionally, in case it wants a click
            if int(time.time()) % 20 == 0:
                _click_iframe_at(page, "_Incapsula_Resource", dx=20)
            human_warmup(page, 0.5)
            time.sleep(2)
        print("  still blank after %ds" % patience)
        blank_report(page)
        dump_html(page)
        try:
            page.reload(wait_until="load", timeout=60000)
        except Exception as e:
            print("  reload failed:", e)
        time.sleep(random.uniform(5, 8))
    return field_count(page) > 0

def one_account(page, acc):
    for attempt in range(3):
        t0 = time.time()
        print(f"  --- attempt {attempt + 1} ---")

        if attempt == 0:
            try:
                page.goto(WARMUP_URL, wait_until="load", timeout=60000)
                time.sleep(random.uniform(2, 4))
                print("  warmup html=%dB" % len(page.content()))
            except Exception as e:
                print("  warmup failed:", e)

        try:
            page.goto(SIGNUP_URL, wait_until="load", timeout=90000)
        except Exception as e:
            print("  goto failed:", e)
        print("  landed on:", page.url)

        if not get_form(page):
            print("  still no form, retrying account")
            snap(page, f"{acc['username']}_nofields")
            try:
                page.context.clear_cookies()
            except Exception:
                pass
            time.sleep(random.uniform(6, 10))
            continue

        wait_for_reese(page, 20)

        if find_any(page, 'input[name="username"]'):
            print("  already on account step")
        else:
            human_warmup(page, 1.0)
            set_select(page, "country", COUNTRY)
            set_select(page, "year", str(DOB_YEAR))
            set_select(page, "month", MONTH_NAME, index_fallback=DOB_MONTH)
            set_select(page, "day", str(DOB_DAY), index_fallback=DOB_DAY)
            click_text(page, "Continue")
            time.sleep(2)
            if click_text(page, "Yes, This Is Correct"):
                print("  birthdate confirmed")
                time.sleep(random.uniform(2.5, 4.0))

        for _ in range(4):
            if find_any(page, 'input[name="username"]'):
                break
            time.sleep(2)

        filled = {}
        filled["username"] = fill_label(page, "username", acc["username"])
        filled["password"] = fill_label(page, "password", acc["password"])
        filled["email"]    = fill_label(page, "email", acc["email"])
        print("  filled:", filled)
        if not all(filled.values()):
            print("  field fill failed, retrying page")
            continue

        print("  terms ticked:", check_label(page, "terms"))

        for _ in range(6):
            tok = find_any(page, 'input[name="recaptcha-token"]')
            try:
                if tok and (tok.input_value() or "").strip():
                    break
            except Exception:
                pass
            time.sleep(0.8)

        t_submit = time.time() - t0
        print("  time to submit: %.1fs   reese84 len=%d" % (t_submit, len(reese_value(page))))

        click_text(page, "Continue", pause=(3.5, 5.0))
        print("  after submit url:", page.url)
        page_text(page)
        dump_errors(page)
        snap(page, f"{acc['username']}_submitted")

        if blocked_page(page):
            print("  >>> IMPERVA BLOCK")
            time.sleep(4)
            if not blocked_page(page):
                print("  block cleared on its own")
            else:
                try:
                    page.context.clear_cookies()
                    print("  cookies cleared")
                except Exception:
                    pass
                time.sleep(random.uniform(20, 30))
                continue

        body = page.content().lower()
        if "not valid" in body or "creation failed" in body:
            print("  >>> EMAIL REJECTED BY SITE")
            return "bad_email"
        if "already taken" in body:
            print("  >>> username taken")
            return "failed"

        if "activation code" in body or "verify your email" in body:
            print("  >>> ACTIVATION CODE STEP")
            code = wait_for_code(acc["email"], acc["mail_token"],
                                 acc["mail_base"], timeout=300)
            if not code:
                print("  no code")
                return "created"
            if not enter_code(page, code):
                return "created"
            time.sleep(4)
            page_text(page)
            snap(page, f"{acc['username']}_activated")
            now = page.content().lower()
            if "invalid" in now or "incorrect" in now or "expired" in now:
                print("  >>> CODE REJECTED")
                return "created"
            if "activation code" in now:
                print("  >>> still asking for a code")
                return "created"
            print("  activated")
            return "ok"

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
        ctx = launch(p)
        ctx.add_init_script("""
            Object.defineProperty(navigator,'webdriver',{get:()=>undefined});
            window.chrome = window.chrome || {};
            window.chrome.runtime = window.chrome.runtime || {};
        """)
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        hook_diag(page)
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

                if status in ("ok", "created"):
                    break
                if status != "bad_email":
                    break
                print("  domain rejected, trying next...")
                time.sleep(3)
            time.sleep(PAUSE)

        ctx.close()

    good = [r for r in results if r.get("status") in ("ok", "created")]
    print("done:", OUT)
    print("created %d of %d account(s)" % (len(good), len(results)))
    if not good:
        print("NO ACCOUNT CREATED - failing the job")
        raise SystemExit(1)

from emailer import (create_inbox, fetch_messages, wait_for_link,
                     wait_for_code, wait_gmail, list_domains, list_mailboxes)
if __name__ == "__main__":
    main()
