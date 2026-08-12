"""Auth service — OTP generation, verification, sessions, and rate limiting.

Handles the full passwordless authentication flow:
1. User requests OTP via email
2. Code is sent (SMTP) or printed to console (dev mode)
3. User submits code → verified → session created
4. Session cookie tracks logged-in state with sliding expiry
"""

import json
import logging
import secrets
import smtplib
from datetime import datetime, timedelta, UTC
from email.message import EmailMessage

from nctqai.config import Config
from nctqai.db import run_sql, run_sql_write
from nctqai.models.auth import User

logger = logging.getLogger(__name__)


def _log(event: str, **kwargs) -> None:
    """Emit a structured JSON log line for observability."""
    payload = {"event": event, **kwargs}
    logger.info(json.dumps(payload))


def _utcnow() -> datetime:
    """Return timezone-aware UTC now."""
    return datetime.now(UTC)


def _ensure_tz(dt: datetime | None) -> datetime | None:
    """Add UTC timezone to naive datetimes from PostgreSQL."""
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=UTC)
    return dt


# ---------------------------------------------------------------------------
# User lookup
# ---------------------------------------------------------------------------

def get_user_by_email(email: str) -> User | None:
    """Look up an active user by email address."""
    rows = run_sql(
        "SELECT id, email, name, role, is_active, last_login "
        "FROM nctqai.users WHERE email = %s AND is_active = true",
        (email.lower().strip(),),
    )
    if not rows:
        return None
    row = rows[0]
    row["last_login"] = _ensure_tz(row.get("last_login"))
    return User(**row)


def get_user_by_id(user_id: int) -> User | None:
    """Look up an active user by ID."""
    rows = run_sql(
        "SELECT id, email, name, role, is_active, last_login "
        "FROM nctqai.users WHERE id = %s AND is_active = true",
        (user_id,),
    )
    if not rows:
        return None
    row = rows[0]
    row["last_login"] = _ensure_tz(row.get("last_login"))
    return User(**row)


# ---------------------------------------------------------------------------
# Rate limiting (internal helpers)
# ---------------------------------------------------------------------------

def _count_recent(ip: str, action: str, minutes: int = 60) -> int:
    """Count rate_limits rows for an IP + action within the given window."""
    cutoff = _utcnow() - timedelta(minutes=minutes)
    rows = run_sql(
        "SELECT COUNT(*) AS cnt FROM nctqai.rate_limits "
        "WHERE ip_address = %s AND action = %s AND created_at > %s",
        (ip, action, cutoff),
    )
    return rows[0]["cnt"] if rows else 0


def _record_rate_event(ip: str, action: str) -> None:
    """Insert a rate limit event row."""
    run_sql_write(
        "INSERT INTO nctqai.rate_limits (ip_address, action) VALUES (%s, %s)",
        (ip, action),
    )


def _count_codes_for_email(user_id: int, minutes: int = 60) -> int:
    """Count auth_codes generated for a user within the given window."""
    cutoff = _utcnow() - timedelta(minutes=minutes)
    rows = run_sql(
        "SELECT COUNT(*) AS cnt FROM nctqai.auth_codes "
        "WHERE user_id = %s AND created_at > %s",
        (user_id, cutoff),
    )
    return rows[0]["cnt"] if rows else 0


# ---------------------------------------------------------------------------
# Email sending
# ---------------------------------------------------------------------------

def _render_code_email_html(code: str, ttl_minutes: int) -> str:
    """Modern, branded HTML body for the OTP email.

    Table-based with fully inline styles for broad email-client support
    (Gmail/Outlook strip <style> blocks and most modern CSS). Uses the NCTQ.ai
    palette: navy #0F223A, blue #1D6CD0, light-blue #98abce.
    """
    year = _utcnow().year
    return f"""\
<!DOCTYPE html>
<html lang="en">
<head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1.0"></head>
<body style="margin:0;padding:0;background-color:#eef1f6;-webkit-font-smoothing:antialiased;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Helvetica,Arial,sans-serif;">
  <table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background-color:#eef1f6;">
    <tr>
      <td align="center" style="padding:32px 16px;">
        <table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="max-width:480px;width:100%;background-color:#ffffff;border-radius:12px;overflow:hidden;border:1px solid #e2e8f0;">
          <tr>
            <td style="background-color:#0F223A;padding:26px 32px;text-align:center;">
              <span style="font-size:24px;font-weight:700;letter-spacing:0.5px;color:#ffffff;">NCTQ<span style="color:#98abce;">.ai</span></span>
            </td>
          </tr>
          <tr><td style="height:4px;background-color:#1D6CD0;font-size:0;line-height:0;">&nbsp;</td></tr>
          <tr>
            <td style="padding:38px 32px 4px 32px;text-align:center;">
              <h1 style="margin:0 0 8px 0;font-size:20px;font-weight:700;color:#0F223A;">Your login code</h1>
              <p style="margin:0 0 26px 0;font-size:15px;line-height:1.5;color:#5b6b7f;">Enter this code to sign in to your NCTQ.ai dashboard.</p>
              <table role="presentation" cellpadding="0" cellspacing="0" align="center" style="margin:0 auto 22px auto;">
                <tr>
                  <td style="background-color:#f1f5fb;border:1px solid #d6e0ef;border-radius:10px;padding:18px 22px 18px 32px;">
                    <span style="font-family:'SFMono-Regular',Consolas,Menlo,monospace;font-size:34px;font-weight:700;letter-spacing:10px;color:#0F223A;">{code}</span>
                  </td>
                </tr>
              </table>
              <p style="margin:0;font-size:14px;color:#5b6b7f;">This code expires in <strong style="color:#0F223A;">{ttl_minutes} minutes</strong>.</p>
            </td>
          </tr>
          <tr>
            <td style="padding:28px 32px 30px 32px;">
              <div style="border-top:1px solid #eef2f6;margin-bottom:18px;"></div>
              <p style="margin:0;font-size:13px;line-height:1.6;color:#94a3b8;text-align:center;">If you didn't request this code, you can safely ignore this email — no changes will be made to your account.</p>
            </td>
          </tr>
        </table>
        <table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="max-width:480px;width:100%;">
          <tr>
            <td style="padding:18px 8px;text-align:center;">
              <p style="margin:0;font-size:12px;color:#aab4c2;">&copy; {year} National Council on Teacher Quality</p>
            </td>
          </tr>
        </table>
      </td>
    </tr>
  </table>
</body>
</html>"""


def _send_code_email(to_email: str, code: str, config: Config) -> None:
    """Send the OTP code via SMTP, or print to console if SMTP is not configured."""
    if not config.smtp_host:
        # Dev mode — print to console
        print(f"\n{'=' * 50}")
        print(f"  OTP CODE for {to_email}: {code}")
        print(f"{'=' * 50}\n")
        _log("otp_send", email=to_email, status="ok", mode="console")
        return

    msg = EmailMessage()
    msg["Subject"] = f"Your NCTQ.ai login code: {code}"
    msg["From"] = config.smtp_from
    msg["To"] = to_email
    # Plain-text fallback (shown by clients that can't render HTML).
    msg.set_content(
        f"Your NCTQ.ai verification code is:\n\n"
        f"    {code}\n\n"
        f"This code expires in {config.otp_ttl_minutes} minutes.\n\n"
        f"If you did not request this code, you can safely ignore this email."
    )
    # Branded HTML version (preferred by modern clients).
    msg.add_alternative(
        _render_code_email_html(code, config.otp_ttl_minutes), subtype="html"
    )

    try:
        with smtplib.SMTP(config.smtp_host, config.smtp_port, timeout=15) as server:
            server.starttls()
            if config.smtp_user:
                server.login(config.smtp_user, config.smtp_password.get_secret_value())
            server.send_message(msg)
        _log("otp_send", email=to_email, status="ok", smtp_host=config.smtp_host)
    except Exception as exc:
        _log("otp_send", email=to_email, status="error", error=str(exc), smtp_host=config.smtp_host)
        raise


# ---------------------------------------------------------------------------
# OTP
# ---------------------------------------------------------------------------

def send_otp(email: str, ip_address: str) -> dict:
    """Generate and send a one-time passcode.

    Returns {"ok": True} on success or {"error": "msg"} on rate limit.
    Never reveals whether the email exists (returns ok either way).

    Codes are stored plaintext; bounded by 10-min TTL + claim-on-use +
    per-IP/per-email rate limits. Acceptable per audit finding #3
    (docs/audit/2026-05-25-audit-report.md, 2026-05-25).
    """
    config = Config()
    email = email.lower().strip()

    # Rate limit: per-IP for send_code action
    if _count_recent(ip_address, "send_code") >= config.otp_rate_limit_per_ip:
        _log("otp_request", email=email, status="rate_limited_ip", ip=ip_address)
        return {"error": "Too many requests. Please try again later."}

    # Always record the send attempt for IP rate limiting
    _record_rate_event(ip_address, "send_code")

    # Look up user — but don't reveal whether they exist
    user = get_user_by_email(email)
    if user is None:
        _log("otp_request", email=email, status="unknown_email", ip=ip_address)
        # Silent success — don't reveal that the email is not registered
        return {"ok": True}

    # Rate limit: per-email (max codes per hour)
    if _count_codes_for_email(user.id) >= config.otp_rate_limit_per_email:
        _log("otp_request", email=email, status="rate_limited_email", ip=ip_address)
        return {"error": "Too many codes requested. Please try again later."}

    # Invalidate any old unused codes for this user
    run_sql_write(
        "UPDATE nctqai.auth_codes SET used = true "
        "WHERE user_id = %s AND used = false",
        (user.id,),
    )

    # Generate 6-digit code (zero-padded)
    code = f"{secrets.randbelow(1000000):06d}"

    # Store the plaintext code in auth_codes. Per audit finding #3
    # (2026-05-25), a 6-digit OTP gains nothing from SHA-256 hashing
    # without a salt — a leaked row is brute-forced in microseconds either
    # way. Defense-in-depth here is the 10-min TTL, claim-on-use CAS, and
    # the per-IP / per-email rate limits enforced above and in verify_otp.
    now = _utcnow()
    expires = now + timedelta(minutes=config.otp_ttl_minutes)
    run_sql_write(
        "INSERT INTO nctqai.auth_codes "
        "(user_id, code, expires_at, ip_address, created_at) "
        "VALUES (%s, %s, %s, %s, %s)",
        (user.id, code, expires, ip_address, now),
    )

    # Send the code
    _send_code_email(email, code, config)
    _log("otp_request", email=email, status="ok", ip=ip_address)

    return {"ok": True}


def verify_otp(email: str, code: str, ip_address: str) -> User | None:
    """Verify a one-time passcode and return the user if valid.

    Returns None on any failure (wrong code, expired, rate limited, etc.).

    Codes are stored plaintext; bounded by 10-min TTL + claim-on-use +
    per-IP/per-email rate limits. Acceptable per audit finding #3
    (docs/audit/2026-05-25-audit-report.md, 2026-05-25).
    """
    config = Config()
    email = email.lower().strip()
    code = code.strip()
    _log("otp_verify_attempt", email=email, ip=ip_address, code_length=len(code))

    # Rate limit: per-IP for verify_code action
    if _count_recent(ip_address, "verify_code") >= config.otp_rate_limit_per_ip:
        _log("otp_verify", email=email, status="rate_limited", ip=ip_address)
        return None

    # Look up user
    user = get_user_by_email(email)
    if user is None:
        _log("otp_verify", email=email, status="user_not_found", ip=ip_address)
        _record_rate_event(ip_address, "verify_code")
        return None

    # Find the latest unused code for this user
    rows = run_sql(
        "SELECT id, code, expires_at, ip_address, attempts "
        "FROM nctqai.auth_codes "
        "WHERE user_id = %s AND used = false "
        "ORDER BY created_at DESC LIMIT 1",
        (user.id,),
    )
    if not rows:
        _record_rate_event(ip_address, "verify_code")
        return None

    auth_code = rows[0]
    code_id = auth_code["id"]
    expires_at = _ensure_tz(auth_code["expires_at"])
    stored_ip = auth_code["ip_address"]
    attempts = auth_code["attempts"] or 0

    # IP binding check — behind Traefik, the client IP may vary between
    # send and verify requests (different source ports/connections). Log but
    # don't block for now. Re-enable when X-Forwarded-For is properly configured.
    if stored_ip != ip_address:
        _log("otp_verify", email=email, status="ip_mismatch_warning", stored_ip=stored_ip, request_ip=ip_address)
        # Don't block — proxy architecture makes IPs unreliable

    _log("otp_verify_code_check", email=email, code_match=(auth_code["code"] == code),
         expired=(_utcnow() > expires_at), attempts=attempts)

    # Check expiry
    if _utcnow() > expires_at:
        _log("otp_verify", email=email, status="expired")
        # Mark expired code as used so it can't be retried
        run_sql_write(
            "UPDATE nctqai.auth_codes SET used = true WHERE id = %s",
            (code_id,),
        )
        _record_rate_event(ip_address, "verify_code")
        return None

    # Check max attempts
    if attempts >= config.otp_max_attempts:
        # Code is exhausted — mark as used
        run_sql_write(
            "UPDATE nctqai.auth_codes SET used = true WHERE id = %s",
            (code_id,),
        )
        _record_rate_event(ip_address, "verify_code")
        return None

    # Check code (plaintext compare; see send_otp docstring for rationale)
    if auth_code["code"] != code:
        # Wrong code — increment attempts
        run_sql_write(
            "UPDATE nctqai.auth_codes SET attempts = attempts + 1 WHERE id = %s",
            (code_id,),
        )
        _record_rate_event(ip_address, "verify_code")
        # If this was the last attempt, invalidate
        if attempts + 1 >= config.otp_max_attempts:
            run_sql_write(
                "UPDATE nctqai.auth_codes SET used = true WHERE id = %s",
                (code_id,),
            )
        return None

    # Code is correct — atomically claim it. The conditional `AND used = false`
    # turns this into a CAS: two concurrent requests with the same valid OTP
    # would both reach this line, but only one will see rowcount=1. The
    # earlier SELECT...WHERE used=false at line 232 *cannot* serialize on
    # its own — psycopg2's default isolation is READ COMMITTED, so both
    # readers see used=false and pass through to here.
    rowcount = run_sql_write(
        "UPDATE nctqai.auth_codes SET used = true "
        "WHERE id = %s AND used = false",
        (code_id,),
    )
    if rowcount == 0:
        # Lost the race — another request already claimed this OTP.
        _log("otp_verify", email=email, status="already_claimed", ip=ip_address)
        _record_rate_event(ip_address, "verify_code")
        return None

    # Update last_login on the user
    run_sql_write(
        "UPDATE nctqai.users SET last_login = %s WHERE id = %s",
        (_utcnow(), user.id),
    )

    _log("otp_verify", email=email, status="ok", user_id=user.id)
    return user


# ---------------------------------------------------------------------------
# Sessions
# ---------------------------------------------------------------------------

def create_session(user_id: int, ip_address: str) -> str:
    """Create a new session and return the session ID (a secure random token)."""
    config = Config()
    session_id = secrets.token_urlsafe(32)
    now = _utcnow()
    expires = now + timedelta(days=config.session_ttl_days)

    run_sql_write(
        "INSERT INTO nctqai.sessions "
        "(session_id, user_id, ip_address, expires_at, created_at) "
        "VALUES (%s, %s, %s, %s, %s)",
        (session_id, user_id, ip_address, expires, now),
    )

    return session_id


def get_session_user(session_id: str) -> User | None:
    """Look up a session, check expiry, apply sliding window, and return the user.

    Returns None if session is expired, missing, or user is inactive.
    Extends the session expiry on each valid access (sliding window).
    """
    if not session_id:
        return None

    config = Config()

    rows = run_sql(
        "SELECT user_id, expires_at FROM nctqai.sessions "
        "WHERE session_id = %s",
        (session_id,),
    )
    if not rows:
        return None

    session = rows[0]
    expires_at = _ensure_tz(session["expires_at"])

    # Check expiry
    if _utcnow() > expires_at:
        # Clean up the expired session
        destroy_session(session_id)
        return None

    # Look up the user (must still be active)
    user = get_user_by_id(session["user_id"])
    if user is None:
        # User deactivated — kill the session
        destroy_session(session_id)
        return None

    # Sliding window — extend session expiry on each access
    new_expires = _utcnow() + timedelta(days=config.session_ttl_days)
    run_sql_write(
        "UPDATE nctqai.sessions SET expires_at = %s WHERE session_id = %s",
        (new_expires, session_id),
    )

    return user


def destroy_session(session_id: str) -> None:
    """Delete a single session."""
    run_sql_write(
        "DELETE FROM nctqai.sessions WHERE session_id = %s",
        (session_id,),
    )


def destroy_user_sessions(user_id: int) -> None:
    """Delete all sessions for a user (e.g., on password change or admin action)."""
    run_sql_write(
        "DELETE FROM nctqai.sessions WHERE user_id = %s",
        (user_id,),
    )


# ---------------------------------------------------------------------------
# Cleanup
# ---------------------------------------------------------------------------

def cleanup_expired() -> dict:
    """Delete expired sessions, used codes, and old rate limit records.

    Returns a dict with counts of deleted rows per table.
    """
    now = _utcnow()
    cutoff_24h = now - timedelta(hours=24)

    sessions_deleted = run_sql_write(
        "DELETE FROM nctqai.sessions WHERE expires_at < %s",
        (now,),
    )

    codes_deleted = run_sql_write(
        "DELETE FROM nctqai.auth_codes WHERE used = true AND created_at < %s",
        (cutoff_24h,),
    )

    rates_deleted = run_sql_write(
        "DELETE FROM nctqai.rate_limits WHERE created_at < %s",
        (cutoff_24h,),
    )

    return {
        "sessions_deleted": sessions_deleted,
        "codes_deleted": codes_deleted,
        "rates_deleted": rates_deleted,
    }
