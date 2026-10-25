#!/usr/bin/env python3
"""Throwaway inbox helpers.

Primary: tempmail.lol (Pokemon accepts its random-subdomain domains).
Fallbacks: mail.tm / mail.gw.

create_inbox() -> (address, token_blob)
fetch_messages(address, token_blob) -> [{"subject","body","html"}]
"""

import os
import time
import socket
import requests
from urllib.parse import quote

# ---------------------------------------------------------------- IPv4 force
# api.mail.gw publishes an AAAA record and GitHub runners have no IPv6 route.
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

MAILTM_DOMAINS = [
    "maxxspace.com",
    "punkproof.com",
    "nontoxmail.com",
    "leacore.com",
    "hitzcart.com",
    "snakemail.com",
]

GUERRILLA_DOMAINS = [
    "sharklasers.com",
    "guerrillamail.info",
    "guerrillamail.biz",
    "guerrillamail.com",
    "guerrillamail.de",
    "guerrillamail.net",
    "guerrillamail.org",
    "grr.la",
    "spam4.me",
    "pokemail.net",
]


def _get(url, **kw):
    kw.setdefault("timeout", 25)
    kw.setdefault("headers", {"User-Agent": UA, "Accept": "application/json"})
    return requests.get(url, **kw)


def _post(url, **kw):
    kw.setdefault("timeout", 25)
    kw.setdefault("headers", {"User-Agent": UA, "Accept": "application/json"})
    return requests.post(url, **kw)


# ---------------------------------------------------------------- tempmail.lol

def _tempmail_create():
    """v2 then v1. Returns (address, {"kind":"tempmail","token":tok})."""
    try:
        r = _post("https://api.tempmail.lol/v2/inbox/create")
        if r.ok:
            j = r.json()
            if j.get("address") and j.get("token"):
                return j["address"], {"kind": "tempmail", "token": j["token"]}
    except Exception:
        pass

    r = _get("https://api.tempmail.lol/generate")
    if r.ok:
        j = r.json()
        if j.get("address") and j.get("token"):
            return j["address"], {"kind": "tempmail", "token": j["token"]}
    raise RuntimeError(f"tempmail create failed ({r.status_code})")


def _tempmail_fetch(token):
    # v2
    try:
        r = _get("https://api.tempmail.lol/v2/inbox?token=" + quote(token or "", safe=""))
        if r.ok:
            j = r.json()
            out = []
            for e in j.get("emails", []) or []:
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

    # v1
    try:
        r = _get("https://api.tempmail.lol/auth/" + quote(token or "", safe=""))
        if r.ok:
            j = r.json()
            out = []
            for e in j.get("email", []) or []:
                out.append(
                    {
                        "subject": e.get("subject", "") or "",
                        "body": e.get("body", "") or "",
                        "html": e.get("html", "") or "",
                    }
                )
            return out
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

def create_inbox():
    """Returns (address, token_blob)."""
    errors = []

    # tempmail.lol first - Pokemon accepts its domains
    for attempt in range(8):
        try:
            return _tempmail_create()
        except Exception as e:
            errors.append(f"tempmail: {e}")
            time.sleep(3 + attempt * 2)

    # mail.tm
    for attempt in range(3):
        try:
            return _mailtm_create("api.mail.tm")
        except Exception as e:
            errors.append(f"mailtm: {e}")
            time.sleep(2)

    # mail.gw
    for attempt in range(2):
        try:
            return _mailtm_create("api.mail.gw")
        except Exception as e:
            errors.append(f"mailgw: {e}")
            time.sleep(2)

    raise RuntimeError("no inbox available | " + " ; ".join(errors[-8:]))


def fetch_messages(address, token=None, base=None):
    """token may be the blob from create_inbox() or a raw string."""
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
