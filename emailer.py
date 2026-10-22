"""
emailer.py -- disposable-inbox layer for the PTC bot.

Interface used by bot.py:
    list_mailboxes()                        -> [(base, domain), ...]
    create_inbox(base, domain)              -> (address, token, base)
    wait_for_link(address, token, base, t)  -> url | None
    wait_for_code(address, token, base, t)  -> code | None
"""

import http.cookiejar
import json
import os
import random
import re
import socket
import string
import time
import urllib.parse
import urllib.request

# --------------------------------------------------------------- IPv4 first
_gai = socket.getaddrinfo


def _gai4(host, port, family=0, type=0, proto=0, flags=0):
    try:
        return _gai(host, port, socket.AF_INET, type, proto, flags)
    except Exception:
        return _gai(host, port, family, type, proto, flags)


socket.getaddrinfo = _gai4

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")

TM = {"mailtm": "https://api.mail.tm", "mailgw": "https://api.mail.gw"}
GM = "https://api.guerrillamail.com/ajax.php"
MD = "https://api.maildrop.cc/graphql"
TMP = "https://api.tempmail.lol/v2"

GM_DOMAINS = ["guerrillamailblock.com", "sharklasers.com", "grr.la",
              "guerrillamail.com", "guerrillamail.net", "guerrillamail.org",
              "guerrillamail.biz", "guerrillamail.de", "spam4.me",
              "pokemail.net"]

YOP_DOMAINS = ["yopmail.com", "yopmail.fr", "yopmail.net", "cool.fr.nf",
               "jetable.fr.nf", "nospam.ze.tc", "nomail.xl.cx", "mega.zik.dj",
               "speed.1s.fr", "courriel.fr.nf", "moncourrier.fr.nf",
               "monemail.fr.nf", "monmail.fr.nf"]

ASSET_EXT = (".eot", ".woff", ".woff2", ".ttf", ".otf", ".css", ".js",
             ".png", ".jpg", ".jpeg", ".gif", ".svg", ".ico", ".webp",
             ".mp4", ".pdf", ".zip")


# ------------------------------------------------------------------ plumbing

def _open(url, headers=None, timeout=30, opener=None):
    h = {"User-Agent": UA, "Accept": "application/json"}
    if headers:
        h.update(headers)
    req = urllib.request.Request(url, headers=h)
    op = opener or urllib.request.build_opener()
    with op.open(req, timeout=timeout) as r:
        return r.read().decode("utf-8", "replace")


def _post(url, obj, timeout=30):
    h = {"User-Agent": UA, "Accept": "application/json",
         "Content-Type": "application/json"}
    req = urllib.request.Request(url, data=json.dumps(obj).encode(),
                                 headers=h, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", "replace")


def _j(txt):
    try:
        return json.loads(txt)
    except Exception:
        return None


def _rand(n=10):
    return "".join(random.choice(string.ascii_lowercase + string.digits)
                   for _ in range(n))


def _strip_html(s):
    s = re.sub(r"(?is)<(script|style).*?</\1>", " ", s or "")
    s = re.sub(r"(?s)<[^>]+>", " ", s)
    s = s.replace("&nbsp;", " ").replace("&amp;", "&")
    return re.sub(r"\s+", " ", s).strip()


def _is_asset(u):
    return urllib.parse.urlparse(u).path.lower().endswith(ASSET_EXT)


def _links(blob, drop_assets=True):
    blob = (blob or "").replace("&amp;", "&").replace("&#38;", "&")
    out = []
    for u in re.findall(r'https?://[^\s"\'<>)\]]+', blob):
        u = u.rstrip(".,;:)")
        if drop_assets and _is_asset(u):
            continue
        if u not in out:
            out.append(u)
    return out


def _blocked():
    return {d.strip().lower() for d in os.getenv("DOMAIN_BLOCK", "").split(",")
            if d.strip()}


def _retry(fn, tries=4, label="", pause=3):
    last = None
    for i in range(tries):
        try:
            out = fn()
            if out:
                return out
        except Exception as e:
            last = e
            print("  %s retry %d: %s" % (label, i + 1, e))
        time.sleep(pause)
    if last:
        raise last
    return None


# --------------------------------------------------------------- tempmail.lol

def _tmp_create(domain=None):
    body = {}
    if domain and domain != "auto":
        body["domain"] = domain
    j = _j(_post(TMP + "/inbox/create", body)) or {}
    if not j.get("address"):
        raise RuntimeError("tempmail: %s" % j)
    print("    tempmail gave: %s" % j["address"])
    return j["address"], j["token"]


def _tmp_text(token):
    url = "%s/inbox?token=%s" % (TMP, urllib.parse.quote(token, safe=""))
    j = _j(_open(url)) or {}
    out = []
    for m in j.get("emails") or []:
        out.append("SUBJ %s\n%s\n%s" % (m.get("subject") or "",
                                        m.get("body") or "", m.get("html") or ""))
    if not out and j:
        out.append(json.dumps(j))
    return "\n".join(out)


# ----------------------------------------------------------------- maildrop

def _md_text(mailbox):
    q = 'query { inbox(mailbox: "%s") { id subject data html } }' % mailbox
    j = _j(_post(MD, {"query": q})) or {}
    out = []
    for m in (j.get("data", {}) or {}).get("inbox") or []:
        out.append("SUBJ %s\n%s\n%s" % (m.get("subject") or "",
                                        m.get("data") or "", m.get("html") or ""))
    return "\n".join(out)


# ---------------------------------------------------------------- guerrilla

def _gm_create(domain):
    u = "ptc" + _rand(8)
    j0 = _j(_open("%s?f=get_email_address&site=%s" % (GM, domain))) or {}
    sid = j0.get("sid_token") or ""
    if not sid:
        raise RuntimeError("guerrilla: no session")
    _open("%s?f=set_email_user&email_user=%s&sid_token=%s&site=%s"
          % (GM, u, sid, domain))
    return "%s@%s" % (u, domain), sid


def _gm_text(sid):
    j = _j(_open("%s?f=check_email&seq=0&sid_token=%s" % (GM, sid))) or {}
    out = []
    for m in j.get("list") or []:
        body = m.get("mail_body") or ""
        if not body and m.get("mail_id"):
            d = _j(_open("%s?f=fetch_email&email_id=%s&sid_token=%s"
                         % (GM, m["mail_id"], sid))) or {}
            body = d.get("mail_body") or ""
        out.append("SUBJ %s\n%s\n%s" % (m.get("mail_subject") or "",
                                        m.get("mail_excerpt") or "", body))
    return "\n".join(out)


# ------------------------------------------------------------------ yopmail

def _yop_text(login):
    jar = http.cookiejar.CookieJar()
    op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
    op.open(urllib.request.Request("https://yopmail.com/en/?login=" + login,
                                   headers={"User-Agent": UA}), timeout=30).read()
    html = _open("https://yopmail.com/en/inc/inbox.html?login=" + login,
                 opener=op)
    out = []
    ids = re.findall(r'id="(m\d+)"', html) or re.findall(r'id="([a-z]+\d+)"', html)
    for mid in dict.fromkeys(ids):
        try:
            out.append(_open("https://yopmail.com/en/inc/mail.html?login=%s&id=%s"
                             % (login, mid), opener=op))
        except Exception:
            continue
    if not out:
        try:
            out.append(_open("https://yopmail.com/en/rss.php?login=" + login))
        except Exception:
            pass
    return "\n".join(out)


# ------------------------------------------------------------------- mail.tm

def _tm_domains(base):
    txt = _open(base + "/domains")
    j = _j(txt)
    if j is None:
        return re.findall(r"<domain>([^<]+)</domain>", txt)
    rows = j if isinstance(j, list) else None
    if rows is None:
        for k in ("hydra:member", "items", "domains"):
            if isinstance(j.get(k), list):
                rows = j[k]
                break
    out = []
    for d in rows or []:
        if isinstance(d, dict) and d.get("domain") \
                and str(d.get("isActive", 1)).lower() not in ("0", "false"):
            out.append(d["domain"])
    return out


def _tm_create(base, address, password):
    _post(base + "/accounts", {"address": address, "password": password})
    tok = _j(_post(base + "/token",
                   {"address": address, "password": password})) or {}
    if not tok.get("token"):
        raise RuntimeError("mail.tm: no token")
    return tok["token"]


def _tm_text(base, token):
    auth = {"Authorization": "Bearer " + token}
    j = _j(_open(base + "/messages", auth))
    msgs = j if isinstance(j, list) else (j or {}).get("hydra:member", [])
    out = []
    for m in msgs or []:
        d = _j(_open(base + "/messages/" + m["id"], auth)) or {}
        html = d.get("html") or []
        out.append("SUBJ %s\n%s\n%s" % (d.get("subject") or "",
                                        d.get("text") or "",
                                        "\n".join(html) if isinstance(html, list)
                                        else str(html)))
    return "\n".join(out)


# --------------------------------------------------------------------- gmail

def _gmail_addr(domain):
    local = os.getenv("GMAIL_USER", "").strip().split("@", 1)[0]
    if len(local) < 2:
        return "%s+ptc%s@%s" % (local, _rand(6), domain)
    parts = [local[0]]
    for ch in local[1:]:
        parts.append(("." if random.random() < 0.5 else "") + ch)
    return "".join(parts) + "@" + domain


def _gmail_text(address):
    import email as _email
    import imaplib

    out = []
    M = imaplib.IMAP4_SSL("imap.gmail.com")
    try:
        M.login(os.getenv("GMAIL_USER", "").strip(),
                os.getenv("GMAIL_APP_PASSWORD", "").strip())
        M.select("INBOX")
        _, data = M.search(None, "TO", address)
        for num in (data[0] or b"").split():
            _, raw = M.fetch(num, "(RFC822)")
            msg = _email.message_from_bytes(raw[0][1])
            text, html = "", ""
            for part in msg.walk():
                ct = part.get_content_type()
                if ct == "text/plain" and not text:
                    text = _payload(part)
                elif ct == "text/html" and not html:
                    html = _payload(part)
            out.append("SUBJ %s\n%s\n%s" % (msg.get("Subject", ""), text, html))
            M.store(num, "+FLAGS", "\\Seen")
    finally:
        try:
            M.logout()
        except Exception:
            pass
    return "\n".join(out)


def _payload(part):
    try:
        raw = part.get_payload(decode=True) or b""
        return raw.decode(part.get_content_charset() or "utf-8", "replace")
    except Exception:
        return ""


# ------------------------------------------------------------------ dispatch

def _domains(tag):
    if tag in TM:
        return _retry(lambda: _tm_domains(TM[tag]), tries=4, label=tag)
    if tag == "tempmail":
        return [os.getenv("TMP_DOMAIN", "").strip() or "auto"]
    if tag == "guerrilla":
        return list(GM_DOMAINS)
    if tag == "yopmail":
        return list(YOP_DOMAINS)
    if tag == "maildrop":
        return ["maildrop.cc"]
    if tag == "gmail":
        u = os.getenv("GMAIL_USER", "").strip()
        if "@" in u and os.getenv("GMAIL_APP_PASSWORD"):
            return [u.split("@", 1)[1]]
        return []
    raise RuntimeError("unknown provider " + tag)


ORDER = ("tempmail", "mailgw", "maildrop", "guerrilla", "yopmail",
         "mailtm", "gmail")


def list_mailboxes():
    want = os.getenv("EMAIL_PROVIDER", "").strip().lower()
    bad = _blocked()
    out = []
    for tag in ORDER:
        if want and tag != want:
            continue
        try:
            doms = _domains(tag)
        except Exception as e:
            print("  %s unavailable: %s" % (tag, e))
            continue
        for d in doms:
            if d.lower() not in bad:
                out.append((tag, d))
    pref = [x.strip().lower() for x in os.getenv("DOMAIN_ORDER", "").split(",")
            if x.strip()]
    if pref:
        out.sort(key=lambda t: pref.index(t[1].lower())
                 if t[1].lower() in pref else 99)
    return out


def _create(base, domain):
    if base == "tempmail":
        return _tmp_create(domain)
    if base == "maildrop":
        return "%s@%s" % (_rand(12), domain), ""
    if base == "guerrilla":
        return _gm_create(domain)
    if base == "yopmail":
        return "ptc%s@%s" % (_rand(9), domain), _rand(9)
    if base in TM:
        addr = "ptc%s@%s" % (_rand(8), domain)
        return addr, _tm_create(TM[base], addr, _rand(12) + "Aa1!")
    if base == "gmail":
        return _gmail_addr(domain), ""
    raise RuntimeError("unknown base " + base)


def create_inbox(base, domain):
    pair = _retry(lambda: _create(base, domain), tries=3,
                  label="create %s/%s" % (base, domain), pause=2)
    if not pair:
        raise RuntimeError("no inbox from %s" % base)
    addr, token = pair
    return addr, token, base


def _poll_text(base, address, token):
    if base == "tempmail":
        return _tmp_text(token)
    if base == "maildrop":
        return _md_text(address.split("@")[0])
    if base == "guerrilla":
        return _gm_text(token)
    if base == "yopmail":
        return _yop_text(address.split("@")[0])
    if base in TM:
        return _tm_text(TM[base], token)
    if base == "gmail":
        return _gmail_text(address)
    return ""


def _poll(base, address, token):
    return _links(_poll_text(base, address, token))


def fetch_messages(inbox, token=None, base=None):
    if isinstance(inbox, dict):
        base = inbox.get("provider") or inbox.get("base") or ""
        token = inbox.get("token")
        inbox = inbox.get("address") or ""
    return _links(_poll_text(base, inbox, token))


SKIP = ("facebook", "twitter", "instagram", "youtube", "unsubscribe",
        "privacy", "terms", "guerrillamail", "yopmail.com/en")
HINT = ("activ", "verif", "confirm", "token", "signup", "join", "validate")

CODE_STOP = {"pokemon", "https", "http", "trainer", "central", "please",
             "account", "verify", "activation", "email", "below", "minutes",
             "inactive", "complete", "step", "code", "enter", "will", "your",
             "this", "that", "click", "here", "team", "support", "inc",
             "nickelodeon", "creatures", "game", "freak", "copyright", "the"}


def _score(u):
    s = 0
    low = u.lower()
    if "pokemon" in low:
        s += 2
    if any(k in low for k in HINT):
        s += 3
    if "static.prod" in low:
        s -= 1
    return s


def wait_for_link(address, token, base, timeout=180):
    end = time.time() + timeout
    seen, best = set(), None
    while time.time() < end:
        try:
            links = _poll(base, address, token)
        except Exception as e:
            print("    poll error:", e)
            links = []
        for l in links:
            if l in seen:
                continue
            seen.add(l)
            if _score(l) >= 4:
                print("    activation link:", l[:100])
                return l
            if not best and not any(s in l.lower() for s in SKIP):
                best = l
        if best and time.time() > end - timeout + 60:
            print("    link (fallback):", best[:100])
            return best
        time.sleep(6)
    if best:
        print("    link (late fallback):", best[:100])
    return best


def _code_from(text):
    flat = _strip_html(text)
    m = re.search(r"\b(\d{6})\b", flat)
    if m:
        return m.group(1)
    for w in re.findall(r"\b([A-Za-z0-9]{5,10})\b", flat):
        if w.lower() in CODE_STOP:
            continue
        if any(c.isdigit() for c in w):
            return w
    return None


def wait_for_code(address, token, base, timeout=180):
    end = time.time() + timeout
    while time.time() < end:
        try:
            text = _poll_text(base, address, token)
        except Exception as e:
            print("    poll error:", e)
            text = ""
        if text.strip():
            print("    mail text:", repr(_strip_html(text)[:220]))
            code = _code_from(text)
            if code:
                print("    code:", code)
                return code
        time.sleep(6)
    print("    no code found")
    return None


list_domains = lambda: {t: _domains(t) for t in ORDER}
wait_gmail = wait_for_link
wait_for_code_alias = wait_for_code


if __name__ == "__main__":
    print("--- candidate domains ---")
    total = 0
    for name in ORDER:
        try:
            doms = _domains(name)
        except Exception as e:
            doms = ["ERROR: %s" % e]
        print("%-10s %s" % (name, doms if doms else "(not configured)"))
        total += len(doms)
    print("total:", total)
    try:
        base, domain = list_mailboxes()[0]
        addr, tok, base = create_inbox(base, domain)
        print("test inbox: %s  [provider=%s]" % (addr, base))
    except Exception as e:
        print("test inbox FAILED:", e)
