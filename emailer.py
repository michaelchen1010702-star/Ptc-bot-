#!/usr/bin/env python3
"""Throwaway inbox helpers.

Primary: tempmail.lol (Pokemon accepts some of its domains).
Fallbacks: mail.tm / mail.gw.

create_inbox(domain=None) -> (address, token_blob)
fetch_messages(address, token_blob) -> [{"subject","body","html"}]
"""

import os
import time
import socket
import requests
from urllib.parse import quote

# ---------------------------------------------------------------- IPv4 force
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

# fallbacks if the pool sample fails
SEED_DOMAINS = [
    "lucktaro.com",
    "xzcoot.com",
    "paxeluni.com",
    "kashre.com",
]

MAILTM_HOSTS = ["api.mail.tm", "api.mail.gw"]

DOMAIN_POOL = []
_pool_idx = 0


def _get(url, **kw):
    kw.setdefault("timeout", 25)
    kw.setdefault("headers", {"User-Agent": UA, "Accept": "application/json"})
    return requests.get(url, **kw)


def _post(url, **kw):
    kw.setdefault("timeout", 25)
    kw.setdefault("headers", {"User-Agent": UA, "Accept": "application/json"})
    return requests.post(url, **kw)


# ---------------------------------------------------------------- domain pool

def _base_domain(host):
    parts = host.split(".")
    return ".".join(parts[-2:]) if len(parts) >= 2 else host


def sample_pool(n=6):
    """Create a few random inboxes and learn which base domains are live."""
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
        time.sleep(0.6)
    return bases


def prime_domains():
    """Populate the rotation pool from the live pool, seeded as a fallback."""
    global DOMAIN_POOL, _pool_idx
    live = sample_pool(6)
    pool = []
    for d in live + SEED_DOMAINS:
        if d and d not in pool:
            pool.append(d)
    DOMAIN_POOL = pool
    _pool_idx = 0
    print(f"[emailer] domain pool: {DOMAIN_POOL}", flush=True)
    return DOMAIN_POOL


def next_domain():
    """Cycle through the pool, then fall back to a random domain."""
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
    try:
        r = _get("https://api.tempmail.lol/v2/inbox?token=" + quote(token or "", safe=""))
        if r.ok:
            out = []
            for e in r.json().get("emails", []) or []:
                out.append(
                    {
                        "subject": e.get("subject", "") or "",
                        "body": e.get("body", "") or "",
                        "html": e.get("html", "") or "",
                    }
                )
            if out:
                return out
    except Exception:
        pass

    try:
        r = _get("https://api.tempmail.lol/auth/" + quote(token or "", safe=""))
        if r.ok:
            return [
                {
                    "subject": e.get("subject", "") or "",
                    "body": e.get("body", "") or "",
                    "html": e.get("html", "") or "",
                }
                for e in r.json().get("email", []) or []
            ]
    except Exception:
        pass
    return []


# ---------------------------------------------------------------- mail.tm / gw

def _mailtm_create(host):
    doms = _get(f"https://{host}/domains").json()
    doms = doms.get("hydra:member", doms) if isinstance(doms, dict) else doms
    domain = doms[0]["domain"]

    for _ in range(4):
        user = "ptcmail" + str(int(time.time() * 1000) % 100000000)
        addr = f"{user}@{domain}"
        pw = "Ptc" + str(int(time.time() * 1000) % 1000000)
        r = _post(f"https://{host}/accounts", json={"address": addr, "password": pw})
        if r.status_code in (200, 201):
            t = _post(f"https://{host}/token", json={"address": addr, "password": pw})
            if t.ok:
                return addr, {"kind": "mailtm", "host": host, "token": t.json()["token"]}
        time.sleep(1)
    raise RuntimeError(f"{host} create failed")


def _mailtm_fetch(host, token):
    try:
        r = _get(
            f"https://{host}/messages",
            headers={"Authorization": "Bearer " + token, "User-Agent": UA},
        )
        if not r.ok:
            return []
        msgs = r.json()
        msgs = msgs.get("hydra:member", msgs) if isinstance(msgs, dict) else msgs
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

def create_inbox(domain=None):
    """Returns (address, token_blob). Tries the given domain, then random,
    then the v1 endpoint, then mail.tm / mail.gw."""
    errors = []

    for attempt in range(3):
        use = domain if attempt == 0 else None
        try:
            return _tempmail_create(use)
        except Exception as e:
            errors.append(f"tempmail[{use}]: {e}")
            time.sleep(2)

    for attempt in range(6):
        try:
            return _tempmail_create(None)
        except Exception as e:
            errors.append(f"tempmail: {e}")
            time.sleep(3 + attempt * 2)

    try:
        return _tempmail_v1()
    except Exception as e:
        errors.append(f"v1: {e}")

    for host in MAILTM_HOSTS:
        try:
            return _mailtm_create(host)
        except Exception as e:
            errors.append(f"{host}: {e}")

    raise RuntimeError("no inbox available | " + " ; ".join(errors[-8:]))


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
