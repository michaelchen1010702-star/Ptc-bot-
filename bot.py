import csv, os, random, re, string, subprocess, tempfile, time

import requests
from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

# ----------------------------------------------------------------- config
SIGNUP_URL = "https://club.pokemon.com/us/pokemon-trainer-club/sign-up/"
COUNTRY    = "United States"
DOB_YEAR, DOB_MONTH, DOB_DAY = 1990, 1, 1
COUNT      = int(os.getenv("COUNT", "1"))
PAUSE      = 8
OUT        = "accounts.csv"

SHEETS_URL   = os.getenv("SHEETS_URL", "")
SHEETS_TOKEN = os.getenv("SHEETS_TOKEN", "")

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36")

WORDS = ["ash","misty","brock","pika","gengar","eevee","snorlax","gyara",
         "dragon","mewtwo","lapras","zapdos","onix","psy","golem","machop"]

# ------------------------------------------------------------ account gen
def random_username():
    base = random.choice(WORDS) + random.choice(WORDS)
    tail = "".join(random.choices(string.digits, k=random.randint(2, 5)))
    return (base + tail)[:16]

def random_password():
    pw = list(random.choice(string.ascii_uppercase)
              + "".join(random.choices(string.ascii_lowercase, k=6))
              + random.choice(string.digits)
              + "".join(random.choices(string.ascii_letters + string.digits, k=4)))
    random.shuffle(pw)
    return "".join(pw)[:16]

# ---------------------------------------------------------------- mail.tm
MAIL = "https://api.mail.tm"

def create_inbox():
    domain = requests.get(f"{MAIL}/domains").json()["hydra:member"][0]["domain"]
    addr = "".join(random.choices(string.ascii_lowercase + string.digits, k=10)) + "@" + domain
    pw = "".join(random.choices(string.ascii_letters + string.digits, k=12))
    requests.post(f"{MAIL}/accounts", json={"address": addr, "password": pw}).raise_for_status()
    tok = requests.post(f"{MAIL}/token", json={"address": addr, "password": pw}).json()["token"]
    return addr, tok

def wait_for_link(addr, tok, timeout=240):
    h = {"Authorization": f"Bearer {tok}"}
    end = time.time() + timeout
    while time.time() < end:
        r = requests.get(f"{MAIL}/messages", headers=h)
        if r.status_code == 200:
            for m in r.json().get("hydra:member", []):
                frm = (m.get("from") or {}).get("address", "").lower()
                if "pokemon" in frm:
                    full = requests.get(f"{MAIL}/messages/{m['id']}", headers=h).json()
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
    for f in page.frames:
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

def solve_recaptcha(page, timeout=180):
    end = time.time() + timeout
    anchor = None
    while time.time() < end and not anchor:
        anchor = _frames(page, "recaptcha/api2/anchor")
        time.sleep(0.5)
    if not anchor:
        raise CaptchaFail("no recaptcha")
    _click_first(anchor, ["#recaptcha-anchor"])
    time.sleep(3)
    a = anchor.query_selector("#recaptcha-anchor")
    if a and a.get_attribute("aria-checked") == "true":
        return True
    for _ in range(5):
        bf = _frames(page, "recaptcha/api2/bframe")
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

def solve_any(page, wait=10):
    end = time.time() + wait
    while time.time() < end:
        if _frames(page, "hcaptcha.com"):
            return solve_hcaptcha(page)
        if _frames(page, "recaptcha/api2"):
            return solve_recaptcha(page)
        time.sleep(0.5)
    return True

# ------------------------------------------------------------- form fill
def fill_label(page, label, value):
    xp = (f'contains(translate(.,"ABCDEFGHIJKLMNOPQRSTUVWXYZ",'
          f'"abcdefghijklmnopqrstuvwxyz"),"{label.lower()}")')
    for sel in [f'xpath=//label[{xp}]/following::input[1]',
                f'xpath=//label[{xp}]/following::select[1]',
                f'input[name="{label.lower()}"]',
                f'input[placeholder*="{label}" i]']:
        try:
            el = page.query_selector(sel)
            if not el:
                continue
            if el.evaluate("e => e.tagName.toLowerCase()") == "select":
                try: el.select_option(label=value)
                except Exception: el.select_option(value=value)
            else:
                el.fill(value)
            return True
        except Exception:
            continue
    return False

def check_label(page, label):
    xp = (f'contains(translate(.,"ABCDEFGHIJKLMNOPQRSTUVWXYZ",'
          f'"abcdefghijklmnopqrstuvwxyz"),"{label.lower()}")')
    try:
        el = page.query_selector(f'xpath=//label[{xp}]/preceding::input[@type="checkbox"][1]')
        if el and not el.is_checked():
            el.check()
    except Exception:
        pass

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
def one_account(page, acc):
    page.goto(SIGNUP_URL, wait_until="domcontentloaded", timeout=90000)
    time.sleep(4)

    if _frames(page, "hcaptcha.com"):
        print("  security check...")
        solve_hcaptcha(page)
        for _ in range(30):
            if not _frames(page, "hcaptcha.com"):
                break
            time.sleep(1)

    time.sleep(3)
    fill_label(page, "country", COUNTRY)
    fill_label(page, "month", str(DOB_MONTH).zfill(2))
    fill_label(page, "day", str(DOB_DAY).zfill(2))
    fill_label(page, "year", str(DOB_YEAR))
    fill_label(page, "username", acc["username"])
    fill_label(page, "password", acc["password"])
    fill_label(page, "email", acc["email"])
    fill_label(page, "confirm", acc["email"])
    check_label(page, "agree")
    check_label(page, "terms")

    solve_any(page)

    for sel in ['button:has-text("Sign Up")', 'button:has-text("Continue")',
                'button[type="submit"]']:
        try:
            b = page.query_selector(sel)
            if b:
                b.click()
                break
        except Exception:
            continue

    time.sleep(6)
    if "already" in page.content().lower():
        print("  already taken")
        return False

    print("  waiting for email...")
    link = wait_for_link(acc["email"], acc["mail_token"])
    if not link:
        print("  no email")
        return False
    page.goto(link, wait_until="domcontentloaded", timeout=90000)
    time.sleep(3)
    print("  activated")
    return True

def main():
    results = []
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, args=["--no-sandbox"])
        ctx = browser.new_context(user_agent=UA)
        page = ctx.new_page()

        for i in range(COUNT):
            print(f"[{i+1}/{COUNT}] start")
            acc = {"username": random_username(), "password": random_password()}
            acc["email"], acc["mail_token"] = create_inbox()
            try:
                acc["status"] = "ok" if one_account(page, acc) else "failed"
            except PWTimeout as e:
                print("  timeout:", e); acc["status"] = "timeout"
            except Exception as e:
                print("  error:", e); acc["status"] = "error"

            results.append(acc)
            push({k: acc[k] for k in ("username", "password", "email", "status")})
            with open(OUT, "w", newline="") as f:
                w = csv.DictWriter(f, fieldnames=["username","password","email","status"])
                w.writeheader(); w.writerows(results)
            print(f"  -> {acc['username']} [{acc['status']}]")
            time.sleep(PAUSE)

        browser.close()
    print("done:", OUT)

if __name__ == "__main__":
    main()
