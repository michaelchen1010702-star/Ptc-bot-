"""
emailer.py -- free disposable-inbox layer for the PTC bot.

Matches the exact interface bot.py calls:
    list_mailboxes()                        -> [(base, domain), ...]
    create_inbox(base, domain)              -> (address, token, base)
    wait_for_link(address, token, base, t)  -> url | None

Providers, in try-order (bot rotates on 'bad_email'):
    maildrop    maildrop.cc      GraphQL API, no signup
    guerrilla   10 domains       ajax API, no signup
    mailtm      maxxspace.com    REST API
    mailgw      mail.gw          REST API (same shape as mail.tm)
    gmail       gmail.com        dot-trick, needs secrets (optional)

Env knobs:
    DOMAIN_BLOCK=maxxspace.com,sharklasers.com    skip these
    EMAIL_PROVIDER=guerrilla                      only this provider
    DOMAIN_ORDER=gmail.com,maildrop.cc            try these first

Probe:  python emailer.py
"""

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

# Guerrilla aliases every one of these to the same mailbox.
GM_DOMAINS = ["guerrillamailblock.com", "sharklasers.com", "grr.la",
              "guerrillamail.com", "guerrillamail.net", "guerrillamail.org",
              "guerrillamail.biz", "guerrillamail.de", "spam4.me",
              "pokemail.net"]


# ------------------------------------------------------------------ plumbing

def _get(url, headers=None, timeout=30):
    h = {"User-Agent": UA, "Accept": "application/json"}
    if headers:
        h.update(headers)
    req = urllib.request.Request(url, headers=h)
    with urllib.request.urlopen(req, timeout=timeout) as r:
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
    j0 = _j(_get("%s?f=get_email_address&site=%s" % (GM, domain))) or {}
    sid = j0.get("sid_token") or ""
    if not sid:
        raise RuntimeError("guerrilla: no session")
    _get("%s?f=set_email_user&email_user=%s&sid_token=%s&site=%s"
         % (GM, u, sid, domain))
    return "%s@%s" % (u, domain), sid


def _gm_links(sid):
    j = _j(_get("%s?f=check_email&seq=0&sid_token=%s" % (GM, sid))) or {}
    out = []
    for m in j.get("list") or []:
        body = m.get("mail_body") or ""
        if not body and m.get("mail_id"):
            d = _j(_get("%s?f=fetch_email&email_id=%s&sid_token=%s"
                        % (GM, m["mail_id"], sid))) or {}
            body = d.get("mail_body") or ""
        out += _links((m.get("mail_excerpt") or "") + "\n" + body)
    return out


# ------------------------------------------------------------------- mail.tm

def _tm_domains(base):
    txt = _get(base + "/domains")
    j = _j(txt)
    if j is None:                                   # XML fallback
        return re.findall(r"<domain>([^<]+)</domain>", txt)
    rows = j if isinstance(j, list) else None
    if rows is None:
        for k in ("hydra:member", "items", "domains"):
            if isinstance(j.get(k), list):
                rows = j[k]
                break
    if not rows:
        return []
    out = []
    for d in rows:
        if not isinstance(d, dict) or not d.get("domain"):
            continue
        if str(d.get("isActive", 1)).lower() in ("0", "false"):
            continue
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
    j = _j(_get(base + "/messages", auth))
    msgs = j if isinstance(j, list) else (j or {}).get("hydra:member", [])
    out = []
    for m in msgs or []:
        d = _j(_get(base + "/messages/" + m["id"], auth)) or {}
        html = d.get("html") or []
        body = "\n".join(html) if isinstance(html, list) else str(html)
        out += _links(body + "\n" + (d.get("text") or ""))
    return out


# --------------------------------------------------------------------- gmail

def _gmail_addr(domain):
    u = os.getenv("GMAIL_USER", "").strip()
    local = u.split("@", 1)[0]
    if len(local) < 2:
        return "%s+ptc%s@%s" % (local, _rand(6), domain)
    parts = [local[0]]
    for ch in local[1:]:
        parts.append(("." if random.random() < 0.5 else "") + ch)
    return "".join(parts) + "@" + domain


def _gmail_links(address):
    import email as _email
    import imaplib
    from email.header import decode_header

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
    if tag == "maildrop":
        return ["maildrop.cc"]
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
    for tag in ("maildrop", "guerrilla", "mailtm", "mailgw", "gmail"):
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
    if base in TM:
        return _tm_links(TM[base], token)
    if base == "gmail":
        return _gmail_links(address)
    return []


SKIP = ("facebook", "twitter", "instagram", "youtube", "unsubscribe",
        "privacy", "terms", "help.guerrillamail")


def wait_for_link(address, token, base, timeout=180):
    end = time.time() + timeout
    seen = set()
    fallback = None
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
    for tag in ("maildrop", "guerrilla", "mailtm", "mailgw", "gmail"):
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
    print("--- reachable domains ---")
    total = 0
    for name, doms in domain_probe().items():
        print("%-10s %s" % (name, doms if doms else "(not configured)"))
        total += len(doms)
    print("total candidate domains:", total)
    try:
        base, domain = list_mailboxes()[0]
        addr, tok, base = create_inbox(base, domain)
        print("test inbox: %s  [provider=%s]" % (addr, base))
    except Exception as e:
        print("test inbox FAILED:", e)
