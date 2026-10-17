import csv, os, random, re, string, subprocess, tempfile, time

import requests
from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

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

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36")

WORDS = ["ash","misty","brock","pika","gengar","eevee","snorlax","gyara",
         "dragon","mewtwo","lapras","zapdos","onix","psy","golem","machop"]

os.makedirs(DBG, exist_ok=True)

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

def dump_frames(page):
    print("  --- frames ---")
    for fr in all_frames(page):
        try:
            n = len(fr.query_selector_all("input, select, button, iframe"))
            print("    frame:", (fr.url or "")[:100], "| elements=", n)
        except Exception:
            pass

def dump_iframes(page):
    print("  --- iframes ---")
    for el in page.query_selector_all("iframe"):
        try:
            print("    iframe src:", (el.get_attribute("src") or "")[:110])
        except Exception:
            pass

def dump_fields(page, tag=""):
    print(f"  --- fields {tag} ---")
    for fr in all_frames(page):
        for el in fr.query_selector_all("input, select, button"):
            try:
                t = el.evaluate("e => e.tagName.toLowerCase()")
                txt = (el.inner_text() or "").strip()[:30]
                print("   ", t, "name=", el.get_attribute("name"),
                      "id=", el.get_attribute("id"),
                      "type=", el.get_attribute("type"), "text=", txt)
            except Exception:
                pass

def dump_select_options(page):
    for fr in all_frames(page):
        for el in fr.query_selector_all("select"):
            try:
                opts = [o.inner_text().strip() for o in el.query_selector_all("option")][:8]
                print("    select name=", el.get_attribute("name"),
                      "id=", el.get_attribute("id"), "opts=", opts)
            except Exception:
                pass

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

MAIL = "https://api.mail.tm"

def create_inbox():
    domain = requests.get(f"{MAIL}/domains").json()["hydra:member"][0]["domain"]
    addr = "".join(random.choices(string.ascii_lowercase + string.digits, k=10)) + "@" + domain
    pw = "".join(random.choices(string.ascii_letters + string.digits, k=12))
    requests.post(f"{MAIL}/accounts", json={"address": addr, "password": pw}).raise_for_status()
    tok = requests.post(f"{MAIL}/token", json={"address": addr, "password": pw}).json()["token"]
    return addr, tok

def wait_for_link(addr, tok, timeout=180):
    h = {"Authorization": f"Bearer {tok}"}
    end = time.time() + timeout
    seen = 0
    while time.time() < end:
        r = requests.get(f"{MAIL}/messages", headers=h)
        if r.status_code == 200:
            msgs = r.json().get("hydra:member", [])
            if len(msgs) != seen:
                seen = len(msgs)
                print(f"    inbox has {seen} message(s)")
            for m in msgs:
                frm = (m.get("from") or {}).get("address", "").lower()
                subj = (m.get("subject") or "")[:60]
                print(f"    msg from={frm} subj={subj}")
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

def solve_any(page, wait=8):
    end = time.time() + wait
    while time.time() < end:
        if _frames(page, "recaptcha/api2/anchor"):
            return solve_recaptcha(page)
        if _frames(page, "hcaptcha.com"):
            return solve_hcaptcha(page)
        time.sleep(0.5)
    raise CaptchaFail("no captcha frame found")

def wait_for_ready(page, timeout=60):
    """Wait until either a form or a captcha appears."""
    end = time.time() + timeout
    while time.time() < end:
        if _frames(page, "hcaptcha.com") or _frames(page, "recaptcha/api2"):
            return "captcha"
        if field_count(page) >= 2:
            return "form"
        time.sleep(1.5)
    return "none"

def fill_label(page, label, value):
    xp = (f'contains(translate(.,"ABCDEFGHIJKLMNOPQRSTUVWXYZ",'
          f'"abcdefghijklmnopqrstuvwxyz"),"{label.lower()}")')
    for sel in [f'xpath=//label[{xp}]/following::input[1]',
                f'input[name="{label.lower()}"]',
                f'input[id*="{label.lower()}"]',
                f'input[placeholder*="{label}" i]']:
        el = find_any(page, sel)
        if el:
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
    xp = (f'contains(translate(.,"ABCDEFGHIJKLMNOPQRSTUVWXYZ",'
          f'"abcdefghijklmnopqrstuvwxyz"),"{label.lower()}")')
    el = find_any(page, f'xpath=//label[{xp}]/preceding::input[@type="checkbox"][1]')
    if el:
        try:
            if not el.is_checked():
                el.check()
            return True
        except Exception:
            pass
    return False

def click_next(page):
    for txt in ["Continue", "Next", "Sign Up", "Sign up", "Create Account",
                "Create account", "Submit", "Register", "Verify"]:
        for sel in [f'button:has-text("{txt}")', f'a:has-text("{txt}")',
                    f'input[value*="{txt}"]']:
            el = find_any(page, sel)
            if el:
                try:
                    print(f"  clicked: {txt}")
                    el.click()
                    time.sleep(5)
                    return True
                except Exception:
                    pass
    el = find_any(page, 'button[type="submit"]')
    if el:
        try:
            print("  clicked: submit")
            el.click()
            time.sleep(5)
            return True
        except Exception:
            pass
    return False

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

        ctx.add_init_script(
            "Object.defineProperty(navigator,'webdriver',{get:()=>undefined});"
        )def click_text(page, text):
    for sel in [f'button:has-text("{text}")', f'a:has-text("{text}")',
                f'input[value*="{text}"]']:
        el = find_any(page, sel)
        if el:
            try:
                el.click()
                print(f"  clicked: {text}")
                time.sleep(4)
                return True
            except Exception:
                pass
    return False


def one_account(page, acc):
    print("  goto:", SIGNUP_URL)
    try:
        page.goto(SIGNUP_URL, wait_until="domcontentloaded", timeout=90000)
    except Exception as e:
        print("  goto failed:", e)
    time.sleep(8)
    print("  landed on:", page.url)
    page_text(page)
    snap(page, f"{acc['username']}_1_landed")

    for attempt in range(3):
        state = wait_for_ready(page, 45)
        print(f"  round {attempt}: state={state}")
        if state == "form":
            break
        if state == "captcha":
            try:
                solve_any(page, wait=5)
            except CaptchaFail as e:
                print("  captcha fail:", e)
            time.sleep(6)
            continue
        time.sleep(10)

    if field_count(page) == 0:
        print("  still no form")
        snap(page, f"{acc['username']}_nofields")
        dump_frames(page)
        return False

    # ---- STEP 1: age gate ----
    snap(page, f"{acc['username']}_2_agegate")
    dump_fields(page, "agegate")
    set_select(page, "country", COUNTRY)
    set_select(page, "year", str(DOB_YEAR))
    set_select(page, "month", MONTH_NAME, index_fallback=DOB_MONTH)
    set_select(page, "day", str(DOB_DAY), index_fallback=DOB_DAY)
    time.sleep(2)
    snap(page, f"{acc['username']}_3_agegate_filled")
    click_text(page, "Continue")
    time.sleep(5)

    # ---- STEP 2: confirm birthdate ----
    snap(page, f"{acc['username']}_4_confirm")
    dump_fields(page, "confirm")
    if click_text(page, "Yes, This Is Correct"):
        print("  birthdate confirmed")
        time.sleep(6)

    # ---- STEP 3: account details ----
    for _ in range(4):
        if find_any(page, 'input[type="password"]'):
            break
        print("  waiting for account fields...")
        time.sleep(5)

    snap(page, f"{acc['username']}_5_account")
    dump_fields(page, "account")

    r = {}
    r["username"] = fill_label(page, "username", acc["username"])
    r["password"] = fill_label(page, "password", acc["password"])
    r["email"]    = fill_label(page, "email", acc["email"])
    r["confirm"]  = fill_label(page, "confirm", acc["email"])
    r["agree"]    = check_label(page, "agree")
    r["terms"]    = check_label(page, "terms")
    print("  filled:", r)

    snap(page, f"{acc['username']}_6_account_filled")
    click_text(page, "Continue")
    click_text(page, "Sign Up")
    time.sleep(8)
    print("  after details url:", page.url)
    snap(page, f"{acc['username']}_7_afterdetails")
    dump_fields(page, "afterdetails")

    # ---- any remaining steps ----
    for n in range(2, 7):
        if field_count(page) == 0:
            break
        print(f"  --- extra step {n} ---")
        if click_text(page, "Yes, This Is Correct"):
            time.sleep(6)
            snap(page, f"{acc['username']}_x{n}_confirm")
            dump_fields(page, f"step{n}")
            continue
        fill_label(page, "username", acc["username"])
        fill_label(page, "password", acc["password"])
        fill_label(page, "email", acc["email"])
        check_label(page, "agree")
        check_label(page, "terms")
        snap(page, f"{acc['username']}_x{n}_filled")
        if not (click_text(page, "Continue") or click_text(page, "Sign Up")):
            break
        time.sleep(7)
        print(f"  after step{n} url:", page.url)
        dump_fields(page, f"after{n}")

    body = page.content().lower()
    for bad in ("already taken", "already exists", "not available", "invalid",
                "required", "error", "verify"):
        if bad in body:
            print(f"  page mentions: {bad}")

    print("  waiting for email...")
    link = wait_for_link(acc["email"], acc["mail_token"], timeout=180)
    if not link:
        print("  no email")
        return False
    print("  link:", link[:80])
    page.goto(link, wait_until="domcontentloaded", timeout=90000)
    time.sleep(3)
    print("  activated")
    return True


def main():
    results = []
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False, args=[
            "--no-sandbox",
            "--disable-blink-features=AutomationControlled",
            "--disable-dev-shm-usage",
        ])
        ctx = browser.new_context(
            viewport={"width": 1366, "height": 900},
            locale="en-US",
        )
        ctx.add_init_script(
            "Object.defineProperty(navigator,'webdriver',{get:()=>undefined});"
        )
        page = ctx.new_page()

        for i in range(COUNT):
            print(f"[{i+1}/{COUNT}] start")
            acc = {"username": random_username(), "password": random_password()}
            acc["email"], acc["mail_token"] = create_inbox()
            print("  email:", acc["email"])
            try:
                acc["status"] = "ok" if one_account(page, acc) else "failed"
            except PWTimeout as e:
                print("  timeout:", e); acc["status"] = "timeout"
            except Exception as e:
                print("  error:", repr(e)); acc["status"] = "error"

            results.append(acc)
            push({k: acc[k] for k in ("username", "password", "email", "status")})
            with open(OUT, "w", newline="") as f:
                w = csv.DictWriter(f, fieldnames=["username","password","email","status"],
                                   extrasaction="ignore")
                w.writeheader(); w.writerows(results)
            print(f"  -> {acc['username']} [{acc['status']}]")
            time.sleep(PAUSE)

        browser.close()
    print("done:", OUT)


if __name__ == "__main__":
    main()
