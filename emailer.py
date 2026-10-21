"""
emailer.py -- disposable-inbox layer for the PTC bot.

Interface, matching exactly what bot.py calls:
    list_mailboxes()                        -> [(base, domain), ...]
    create_inbox(base, domain)              -> (address, token, base)
    wait_for_link(address, token, base, t)  -> url | None

bot.py rotates to the next (base, domain) every time Pokemon answers
'email not valid', so the length of this list is how many shots you get
at beating the blocklist.

Providers: maildrop, guerrilla (10 domains), yopmail (13 domains),
tempmail.lol, mail.tm, mail.gw, gmail (optional, last).

Env knobs:
    DOMAIN_ORDER=grr.la,maildrop.cc    try these first
    DOMAIN_BLOCK=sharklasers.com       never use these
    EMAIL_PROVIDER=yopmail             only this provider

Probe:  python emailer.py
"""

import http.cookiejar
import json
import os
import random
import re
import string
import time
import urllib.request

UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")

TM = {"mailtm": "https://api.mail.tm", "mailgw": "https://api.mail.gw"}
GM = "https://api.guerrillamail.com/ajax.php"
MD = "https://api.maildrop.cc/graphql"
TMP = "https://api.tempmail.lol/v2"

# Guerrilla aliases all of these to one mailbox.
GM_DOMAINS = ["guerrillamailblock.com", "sharklasers.com", "grr.la",
              "guerrillamail.com", "guerrillamail.net", "guerrillamail.org",
              "guerrillamail.biz", "guerrillamail.de", "spam4.me",
              "pokemail.net"]

# YOPmail aliases all of these to one mailbox too.
YOP_DOMAINS = ["yopmail.com", "yopmail.fr", "yopmail.net", "cool.fr.nf",
               "jetable.fr.nf", "nospam.ze.tc", "nomail.xl.cx", "mega.zik.dj",
               "speed.1s.fr", "courriel.fr.nf", "moncourrier.fr.nf",
               "monemail.fr.nf", "monmail.fr.nf"]


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


def _links(blob):
    blob = (blob or "").replace("&amp;", "&").replace("&#38;", "&")
    out = []
    for u in re.findall(r'https?://[^\s"\'<>)\]]+', blob):
        u = u.rstrip(".,;:)")
        if u not in out:
            out.append(u)
    return out


def _blocked():
    return {d.strip().lower() for d in os.getenv("DOMAIN_BLOCK", "").split(",")
            if d.strip()}


# ----------------------------------------------------------------- maildrop

def _md_links(mailbox):
    q = 'query { inbox(mailbox: "%s") { id subject data html } }' % mailbox
    j = _j(_post(MD, {"query": q})) or {}
    out = []
    for m in (j.get("data", {}) or {}).get("inbox") or []:
        out += _links((m.get("data") or "") + "\n" + (m.get("html") or ""))
    return out


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


def _gm_links(sid):
    j = _j(_open("%s?f=check_email&seq=0&sid_token=%s" % (GM, sid))) or {}
    out = []
    for m in j.get("list") or []:
        body = m.get("mail_body") or ""
        if not body and m.get("mail_id"):
            d = _j(_open("%s?f=fetch_email&email_id=%s&sid_token=%s"
                         % (GM, m["mail_id"], sid))) or {}
            body = d.get("mail_body") or ""
        out += _links((m.get("mail_excerpt") or "") + "\n" + body)
    return out


# ------------------------------------------------------------------ yopmail

def _yop_links(login):
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
            body = _open("https://yopmail.com/en/inc/mail.html?login=%s&id=%s"
                         % (login, mid), opener=op)
        except Exception:
            continue
        out += _links(body)
    if not out:
        try:
            out += _links(_open("https://yopmail.com/en/rss.php?login=" + login))
        except Exception:
            pass
    return out


# --------------------------------------------------------------- tempmail.lol

def _tmp_create():
    j = _j(_post(TMP + "/inbox/create", {})) or {}
    if not j.get("address"):
        raise RuntimeError("tempmail: %s" % j)
    return j["address"], j["token"]


def _tmp_links(token):
    j = _j(_open("%s/inbox?token=%s" % (TMP, token))) or {}
    out = []
    for m in j.get("emails") or []:
        out += _links((m.get("body") or "") + "\n" + (m.get("html") or ""))
    return out


# ------------------------------------------------------------------- mail.tm

def _tm_domains(base):
    txt = _open(base + "/domains")
    j = _j(txt)
    if j is None:                                   # XML fallback
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


def _tm_links(base, token):
    auth = {"Authorization": "Bearer " + token}
    j = _j(_open(base + "/messages", auth))
    msgs = j if isinstance(j, list) else (j or {}).get("hydra:member", [])
    out = []
    for m in msgs or []:
        d = _j(_open(base + "/messages/" + m["id"], auth)) or {}
        html = d.get("html") or []
        body = "\n".join(html) if isinstance(html, list) else str(html)
        out += _links(body + "\n" + (d.get("text") or ""))
    return out


# --------------------------------------------------------------------- gmail

def _gmail_addr(domain):
    local = os.getenv("GMAIL_USER", "").strip().split("@", 1)[0]
    if len(local) < 2:
        return "%s+ptc%s@%s" % (local, _rand(6), domain)
    parts = [local[0]]
    for ch in local[1:]:
        parts.append(("." if random.random() < 0.5 else "") + ch)
    return "".join(parts) + "@" + domain


def _gmail_links(address):
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
            out += _links(html + "\n" + text)
            M.store(num, "+FLAGS", "\\Seen")
    finally:
        try:
            M.logout()
        except Exception:
            pass
    return out


def _payload(part):
    try:
        raw = part.get_payload(decode=True) or b""
        return raw.decode(part.get_content_charset() or "utf-8", "replace")
    except Exception:
        return ""


# ------------------------------------------------------------------ dispatch

def _domains(tag):
    if tag in TM:
        return _tm_domains(TM[tag])
    if tag == "guerrilla":
        return list(GM_DOMAINS)
    if tag == "yopmail":
        return list(YOP_DOMAINS)
    if tag == "maildrop":
        return ["maildrop.cc"]
    if tag == "tempmail":
        return ["auto"]
    if tag == "gmail":
        u = os.getenv("GMAIL_USER", "").strip()
        if "@" in u and os.getenv("GMAIL_APP_PASSWORD"):
            return [u.split("@", 1)[1]]
        return []
    raise RuntimeError("unknown provider " + tag)


def list_mailboxes():
    want = os.getenv("EMAIL_PROVIDER", "").strip().lower()
    bad = _blocked()
    out = []
    for tag in ("maildrop", "guerrilla", "yopmail", "tempmail", "mailtm",
                "mailgw", "gmail"):
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


def create_inbox(base, domain):
    if base == "maildrop":
        return "%s@%s" % (_rand(12), domain), "", base
    if base == "guerrilla":
        addr, sid = _gm_create(domain)
        return addr, sid, base
    if base == "yopmail":
        return "ptc%s@%s" % (_rand(9), domain), _rand(9), base
    if base == "tempmail":
        addr, tok = _tmp_create()
        return addr, tok, base
    if base in TM:
        addr = "ptc%s@%s" % (_rand(8), domain)
        return addr, _tm_create(TM[base], addr, _rand(12) + "Aa1!"), base
    if base == "gmail":
        return _gmail_addr(domain), "", base
    raise RuntimeError("unknown base " + base)


def _poll(base, address, token):
    if base == "maildrop":
        return _md_links(address.split("@")[0])
    if base == "guerrilla":
        return _gm_links(token)
    if base == "yopmail":
        return _yop_links(address.split("@")[0])
    if base == "tempmail":
        return _tmp_links(token)
    if base in TM:
        return _tm_links(TM[base], token)
    if base == "gmail":
        return _gmail_links(address)
    return []


SKIP = ("facebook", "twitter", "instagram", "youtube", "unsubscribe",
        "privacy", "terms", "guerrillamail", "yopmail.com/en")


def wait_for_link(address, token, base, timeout=180):
    end = time.time() + timeout
    seen, fallback = set(), None
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
            if "pokemon" in l.lower():
                print("    activation link:", l[:95])
                return l
            if not fallback and not any(s in l.lower() for s in SKIP):
                fallback = l
        if fallback and time.time() > end - timeout + 60:
            print("    link (fallback):", fallback[:95])
            return fallback
        time.sleep(6)
    if fallback:
        print("    link (late fallback):", fallback[:95])
    return fallback


# --------------------------------------------------------------------- probe

def domain_probe():
    out = {}
    for tag in ("maildrop", "guerrilla", "yopmail", "tempmail", "mailtm",
                "mailgw", "gmail"):
        try:
            out[tag] = _domains(tag)
        except Exception as e:
            out[tag] = ["ERROR: %s" % e]
    return out


list_domains = domain_probe
wait_for_code = wait_for_link
wait_gmail = wait_for_link
wait_code = wait_for_link


if __name__ == "__main__":
    print("--- candidate domains ---")
    total = 0
    for name, doms in domain_probe().items():
        print("%-10s %s" % (name, doms if doms else "(not configured)"))
        total += len(doms)
    print("total:", total)
    try:
        base, domain = list_mailboxes()[0]
        addr, tok, base = create_inbox(base, domain)
        print("test inbox: %s  [provider=%s]" % (addr, base))
    except Exception as e:
        print("test inbox FAILED:", e)
# ---- appended: bot.py also imports fetch_messages ----
def fetch_messages(inbox, token=None, base=None):
    if isinstance(inbox, dict):
        base = inbox.get("provider") or inbox.get("base") or ""
        token = inbox.get("token")
        inbox = inbox.get("address") or ""
    return _poll(base, inbox, token)


# ---- appended: try the fresh mail.gw domain first, then sweep the rest ----
_DEFAULT_ORDER = "westcast-systems.com"
_orig_list_mailboxes = list_mailboxes


def list_mailboxes():
    boxes = _orig_list_mailboxes()
    pref = [d.strip().lower()
            for d in os.getenv("DOMAIN_ORDER", _DEFAULT_ORDER).split(",")
            if d.strip()]
    if pref:
        boxes.sort(key=lambda t: pref.index(t[1].lower())
                   if t[1].lower() in pref else 99)
    return boxes
