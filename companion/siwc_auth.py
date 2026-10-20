"""First-party Sign in with ChatGPT OAuth for an eligible local application.

No Codex cookie, existing CLI credential, ChatGPT backend API, or private
webpage extraction is used. Browser approval and plan-use consent are required.
"""
from __future__ import annotations

import argparse
import base64
from contextlib import contextmanager
import hashlib
import hmac
import json
import os
import secrets
import threading
import time
import uuid
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import urlencode, urlsplit, parse_qs
from urllib.request import Request, urlopen

from . import core

ISSUER = "https://auth.openai.com"
RESOURCE = "https://api.openai.com/v1"
REQUIRED_SCOPE = "chatgpt.tokens.use.direct"
SCOPES = "openid profile email offline_access resource.invoke chatgpt.tokens.use.direct"
AUTHORIZATION_URL = ISSUER + "/api/accounts/authorize"
TOKEN_URL = ISSUER + "/api/accounts/oauth/token"
JWKS_URL = ISSUER + "/.well-known/jwks.json"
LOCK = threading.RLock()


def _directory(root: Path = core.ROOT) -> Path:
    # On Windows, keep renewable OAuth credentials inside the user's protected
    # profile instead of a shareable project directory.
    if root.resolve() == core.ROOT.resolve() and os.getenv("LOCALAPPDATA"):
        return Path(os.environ["LOCALAPPDATA"]) / "QQ-AI-Companion" / "siwc"
    return root / "data" / "siwc"


def credentials_path(root: Path = core.ROOT) -> Path:
    return _directory(root) / "credentials.json"


def _read_json(path: Path) -> dict:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("Invalid local SIWC JSON")
    return value


def _save_private(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + "." + secrets.token_hex(5) + ".tmp")
    try:
        descriptor = os.open(tmp, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(value, handle, ensure_ascii=False)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
        os.chmod(path, 0o600)
    finally:
        tmp.unlink(missing_ok=True)


@contextmanager
def _refresh_file_lock(root: Path):
    """Serialize rotating refresh tokens across competing worker processes."""
    path = _directory(root) / "refresh.lock"
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        if os.name == "nt":
            import msvcrt
            if os.fstat(descriptor).st_size == 0:
                os.write(descriptor, b"0")
            os.lseek(descriptor, 0, os.SEEK_SET)
            msvcrt.locking(descriptor, msvcrt.LK_LOCK, 1)
        else:
            import fcntl
            fcntl.flock(descriptor, fcntl.LOCK_EX)
        yield
    finally:
        if os.name == "nt":
            import msvcrt
            os.lseek(descriptor, 0, os.SEEK_SET)
            msvcrt.locking(descriptor, msvcrt.LK_UNLCK, 1)
        else:
            import fcntl
            fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


def _host_id(root: Path) -> str:
    filename = _directory(root) / "host.json"
    if filename.exists():
        value = _read_json(filename).get("host_id", "")
        if not isinstance(value, str) or not value.startswith("urn:uuid:"):
            raise ValueError("Invalid existing host ID")
        return value
    identifier = "urn:uuid:" + str(uuid.uuid4())
    _save_private(filename, {"host_id": identifier})
    return identifier


def _decode_segment(text: str) -> dict:
    data = base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))
    item = json.loads(data)
    if not isinstance(item, dict):
        raise ValueError("JWT header or payload must be an object")
    return item


def verify_id_token(encoded: str, client_id: str, nonce: str,
                    jwks: dict, expected_subject: str | None = None) -> dict:
    """Validate RS256 OIDC signature, issuer, audience, nonce and lifetime."""
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import padding, rsa
    parts = encoded.split(".")
    if len(parts) != 3:
        raise ValueError("Malformed OIDC ID token")
    header, payload = _decode_segment(parts[0]), _decode_segment(parts[1])
    if header.get("alg") != "RS256" or not isinstance(header.get("kid"), str):
        raise ValueError("Unsupported OIDC signing algorithm")
    keys = [key for key in jwks.get("keys", []) if key.get("kid") == header["kid"]
            and key.get("kty") == "RSA" and key.get("use", "sig") == "sig"]
    if len(keys) != 1:
        raise ValueError("OIDC signing key unavailable")
    key = keys[0]
    n = int.from_bytes(base64.urlsafe_b64decode(key["n"] + "=" * (-len(key["n"]) % 4)), "big")
    e = int.from_bytes(base64.urlsafe_b64decode(key["e"] + "=" * (-len(key["e"]) % 4)), "big")
    public_key = rsa.RSAPublicNumbers(e, n).public_key()
    signature = base64.urlsafe_b64decode(parts[2] + "=" * (-len(parts[2]) % 4))
    public_key.verify(signature, (parts[0] + "." + parts[1]).encode("ascii"),
                      padding.PKCS1v15(), hashes.SHA256())
    audience = payload.get("aud")
    audience_ok = client_id in audience if isinstance(audience, list) else audience == client_id
    current = time.time()
    if not (payload.get("iss") == ISSUER and audience_ok and payload.get("nonce") == nonce
            and isinstance(payload.get("sub"), str) and payload["sub"]
            and isinstance(payload.get("exp"), (int, float)) and payload["exp"] > current - 30
            and isinstance(payload.get("iat"), (int, float)) and payload["iat"] < current + 60
            and (not payload.get("nbf") or payload["nbf"] <= current + 60)):
        raise ValueError("OIDC identity claims did not validate")
    if expected_subject and payload["sub"] != expected_subject:
        raise ValueError("Different ChatGPT account selected")
    return payload


def _json_url(url: str, data: bytes | None = None,
              headers: dict | None = None) -> dict:
    with urlopen(Request(url, data=data, headers=headers or {}), timeout=12) as response:
        result = json.load(response)
    if not isinstance(result, dict):
        raise ValueError("Unexpected OAuth server response")
    return result


def _token_exchange(form: dict) -> dict:
    return _json_url(TOKEN_URL, urlencode(form).encode("utf-8"),
                     {"Content-Type": "application/x-www-form-urlencoded"})


def _normalize_grant(response: dict, client_id: str, host_id: str,
                     identity: dict, old_record: dict | None = None) -> dict:
    scopes = response.get("scope", "")
    granted = set(scopes.split()) if isinstance(scopes, str) else set()
    if not {REQUIRED_SCOPE, "resource.invoke", "openid"}.issubset(granted):
        raise PermissionError("ChatGPT plan-use consent not granted")
    access = response.get("access_token")
    refresh = response.get("refresh_token")
    if not all(isinstance(x, str) and x for x in (access, refresh)):
        raise ValueError("Missing OAuth access or refresh token")
    expires = response.get("expires_in")
    if not isinstance(expires, (int, float)) or expires <= 0:
        raise ValueError("Missing token lifetime")
    return {
        "client_id": client_id, "ext_agent_host_id": host_id,
        "subject": identity["sub"], "issuer": ISSUER,
        "email": identity.get("email"), "id_token": response.get("id_token") or
        (old_record or {}).get("id_token"),
        "access_token": access, "refresh_token": refresh,
        "scopes": sorted(granted), "expires_at": int(time.time() + expires),
    }


def status(root: Path = core.ROOT) -> dict:
    path = credentials_path(root)
    if not path.exists():
        return {"signed_in": False, "plan_permission": False}
    item = _read_json(path)
    return {
        "signed_in": bool(item.get("subject") and item.get("client_id")),
        "plan_permission": REQUIRED_SCOPE in item.get("scopes", []),
        "token_current": item.get("expires_at", 0) > time.time() + 60,
    }


def current_access_token(root: Path = core.ROOT) -> str:
    """Read the app's own approved credentials; never access Codex credentials."""
    with LOCK, _refresh_file_lock(root):
        record = _read_json(credentials_path(root))
        if record.get("issuer") != ISSUER or not record.get("client_id", "").startswith("oaiapp_"):
            raise ValueError("Not a registered ChatGPT OAuth app")
        if REQUIRED_SCOPE not in record.get("scopes", []):
            raise PermissionError("ChatGPT plan-use consent not granted")
        if record.get("expires_at", 0) <= time.time() + 90:
            record = refresh(root, record)
        return record["access_token"]


def refresh(root: Path, old: dict) -> dict:
    """Refresh under the app's single-process lock; rotating token is atomic."""
    response = _token_exchange({
        "grant_type": "refresh_token", "client_id": old["client_id"],
        "refresh_token": old["refresh_token"], "resource": RESOURCE,
    })
    scopes = response.get("scope", " ".join(old["scopes"]))
    response["scope"] = scopes
    updated = _normalize_grant(response, old["client_id"],
                               old["ext_agent_host_id"],
                               {"sub": old["subject"], "email": old.get("email")},
                               old)
    _save_private(credentials_path(root), updated)
    return updated


def login(root: Path = core.ROOT, wait_seconds: int = 180) -> str:
    """Run a one-time browser consent flow on the user's own local machine."""
    previous = _read_json(credentials_path(root)) if credentials_path(root).exists() else None
    host_id = _host_id(root)
    client = previous["client_id"] if previous else "dynamic_agent_client"
    state = secrets.token_urlsafe(32)
    nonce = secrets.token_urlsafe(32)
    verifier = secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    received: dict = {}
    completed = threading.Event()

    class Callback(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass  # callback URLs include one-time authorization codes

        def do_GET(self):
            parsed = urlsplit(self.path)
            args = parse_qs(parsed.query)
            valid = parsed.path == "/auth/callback" and hmac.compare_digest(
                args.get("state", [""])[0], state)
            if not valid:
                self.send_error(400)
                return
            received.update({k: v[0] for k, v in args.items() if v})
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.end_headers()
            self.wfile.write("Authorization returned. You may close this tab.".encode())
            completed.set()

    with HTTPServer(("127.0.0.1", 0), Callback) as listener:
        redirect = f"http://127.0.0.1:{listener.server_port}/auth/callback"
        values = {
            "client_id": client, "redirect_uri": redirect, "response_type": "code",
            "scope": SCOPES, "resource": RESOURCE, "state": state,
            "nonce": nonce, "code_challenge_method": "S256",
            "code_challenge": challenge, "ext_agent_host_id": host_id,
        }
        if not previous:
            values["agent_name_hint"] = "QQ AI Companion"
        target = AUTHORIZATION_URL + "?" + urlencode(values)
        print("Opening the official ChatGPT consent page on this computer.")
        if not webbrowser.open(target):
            print("Could not open the browser. Start login on the actual desktop.")
            raise RuntimeError("Browser unavailable")
        deadline = time.monotonic() + max(15, min(wait_seconds, 600))
        listener.timeout = 0.5
        while time.monotonic() < deadline and not completed.is_set():
            listener.handle_request()
    if not completed.is_set():
        raise TimeoutError("Browser authorization did not finish")
    if received.get("error"):
        raise PermissionError("User declined the authorization")
    code = received.get("code")
    issued_client = received.get("client_id", client)
    if not isinstance(code, str) or not code:
        raise ValueError("Missing OAuth authorization code")
    if previous and issued_client != previous["client_id"]:
        raise ValueError("OAuth callback changed the saved client ID")
    if not previous and not issued_client.startswith("oaiapp_"):
        raise ValueError("Registration did not issue a client ID")
    response = _token_exchange({
        "grant_type": "authorization_code", "client_id": issued_client,
        "code": code, "code_verifier": verifier, "redirect_uri": redirect,
        "resource": RESOURCE,
    })
    jwks = _json_url(JWKS_URL)
    claims = verify_id_token(response.get("id_token", ""), issued_client, nonce,
                             jwks, previous.get("subject") if previous else None)
    account = _normalize_grant(response, issued_client, host_id, claims, previous)
    _save_private(credentials_path(root), account)
    return "authorized"


def main() -> None:
    parser = argparse.ArgumentParser(description="Official local ChatGPT plan sign-in")
    parser.add_argument("action", choices=("status", "login"))
    args = parser.parse_args()
    if args.action == "status":
        print(json.dumps(status(), ensure_ascii=False))
    else:
        print(login())


if __name__ == "__main__":
    main()
