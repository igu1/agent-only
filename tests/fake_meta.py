#!/usr/bin/env python3
"""Fake Meta WhatsApp Cloud API - captures the agent's outbound sends.

Run:    python -u tests/fake_meta.py            (listens on :9001)
Agent:  WHATSAPP_API_BASE=http://host.docker.internal:9001
        (the WhatsApp service builds {base}/{version}/{phone_number_id}/messages)

Endpoints:
  POST /<version>/<phone_number_id>/messages  -> captures text sends, 200 OK
  GET  /outbox?after_id=N                     -> captured messages since N
Pair with tests/wa_simulator.ps1 for an interactive WhatsApp-style chat.
"""
import json
import re
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PORT = 9001

_lock = threading.Lock()
_outbox: list[dict] = []
_next_id = 1


class Handler(BaseHTTPRequestHandler):
    def _send(self, obj: dict, status: int = 200) -> None:
        body = json.dumps(obj).encode()
        self.send_response(status)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *args):
        pass

    def do_POST(self):
        global _next_id
        length = int(self.headers.get('Content-Length') or 0)
        try:
            payload = json.loads(self.rfile.read(length) or b'{}')
        except Exception:
            payload = {}

        if re.fullmatch(r'/v[\d.]+/[^/]+/messages', self.path.split('?')[0]):
            text = ((payload.get('text') or {}).get('body') or '').strip()
            to = payload.get('to') or ''
            if text:  # skip typing indicators / read receipts
                with _lock:
                    msg = {'id': _next_id, 'to': to, 'text': text}
                    _outbox.append(msg)
                    _next_id += 1
                print(f"  -> WhatsApp send to {to}: {text[:120]}")
            self._send({'messaging_product': 'whatsapp',
                        'messages': [{'id': f'wamid.FAKE{_next_id}'}]})
            return
        self._send({'ok': True})

    def do_GET(self):
        if self.path.split('?')[0] == '/outbox':
            after = 0
            m = re.search(r'after_id=(\d+)', self.path)
            if m:
                after = int(m.group(1))
            with _lock:
                msgs = [m for m in _outbox if m['id'] > after]
            self._send({'messages': msgs})
            return
        self._send({'ok': True})


if __name__ == '__main__':
    import sys
    try:
        sys.stdout.reconfigure(line_buffering=True)
    except Exception:
        pass
    print(f"Fake Meta WhatsApp API on http://localhost:{PORT}")
    print("Agent env: WHATSAPP_API_BASE=http://host.docker.internal:9001\n")
    ThreadingHTTPServer(('0.0.0.0', PORT), Handler).serve_forever()
