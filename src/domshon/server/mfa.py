"""Multi-factor authentication for the admin login (stdlib + httpx only).

Three second factors, any combination:
  * authenticator app  - standard TOTP (RFC 6238): Google Authenticator, Microsoft Authenticator,
                         Authy, 1Password, Aegis, ...
  * text message (SMS) - a 6-digit code sent to a phone number, through Twilio or any HTTP gateway.
  * e-mail             - a 6-digit code sent through any SMTP server (smtp_* settings).

Plus one-time recovery codes. State lives in <config dir>/mfa.json (mode 0600). Once MFA is on, every
username + password login (web, desktop, terminal, Python package) must also give a code.
Device API tokens (dst_...) are unaffected: they are already second-factor-proof credentials.

Lost your phone and your recovery codes?  On the server:  domshon server mfa disable
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import re
import secrets
import smtplib
import ssl
import struct
import threading
import time
from email.message import EmailMessage
from pathlib import Path
from urllib.parse import quote

STEP = 30
DIGITS = 6
SMS_TTL = 300          # seconds an SMS code stays valid
SMS_MIN_GAP = 30       # seconds between two SMS sends
MAX_TRIES = 5          # wrong guesses allowed per texted / e-mailed code
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
PHONE_RE = re.compile(r"^\+[1-9][0-9]{6,14}$")  # E.164, e.g. +15551234567


# ---- TOTP ------------------------------------------------------------------
def new_secret() -> str:
    return base64.b32encode(secrets.token_bytes(20)).decode().rstrip("=")


def _hotp(secret: str, counter: int) -> str:
    key = base64.b32decode(secret + "=" * (-len(secret) % 8), casefold=True)
    mac = hmac.new(key, struct.pack(">Q", counter), hashlib.sha1).digest()
    off = mac[-1] & 0x0F
    num = (struct.unpack(">I", mac[off:off + 4])[0] & 0x7FFFFFFF) % (10 ** DIGITS)
    return str(num).zfill(DIGITS)


def totp_now(secret: str, at: float | None = None) -> str:
    return _hotp(secret, int((at if at is not None else time.time()) // STEP))


def totp_match(secret: str, code: str, window: int = 1, at: float | None = None) -> int | None:
    """Return the matching time-step counter (so it can be marked used) or None. +-1 step of clock drift."""
    code = re.sub(r"\s+", "", code or "")
    if not re.fullmatch(r"\d{6}", code):
        return None
    now = int((at if at is not None else time.time()) // STEP)
    for c in range(now - window, now + window + 1):
        if hmac.compare_digest(_hotp(secret, c), code):
            return c
    return None


def otpauth_uri(secret: str, account: str, issuer: str = "Domshon") -> str:
    label = quote(f"{issuer}:{account}")
    return f"otpauth://totp/{label}?secret={secret}&issuer={quote(issuer)}&algorithm=SHA1&digits={DIGITS}&period={STEP}"


def group(secret: str) -> str:
    return " ".join(secret[i:i + 4] for i in range(0, len(secret), 4))


# ---- recovery codes --------------------------------------------------------
def _norm_recovery(code: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (code or "").lower())


def _rhash(code: str) -> str:
    return hashlib.sha256(_norm_recovery(code).encode()).hexdigest()


def make_recovery(n: int = 8) -> list[str]:
    alphabet = "abcdefghjkmnpqrstuvwxyz23456789"  # no look-alikes
    return ["".join(secrets.choice(alphabet) for _ in range(4)) + "-" +
            "".join(secrets.choice(alphabet) for _ in range(4)) for _ in range(n)]


# ---- SMS delivery ------------------------------------------------------------
class SmsError(Exception):
    pass


def sms_configured(settings) -> bool:
    return bool((settings.get("sms_twilio_sid") and settings.get("sms_twilio_token")
                 and settings.get("sms_twilio_from")) or settings.get("sms_webhook_url"))


def send_sms(settings, to: str, text: str) -> None:
    """Send through Twilio (sms_twilio_*) or a generic gateway (sms_webhook_url, JSON POST)."""
    import httpx
    try:
        if settings.get("sms_twilio_sid"):
            sid = settings.get("sms_twilio_sid")
            r = httpx.post(f"https://api.twilio.com/2010-04-01/Accounts/{sid}/Messages.json",
                           auth=(sid, settings.get("sms_twilio_token")), timeout=15,
                           data={"To": to, "From": settings.get("sms_twilio_from"), "Body": text})
        elif settings.get("sms_webhook_url"):
            headers = {}
            if settings.get("sms_webhook_token"):
                headers["Authorization"] = "Bearer " + settings.get("sms_webhook_token")
            r = httpx.post(settings.get("sms_webhook_url"), json={"to": to, "message": text},
                           headers=headers, timeout=15)
        else:
            raise SmsError("SMS is not configured on this server (see README: Two-factor login)")
    except httpx.HTTPError as exc:
        raise SmsError(f"SMS gateway unreachable: {type(exc).__name__}") from exc
    if r.status_code >= 300:
        raise SmsError(f"SMS gateway refused the message (HTTP {r.status_code})")


def mask_phone(p: str) -> str:
    return (p[:3] + "*" * max(0, len(p) - 5) + p[-2:]) if p else ""


# ---- e-mail delivery ---------------------------------------------------------
def email_configured(settings) -> bool:
    return bool(settings.get("smtp_host") and settings.get("smtp_from"))


def send_email(settings, to: str, subject: str, text: str) -> None:
    msg = EmailMessage()
    msg["From"], msg["To"], msg["Subject"] = settings.get("smtp_from"), to, subject
    msg.set_content(text)
    host, port = settings.get("smtp_host"), int(settings.get("smtp_port") or 587)
    mode = (settings.get("smtp_security") or "starttls").lower()      # starttls | ssl | none
    try:
        if mode == "ssl":
            smtp = smtplib.SMTP_SSL(host, port, timeout=20, context=ssl.create_default_context())
        else:
            smtp = smtplib.SMTP(host, port, timeout=20)
        with smtp:
            if mode == "starttls":
                smtp.starttls(context=ssl.create_default_context())
            if settings.get("smtp_user"):
                smtp.login(settings.get("smtp_user"), settings.get("smtp_password"))
            smtp.send_message(msg)
    except (OSError, smtplib.SMTPException) as exc:
        raise SmsError(f"E-mail could not be sent: {type(exc).__name__}") from exc


def mask_email(e: str) -> str:
    if "@" not in e:
        return ""
    u, d = e.split("@", 1)
    return (u[:1] + "*" * max(1, len(u) - 1)) + "@" + d


# ---- persisted state -----------------------------------------------------------
class MfaStore:
    def __init__(self, path: Path):
        self.path = path
        self.lock = threading.RLock()
        self._codes: dict[str, list] = {}     # purpose -> [code hash, expiry, tries]
        self._last: dict[str, float] = {}     # channel -> time of last send

    # -- file
    def _load(self) -> dict:
        try:
            d = json.loads(self.path.read_text("utf-8"))
        except (OSError, ValueError):
            d = {}
        for k, v in (("totp_secret", ""), ("totp_last", 0), ("pending_secret", ""), ("phone", ""),
                     ("pending_phone", ""), ("email", ""), ("pending_email", ""), ("recovery", [])):
            d.setdefault(k, v)
        return d

    def _save(self, d: dict):
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(d, indent=2), "utf-8")
        try:
            os.chmod(tmp, 0o600)
        except OSError:
            pass
        os.replace(tmp, self.path)

    @staticmethod
    def _any(d: dict) -> bool:
        return bool(d["totp_secret"] or d["phone"] or d["email"])

    def _finish_enrol(self, d: dict, first: bool) -> list[str]:
        """Persist; hand out fresh recovery codes when this is the first factor (else [])."""
        codes: list[str] = []
        if first or not d["recovery"]:
            codes = make_recovery()
            d["recovery"] = [_rhash(c) for c in codes]
        self._save(d)
        return codes

    # -- state
    def status(self, settings=None) -> dict:
        d = self._load()
        return {"enabled": self._any(d), "totp": bool(d["totp_secret"]), "sms": bool(d["phone"]),
                "email": bool(d["email"]), "phone": mask_phone(d["phone"]), "address": mask_email(d["email"]),
                "recovery_left": len(d["recovery"]),
                "sms_available": sms_configured(settings) if settings else False,
                "email_available": email_configured(settings) if settings else False}

    def enabled(self) -> bool:
        return self._any(self._load())

    def methods(self, settings=None) -> list[str]:
        d = self._load()
        out = []
        if d["totp_secret"]:
            out.append("totp")
        if d["phone"] and (settings is None or sms_configured(settings)):
            out.append("sms")
        if d["email"] and (settings is None or email_configured(settings)):
            out.append("email")
        out.append("recovery")
        return out

    # -- enrolment: authenticator app
    def begin_totp(self) -> str:
        with self.lock:
            d = self._load()
            d["pending_secret"] = new_secret()
            self._save(d)
            return d["pending_secret"]

    def confirm_totp(self, code: str) -> list[str] | None:
        """Verify the first code from the app. Returns recovery codes ([] if none are new) or None if wrong."""
        with self.lock:
            d = self._load()
            if not d["pending_secret"] or totp_match(d["pending_secret"], code) is None:
                return None
            first = not self._any(d)
            d["totp_secret"], d["pending_secret"] = d["pending_secret"], ""
            d["totp_last"] = int(time.time() // STEP)
            return self._finish_enrol(d, first)

    def remove_totp(self):
        with self.lock:
            d = self._load()
            d["totp_secret"] = d["pending_secret"] = ""
            if not self._any(d):
                d["recovery"] = []
            self._save(d)

    # -- enrolment: SMS / e-mail (same shape)
    def begin_sms(self, settings, phone: str) -> None:
        phone = re.sub(r"[\s().-]", "", phone or "")
        if not PHONE_RE.match(phone):
            raise ValueError("Phone number must be in international format, e.g. +15551234567")
        if not sms_configured(settings):
            raise SmsError("SMS is not configured on this server (set sms_twilio_* or sms_webhook_url)")
        with self.lock:
            self._throttle("sms")
            d = self._load()
            d["pending_phone"] = phone
            self._save(d)
            code = self._new_code("enroll_sms")
        send_sms(settings, phone, f"Domshon verification code: {code}")

    def confirm_sms(self, code: str) -> list[str] | None:
        with self.lock:
            d = self._load()
            if not d["pending_phone"] or not self._check_code("enroll_sms", code):
                return None
            first = not self._any(d)
            d["phone"], d["pending_phone"] = d["pending_phone"], ""
            return self._finish_enrol(d, first)

    def remove_sms(self):
        with self.lock:
            d = self._load()
            d["phone"] = d["pending_phone"] = ""
            if not self._any(d):
                d["recovery"] = []
            self._save(d)

    def begin_email(self, settings, address: str) -> None:
        address = (address or "").strip()
        if not EMAIL_RE.match(address):
            raise ValueError("Enter a valid e-mail address")
        if not email_configured(settings):
            raise SmsError("E-mail is not configured on this server (set smtp_host and smtp_from)")
        with self.lock:
            self._throttle("email")
            d = self._load()
            d["pending_email"] = address
            self._save(d)
            code = self._new_code("enroll_email")
        send_email(settings, address, "Domshon verification code", f"Your Domshon verification code is {code}")

    def confirm_email(self, code: str) -> list[str] | None:
        with self.lock:
            d = self._load()
            if not d["pending_email"] or not self._check_code("enroll_email", code):
                return None
            first = not self._any(d)
            d["email"], d["pending_email"] = d["pending_email"], ""
            return self._finish_enrol(d, first)

    def remove_email(self):
        with self.lock:
            d = self._load()
            d["email"] = d["pending_email"] = ""
            if not self._any(d):
                d["recovery"] = []
            self._save(d)

    # -- login
    def send_login_code(self, settings, channel: str) -> str:
        """Text or e-mail a sign-in code. Returns a masked destination for the UI."""
        with self.lock:
            d = self._load()
            if channel == "sms":
                if not d["phone"]:
                    raise ValueError("Text-message codes are not set up for this account")
                dest = d["phone"]
            elif channel == "email":
                if not d["email"]:
                    raise ValueError("E-mail codes are not set up for this account")
                dest = d["email"]
            else:
                raise ValueError("Unknown method")
            self._throttle(channel)
            code = self._new_code("login_" + channel)
        text = f"Domshon sign-in code: {code} (valid {SMS_TTL // 60} min)"
        if channel == "sms":
            send_sms(settings, dest, text)
            return mask_phone(dest)
        send_email(settings, dest, "Domshon sign-in code", text)
        return mask_email(dest)

    def send_login_sms(self, settings) -> None:      # kept for callers of the first version
        self.send_login_code(settings, "sms")

    def verify_login(self, code: str) -> bool:
        """Accept an authenticator code, a texted / e-mailed code or a recovery code (each usable once)."""
        code = (code or "").strip()
        if not code:
            return False
        with self.lock:
            d = self._load()
            if re.fullmatch(r"\d{3}\s?\d{3}", code):
                digits = re.sub(r"\s", "", code)
                if d["totp_secret"]:
                    step = totp_match(d["totp_secret"], digits)
                    if step is not None and step > d["totp_last"]:   # a code cannot be replayed
                        d["totp_last"] = step
                        self._save(d)
                        return True
                for ch, key in (("sms", "phone"), ("email", "email")):
                    if d[key] and self._check_code("login_" + ch, digits):
                        return True
                return False
            h = _rhash(code)
            for known in d["recovery"]:
                if hmac.compare_digest(known, h):
                    d["recovery"].remove(known)
                    self._save(d)
                    return True
            return False

    def new_recovery(self) -> list[str]:
        with self.lock:
            d = self._load()
            codes = make_recovery()
            d["recovery"] = [_rhash(c) for c in codes]
            self._save(d)
            return codes

    def disable(self):
        with self.lock:
            self._save({})
            self._codes.clear()

    # -- one-time code bookkeeping (SMS / e-mail)
    def _throttle(self, channel: str):
        if time.time() - self._last.get(channel, 0.0) < SMS_MIN_GAP:
            raise ValueError(f"Please wait {SMS_MIN_GAP} seconds before asking for another code")
        self._last[channel] = time.time()

    def _new_code(self, purpose: str) -> str:
        code = str(secrets.randbelow(10 ** DIGITS)).zfill(DIGITS)
        self._codes[purpose] = [hashlib.sha256(code.encode()).hexdigest(), time.time() + SMS_TTL, 0]
        return code

    def _check_code(self, purpose: str, code: str) -> bool:
        rec = self._codes.get(purpose)
        if not rec or rec[1] < time.time() or rec[2] >= MAX_TRIES:
            self._codes.pop(purpose, None)
            return False
        if hmac.compare_digest(rec[0], hashlib.sha256(re.sub(r"\s", "", code or "").encode()).hexdigest()):
            del self._codes[purpose]
            return True
        rec[2] += 1
        return False
