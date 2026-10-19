"""
emailer.py -- free disposable-inbox layer for the PTC bot.

Providers (auto-rotating, first working one wins):
    mailtm  -> api.mail.tm
    mailgw  -> api.mail.gw        (same engine, DIFFERENT domains)
    sec     -> 1secmail           (many domains)
    gmail   -> dot-trick on your own Gmail (optional, needs secrets)

Standalone probe / smoke test:
    python emailer.py

Force a provider or blacklist a bad domain via env:
    EMAIL_PROVIDER=mailgw
    DOMAIN_BLOCK=maxxspace.com,1secmail.com
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

MAILTM = "https://api.mail.tm"
MAILGW = "https://api.mail.gw"
SECBASES = ["https://www.1secmail.com/api/v1/",
            "https://api.1secmail.com/api/v1/"]


def _req(url, method="GET", body=None, headers=None, timeout=30):
    h = {"Accept": "application/json", "User-Agent": UA}
    if headers:
        h.update(headers)
    data = None
    if body is not None:
        data = json.dumps(body).encode()
        h["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=h, method=method)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        raw = r.read().decode("utf-8", "replace")
    try:
        return json.loads(raw)
    except Exception:
        return {"_raw": raw}


def _rand(n=10):
    return "".join(random.choice(string.ascii_lowercase + string.digits)
                   for _ in range(n))


def _norm(subject, text, html):
    if isinstance(text, list):
        text = "\n".join(str(x) for x in text)
    if isinstance(html, list):
        html = "\n".join(str(x) for x in html)
    return {"subject": subject or "", "text": text or "", "html": html or ""}


def _blocked():
    return {d.strip().lower() for d in os.getenv("DOMAIN_BLOCK", "").split(",")
            if d.strip()}


# ---------------------------------------------------------------- providers

class _Tm:
    """mail.tm / mail.gw -- identical REST API, separate domain pools."""

    def __init__(self, name, base):
        self.name = name
        self.base = base

    def domains(self):
        j = _req(self.base + "/domains")
        return [d["domain"] for d in j.get("hydra:member", [])
                if d.get("isActive", True)]

    def make(self, domain):
        return "ptc%s@%s" % (_rand(8), domain)

    def create(self, address, password):
        _req(self.base + "/accounts", "POST",
             {"address": address, "password": password})
        tok = _req(self.base + "/token", "POST",
                   {"address": address, "password": password}).get("token")
        if not tok:
            raise RuntimeError("no token returned")
        return {"provider": self.name, "address": address,
                "password": password, "token": tok, "base": self.base}

    def messages(self, ib):
        auth = {"Authorization": "Bearer " + ib["token"]}
        lst = _req(self.base + "/messages", headers=auth).get("hydra:member", [])
        out = []
        for m in lst:
            d = _req(self.base + "/messages/" + m["id"], headers=auth)
            out.append(_norm(d.get("subject", ""), d.get("text", ""),
                             d.get("html", "")))
        return out


class _Sec:
    """1secmail -- no account step, many domains, shared read access."""

    name = "sec"

    def _base(self):
        for b in SECBASES:
            try:
                _req(b + "?action=getDomainList")
                return b
            except Exception:
                continue
        raise RuntimeError("1secmail unreachable")

    def domains(self):
        j = _req(self._base() + "?action=getDomainList")
        return j if isinstance(j, list) else []

    def make(self, domain):
        return "%s@%s" % (_rand(10), domain)

    def create(self, address, password=None):
        login, _, domain = address.partition("@")
        return {"provider": self.name, "address": address,
                "login": login, "domain": domain}

    def messages(self, ib):
        b = self._base()
        lst = _req("%s?action=getMessages&login=%s&domain=%s"
                   % (b, ib["login"], ib["domain"]))
        if not isinstance(lst, list):
            return []
        out = []
        for m in lst:
            try:
                d = _req("%s?action=readMessage&login=%s&domain=%s&id=%s"
                         % (b, ib["login"], ib["domain"], m["id"]))
            except Exception:
                continue
            out.append(_norm(d.get("subject", ""),
                             d.get("textBody", "") or d.get("body", ""),
                             d.get("htmlBody", "")))
        return out


class _Gmail:
    """Your own Gmail + the dot trick. Gmail ignores dots, Pokemon does not."""

    name = "gmail"

    def _cred(self):
        u = os.getenv("GMAIL_USER", "").strip()
        p = os.getenv("GMAIL_APP_PASSWORD", "").strip()
        return (u, p) if u and p and "@" in u else (None, None)

    def domains(self):
        u, p = self._cred()
        return [u.split("@", 1)[1]] if u else []

    def make(self, domain):
        u, _ = self._cred()
        local = u.split("@", 1)[0]
        out = [local[0]]
        for ch in local[1:]:
            out.append(("." if random.random() < 0.5 else "") + ch)
        return "".join(out) + "@" + domain

    def create(self, address, password=None):
        u, p = self._cred()
        return {"provider": self.name, "address": address, "user": u, "pw": p}

    def messages(self, ib):
        import email as _email
        import imaplib
        from email.header import decode_header

        out = []
        M = imaplib.IMAP4_SSL("imap.gmail.com")
        try:
            M.login(ib["user"], ib["pw"])
            M.select("INBOX")
            _, data = M.search(None, "UNSEEN")
            for num in data[0].split():
                _, raw = M.fetch(num, "(RFC822)")
                msg = _email.message_from_bytes(raw[0][1])
                subj = ""
                for chunk, enc in decode_header(msg.get("Subject", "")):
                    subj += chunk.decode(enc or "utf-8", "replace") \
                        if isinstance(chunk, bytes) else chunk
                text, html = "", ""
                for part in msg.walk():
                    ct = part.get_content_type()
                    if ct == "text/plain" and not text:
                        text = _payload(part)
                    elif ct == "text/html" and not html:
                        html = _payload(part)
                out.append(_norm(subj, text, html))
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


_PROVIDERS = [_Tm("mailtm", MAILTM), _Tm("mailgw", MAILGW), _Sec(), _Gmail()]
_ALIAS = {"mail.tm": "mailtm", "mail.gw": "mailgw", "1secmail": "sec",
          "gmail.com": "gmail"}


# ------------------------------------------------------------------- public

def domain_probe():
    out = {}
    for p in _PROVIDERS:
        try:
            out[p.name] = p.domains()
        except Exception as e:
            out[p.name] = ["ERROR: %s" % e]
    return out


list_domains = domain_probe
list_mailboxes = domain_probe


def create_inbox(provider=None, domain=None, prefix="ptc"):
    want = (provider or os.getenv("EMAIL_PROVIDER", "")).strip().lower()
    want = _ALIAS.get(want, want)
    bad = _blocked()
    errors = []

    for p in _PROVIDERS:
        if want and p.name != want:
            continue
        try:
            doms = p.domains()
        except Exception as e:
            errors.append("%s: %s" % (p.name, e))
            continue
        doms = [d for d in doms if d.lower() not in bad]
        if domain:
            doms = [d for d in doms if d == domain]
        if not doms:
            errors.append("%s: no usable domain" % p.name)
            continue
        random.shuffle(doms)
        for d in doms:
            addr = p.make(d)
            try:
                ib = p.create(addr, _rand(12) + "Aa1!")
                print("[mail] %s -> %s" % (p.name, addr))
                return ib
            except Exception as e:
                errors.append("%s/%s: %s" % (p.name, d, e))
    raise RuntimeError("no inbox available: " + " | ".join(errors))


def fetch_messages(inbox):
    for p in _PROVIDERS:
        if p.name == inbox.get("provider"):
            try:
                return p.messages(inbox)
            except Exception as e:
                print("[mail] fetch error: %s" % e)
                return []
    return []


def _links(blob):
    blob = (blob or "").replace("&amp;", "&").replace("&#38;", "&")
    out = []
    for u in re.findall(r'https?://[^\s"\'<>)\]]+', blob):
        u = u.rstrip(".,;:)")
        if u not in out:
            out.append(u)
    return out


def wait_for_link(inbox, timeout=300, interval=8, want=("pokemon", "verify",
                                                        "activ", "confirm")):
    """Return the activation URL (or a 6-digit code if that's what arrives)."""
    deadline = time.time() + timeout
    seen = set()
    while time.time() < deadline:
        for m in fetch_messages(inbox):
            key = (m["subject"], m["text"][:60], m["html"][:60])
            if key in seen:
                continue
            seen.add(key)
            blob = m["html"] + "\n" + m["text"]
            links = _links(blob)
            good = [l for l in links if any(w in l.lower() for w in want)]
            if good or links:
                print("[mail] subject: %s" % m["subject"][:70])
                return (good or links)[0]
            code = re.search(r"\b(\d{6})\b", blob)
            if code:
                print("[mail] code: %s" % code.group(1))
                return code.group(1)
        time.sleep(interval)
    print("[mail] timed out waiting for activation")
    return None


wait_for_code = wait_for_link
wait_gmail = wait_for_link
wait_code = wait_for_link


if __name__ == "__main__":
    print("--- live domains per provider ---")
    for name, doms in domain_probe().items():
        print("%s: %d" % (name, len(doms)))
        for d in doms:
            print("    ", d)
    print("--- creating a test inbox ---")
    box = create_inbox()
    print("address:", box["address"])
