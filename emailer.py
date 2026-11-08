#!/usr/bin/env python3
"""Throwaway inbox helpers.

Domain pool interleaves mail.tm / mail.gw / tempmail.lol.
Domains without an MX record are dropped.
"""

import os
import time
import socket
import requests
from urllib.parse import quote

_orig_gai = socket.getaddrinfo


def _gai(host, port, family=0, type=0, proto=0, flags=0):
    try:
        return _orig_gai(host, port, socket.AF_INET, type, proto, flags)
    except socket.gaierror:
        return _orig_gai(host, port, family, type, proto, flags)


socket.getaddrinfo = _gai

UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/156.0.0.0 Safari/537.36"
)

TEMPMAIL_CREATE = "https://api.tempmail.lol/v2/inbox/create"

SEED_DOMAINS = [
    "marchslurp.com",
    "arcadehearth.com",
    "lucktaro.com",
    "xzcoot.com",
    "paxeluni.com",
    "kashre.com",
]

DOMAIN_POOL = []
_pool_idx = 0
LAST_INFO = {}


def _get(url, **kw):
    kw.setdefault("timeout", 25)
    kw.setdefault("headers", {"User-Agent": UA, "Accept": "application/json"})
    return requests.get(url, **kw)


def _post(url, **kw):
    kw.setdefault("timeout", 25)
    kw.setdefault("headers", {"User-Agent": UA, "Accept": "application/json"})
    return requests.post(url, **kw)


# ---------------------------------------------------------------- dns

def has_mx(domain):
    try:
        r = _get("https://dns.google/resolve", params={"name": domain, "type": "MX"})
        if not r.ok:
            return None
        return bool(r.json().get("Answer"))
    except Exception:
        return None


# ---------------------------------------------------------------- domain pool

def _base_domain(host):
    parts = host.split(".")
    return ".".join(parts[-2:]) if len(parts) >= 2 else host


def sample_pool(n=3):
    bases = []
    for _ in range(n):
        try:
            r = _post(TEMPMAIL_CREATE, json={})
            if r.ok:
                addr = r.json().get("address", "")
                if "@" in addr:
                    b = _base_domain(addr.split("@", 1)[1])
                    if b not in bases:
                        bases.append(b)
        except Exception:
            pass
        time.sleep(0.5)
    return bases


def sample_mailtm_domains(host, n=6):
    try:
        r = _get(f"https://{host}/domains")
        if not r.ok:
            print(f"[emailer] {host} domains: {r.status_code}", flush=True)
            return []
        j = r.json()
        doms = j.get("hydra:member", j) if isinstance(j, dict) else j
        out = []
        for d in doms[:n]:
            dom = d.get("domain") if isinstance(d, dict) else d
            if dom:
                out.append(dom)
        return out
    except Exception as e:
        print(f"[emailer] {host} domains err: {str(e)[:60]}", flush=True)
        return []


def prime_domains():
    global DOMAIN_POOL, _pool_idx

    tm = []
    for d in ("marchslurp.com", "arcadehearth.com"):
        tm.append({"domain": d, "provider": "tempmail"})
    for d in sample_pool(3):
        if not any(p["domain"] == d for p in tm):
            tm.append({"domain": d, "provider": "tempmail"})
    for d in SEED_DOMAINS:
        if not any(p["domain"] == d for p in tm):
            tm.append({"domain": d, "provider": "tempmail"})

    mt = []
    for host in ("api.mail.tm", "api.mail.gw"):
        for d in sample_mailtm_domains(host, 6):
            mt.append({"domain": d, "provider": "mailtm", "host": host})

    pool = []
    for i in range(max(len(tm), len(mt))):
        if i < len(mt):
            pool.append(mt[i])
        if i < len(tm):
            pool.append(tm[i])

    kept = []
    seen = set()
    for p in pool:
        if p["domain"] in seen:
            continue
        seen.add(p["domain"])
        mx = has_mx(p["domain"])
        print(f"[emailer] mx {p['domain']}: {mx}", flush=True)
        if mx is False:
            continue
        kept.append(p)

    DOMAIN_POOL = kept or pool
    _pool_idx = 0
    print(f"[emailer] domain pool: {[p['domain'] for p in DOMAIN_POOL]}", flush=True)
    return DOMAIN_POOL


def next_domain():
    global _pool_idx
    if not DOMAIN_POOL:
        return None
    d = DOMAIN_POOL[_pool_idx % len(DOMAIN_POOL)]
    _pool_idx += 1
    return d


# ---------------------------------------------------------------- tempmail.lol

def _tempmail_create(domain=None):
    body = {}
    if domain:
        body["domain"] = domain
    r = _post(TEMPMAIL_CREATE, json=body)
    if r.ok:
        j = r.json()
        if j.get("address") and j.get("token"):
            return j["address"], {"kind": "tempmail", "token": j["token"]}
    raise RuntimeError(f"create {r.status_code} {r.text[:90]}")


def _tempmail_v1():
    r = _get("https://api.tempmail.lol/generate")
    if r.ok:
        j = r.json()
        if j.get("address") and j.get("token"):
            return j["address"], {"kind": "tempmail", "token": j["token"]}
    raise RuntimeError(f"v1 {r.status_code}")


def _tempmail_fetch(token):
    global LAST_INFO
    LAST_INFO = {"status": 0, "v1": 0, "expired": None, "n": 0}
    out = []

    try:
        r = _get("https://api.tempmail.lol/v2/inbox?token=" + quote(token or "", safe=""))
        LAST_INFO["status"] = r.status_code
        if r.ok:
            j = r.json()
            LAST_INFO["expired"] = j.get("expired")
            for e in j.get("emails", []) or []:
                out.append(
                    {
                        "subject": e.get("subject", "") or "",
                        "body": e.get("body", "") or "",
                        "html": e.get("html", "") or "",
                    }
                )
    except Exception as e:
        LAST_INFO["err"] = str(e)[:60]

    if not out:
        try:
            r = _get("https://api.tempmail.lol/auth/" + quote(token or "", safe=""))
            LAST_INFO["v1"] = r.status_code
            if r.ok:
                for e in r.json().get("email", []) or []:
                    out.append(
                        {
                            "subject": e.get("subject", "") or "",
                            "body": e.get("body", "") or "",
                            "html": e.get("html", "") or "",
                        }
                    )
        except Exception:
            pass

    LAST_INFO["n"] = len(out)
    return out


# ---------------------------------------------------------------- mail.tm / gw

def _mailtm_create(host, domain=None):
    if not domain:
        doms = _get(f"https://{host}/domains").json()
        doms = doms.get("hydra:member", doms) if isinstance(doms, dict) else doms
        domain = doms[0]["domain"]

    for _ in range(3):
        stamp = int(time.time() * 1000) % 100000000
        addr = f"ptc{stamp}@{domain}"
        pw = f"Ptc{stamp}"
        r = _post(f"https://{host}/accounts", json={"address": addr, "password": pw})
        if r.status_code in (200, 201):
            t = _post(f"https://{host}/token", json={"address": addr, "password": pw})
            if t.ok:
                return addr, {"kind": "mailtm", "host": host, "token": t.json()["token"]}
        time.sleep(1)
    raise RuntimeError(f"{host} create failed for {domain}")


def _mailtm_fetch(host, token):
    global LAST_INFO
    try:
        r = _get(
            f"https://{host}/messages",
            headers={"Authorization": "Bearer " + token, "User-Agent": UA},
        )
        LAST_INFO = {"status": r.status_code, "n": 0}
        if not r.ok:
            return []
        msgs = r.json()
        msgs = msgs.get("hydra:member", msgs) if isinstance(msgs, dict) else msgs
        LAST_INFO["n"] = len(msgs)
        out = []
        for m in msgs:
            full = _get(
                f"https://{host}/messages/{m['id']}",
                headers={"Authorization": "Bearer " + token, "User-Agent": UA},
            ).json()
            out.append(
                {
                    "subject": full.get("subject", "") or "",
                    "body": full.get("text", "") or "",
                    "html": " ".join(full.get("html", []) or []),
                }
            )
        return out
    except Exception:
        return []


# ---------------------------------------------------------------- public API

def create_inbox(entry=None):
    dom = None
    prov = "tempmail"
    host = "api.mail.tm"
    if isinstance(entry, dict):
        dom = entry.get("domain")
        prov = entry.get("provider", "tempmail")
        host = entry.get("host") or "api.mail.tm"
    elif isinstance(entry, str):
        dom = entry

    errors = []

    if dom:
        if prov == "mailtm":
            try:
                return _mailtm_create(host, dom)
            except Exception as e:
                errors.append(f"mailtm[{dom}]: {e}")
        else:
            for _ in range(2):
                try:
                    return _tempmail_create(dom)
                except Exception as e:
                    errors.append(f"tempmail[{dom}]: {e}")
                    time.sleep(2)

    for _ in range(4):
        try:
            return _tempmail_create(None)
        except Exception as e:
            errors.append(f"tempmail: {e}")
            time.sleep(3)

    try:
        return _tempmail_v1()
    except Exception as e:
        errors.append(f"v1: {e}")

    for h in ("api.mail.tm", "api.mail.gw"):
        try:
            return _mailtm_create(h)
        except Exception as e:
            errors.append(f"{h}: {e}")

    raise RuntimeError("no inbox available | " + " ; ".join(errors[-6:]))


def fetch_messages(address, token=None, base=None):
    if isinstance(token, dict):
        kind = token.get("kind")
        tok = token.get("token")
        host = token.get("host")
    else:
        kind = "tempmail"
        tok = token
        host = None

    if kind == "mailtm":
        return _mailtm_fetch(host or "api.mail.tm", tok)
    return _tempmail_fetch(tok)
