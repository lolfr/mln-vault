#!/usr/bin/env python3
"""Lanceur réseau du site catalogue (exposition via Traefik/SSO de pi5b).

app.py n'a aucune authentification : le portier est le middleware `sso` de
Traefik (archive.mylastnight.eu). Écouter sur l'IP LAN rendrait le site
joignable SANS SSO par tout le LAN : on refuse donc toute IP source hors
VAULT_ALLOWED_PEERS (pi5b = Traefik + subnet router tailnet, et le mini).
"""
from __future__ import annotations

import os

from werkzeug.serving import run_simple

from app import app

HOST = os.environ.get("VAULT_HOST", "127.0.0.1")
PORT = int(os.environ.get("VAULT_PORT", "5055"))
ALLOWED = {p.strip() for p in os.environ.get(
    "VAULT_ALLOWED_PEERS", "127.0.0.1,192.168.0.210").split(",") if p.strip()}


def gate(wsgi):
    def wrapped(environ, start_response):
        if environ.get("REMOTE_ADDR") not in ALLOWED:
            start_response("403 Forbidden", [("Content-Type", "text/plain")])
            return [b"forbidden\n"]
        return wsgi(environ, start_response)
    return wrapped


if __name__ == "__main__":
    run_simple(HOST, PORT, gate(app.wsgi_app), threaded=True,
               use_reloader=False, use_debugger=False)
