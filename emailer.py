"""
emailer.py -- disposable-inbox layer for the PTC bot.

Interface, matching exactly what bot.py calls:
    list_mailboxes()                        -> [(base, domain), ...]
    create_inbox(base, domain)              -> (address, token, base)
    wait_for_link(address, token, base, t)  -> url | None
    wait_for_code(address, token, base, t)  -> "123456" | None

tempmail is the only provider whose domain Pokemon accepts, so it runs
first AND last, with a hard retry on its API.

Env knobs:
    TMP_DOMAIN=some.domain     pin a tempmail domain instead of random
    DOMAIN_ORDER=auto          force a specific domain to the front
    DOMAIN_BLOCK=a.com,b.com   never use these
    EMAIL_PROVIDER=tempmail    only this provider
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

UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
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

ASSET_EXT = (".eot", ".woff", ".woff2", ".ttf", ".css", ".js", ".png", ".jpg",
             ".jpeg", ".gif", ".svg", ".ico", ".webp", ".mp4", ".mp3")


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


def _clean(blob):
    return (blob or "").replace("&amp;", "&").replace("&#38;", "&")


def _links(blob):
    blob = _clean(blob)
    out = []
    for u in re.findall(r'https?://[^\s"\'<>)\]]+', blob):
        u = u.rstrip(".,;:)")
        if u not in out:
            out.append(u)
    return out


def _prep(blob):
    """Strip CSS colours, tags and entities so only visible text remains."""
    t = _clean(blob)
    t = re.sub(r"#[0-9a-fA-F]{3,8}\b", " ", t)
    t = re.sub(r"rgba?\([^)]*\)", " ", t)
    t = re.sub(r"<[^>]+>", " ", t)
    t = t.replace("&nbsp;", " ").replace("&#160;", " ")
    return re.sub(r"\s+", " ", t)


def _find_codes(blob):
    """Candidate 6-digit codes, most likely first."""
    t = _prep(blob)
    out = [m.group(1) for m in re.finditer(r"[Cc]ode[^0-9]{0,60}?(\d{6})\b", t)]
    if out:
        return out
    out = [re.sub(r"\s+", "", m.group(1)) for m in
           re.finditer(r"[Cc]ode[^0-9]{0,60}?((?:\d\s+){5}\d)", t)]
    if out:
        return out
    return [m.group(1) for m in re.finditer(r"\b(\d{6})\b", t)]


def _not_asset(u):
    return not u.split("?")[0].lower().endswith(ASSET_EXT)


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
    return "\n".join("%s\n%s\n%s" % (m.get("subject") or "", m.get("body") or "",
                                     m.get("html") or "")
                     for m in j.get("emails") or [])


# ----------------------------------------------------------------- maildrop

def _md_text(mailbox):
    q = 'query { inbox(mailbox: "%s") { id subject data html } }' % mailbox
    j = _j(_post(MD, {"query": q})) or {}
    return "\n".join("%s\n%s\n%s" % (m.get("subject") or "", m.get("data") or "",
                                     m.get("html") or "")
                     for m in (j.get("data", {}) or {}).get("inbox") or [])


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
    parts = []
    for m in j.get("list") or []:
        body = m.get("mail_body") or ""
        if not body and m.get("mail_id"):
            d = _j(_open("%s?f=fetch_email&email_id=%s&sid_token=%s"
                         % (GM, m["mail_id"], sid))) or {}
            body = d.get("mail_body") or ""
        parts.append("%s\n%s\n%s" % (m.get("mail_subject") or "",
                                     m.get("mail_excerpt") or "", body))
    return "\n".join(parts)


# ------------------------------------------------------------------ yopmail

def _yop_text(login):
    jar = http.cookiejar.CookieJar()
    op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
    op.open(urllib.request.Request("https://yopmail.com/en/?login=" + login,
                                   headers={"User-Agent": UA}), timeout=30).read()
    html = _open("https://yopmail.com/en/inc/inbox.html?login=" + login,
                 opener=op)
    parts = [html]
    ids = re.findall(r'id="(m\d+)"', html) or re.findall(r'id="([a-z]+\d+)"', html)
    for mid in dict.fromkeys(ids):
        try:
            parts.append(_open("https://yopmail.com/en/inc/mail.html?login=%s&id=%s"
                               % (login, mid), opener=op))
        except Exception:
            continue
    try:
        parts.append(_open("https://yopmail.com/en/rss.php?login=" + login))
    except Exception:
        pass
    return "\n".join(parts)


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
    parts = []
    for m in msgs or []:
        d = _j(_open(base + "/messages/" + m["id"], auth)) or {}
        html = d.get("html") or []
        body = "\n".join(html) if isinstance(html, list) else str(html)
        parts.append("%s\n%s\n%s" % (d.get("subject") or "", body,
                                     d.get("text") or ""))
    return "\n".join(parts)


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

    parts = []
    M = imaplib.IMAP4_SSL("imap.gmail.com")
    try:
        M.login(os.getenv("GMAIL_USER", "").strip(),
                os.getenv("GMAIL_APP_PASSWORD", "").strip())
        M.select("INBOX")
        _, data = M.search(None, "TO", address)
        for num in (data[0] or b"").split():
            _, raw = M.fetch(num, "(RFC822)")
            msg = _email.message_from_bytes(raw[0][1])
            subj = msg.get("Subject", "")
            text, html = "", ""
            for part in msg.walk():
                ct = part.get_content_type()
                if ct == "text/plain" and not text:
                    text = _payload(part)
                elif ct == "text/html" and not html:
                    html = _payload(part)
            parts.append("%s\n%s\n%s" % (subj, text, html))
            M.store(num, "+FLAGS", "\\Seen")
    finally:
        try:
            M.logout()
        except Exception:
            pass
    return "\n".join(parts)


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
    # tempmail is the only working domain pool - give it a second chance
    # at the end of the sweep instead of ending the run on a transient blip
    if not want and out and out[-1][0] != "tempmail":
        out.append(("tempmail", "auto"))
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
    """tempmail gets a long retry - its API rate-limits under repeat runs."""
    tries = 6 if base == "tempmail" else 3
    last = None
    for i in range(tries):
        try:
            pair = _create(base, domain)
            if pair:
                return pair[0], pair[1], base
        except Exception as e:
            last = e
            if base == "tempmail":
                print("  >>> TEMPMAIL RETRY %d/%d: %s" % (i + 1, tries, e))
                time.sleep(5 * (i + 1))
            else:
                print("  create retry %d/%d (%s/%s): %s"
                      % (i + 1, tries, base, domain, e))
                time.sleep(2)
    if last:
        raise last
    raise RuntimeError("no inbox from %s" % base)


def _blob(base, address, token):
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


def fetch_messages(inbox, token=None, base=None):
    """bot.py imports this; accepts an inbox dict or (address, token, base)."""
    if isinstance(inbox, dict):
        base = inbox.get("provider") or inbox.get("base") or ""
        token = inbox.get("token")
        inbox = inbox.get("address") or ""
    return _links(_blob(base, inbox, token))


SKIP = ("facebook", "twitter", "instagram", "youtube", "unsubscribe",
        "privacy", "terms", "guerrillamail", "yopmail.com/en")


def wait_for_link(address, token, base, timeout=180):
    end = time.time() + timeout
    seen, fallback = set(), None
    while time.time() < end:
        try:
            links = _links(_blob(base, address, token))
        except Exception as e:
            print("    poll error:", e)
            links = []
        for l in links:
            if l in seen:
                continue
            seen.add(l)
            if not _not_asset(l):
                continue
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


def wait_for_code(address, token, base, timeout=300, avoid=None):
    """Six-digit activation code. 'avoid' skips a code already tried."""
    end = time.time() + timeout
    buf, shown = "", False
    while time.time() < end:
        try:
            buf += "\n" + _blob(base, address, token)
        except Exception as e:
            print("    code poll error:", e)
        if buf.strip() and not shown:
            print("    mail text:", _prep(buf)[:300])
            shown = True
        for c in _find_codes(buf):
            if avoid and c == avoid:
                continue
            print("    activation code:", c)
            return c
        time.sleep(6)
    print("    no activation code arrived")
    return None


# --------------------------------------------------------------------- probe

def domain_probe():
    out = {}
    for tag in ORDER:
        try:
            out[tag] = _domains(tag)
        except Exception as e:
            out[tag] = ["ERROR: %s" % e]
    return out


list_domains = domain_probe
wait_gmail = wait_for_link


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
