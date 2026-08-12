"""Auth routes — login, OTP verification, logout."""

from fasthtml.common import (
    Button,
    Div,
    Form,
    Input,
    Label,
    Main,
    P,
    Script,
    Span,
    Title,
)
from urllib.parse import quote

from starlette.requests import Request
from starlette.responses import RedirectResponse

from nctqai.config import Config
from nctqai.components.footer import _google_analytics
from nctqai.services.auth import (
    create_session,
    destroy_session,
    send_otp,
    verify_otp,
)


_OTP_JS = """
(function() {
    var digits = document.querySelectorAll('.otp-digit');
    var hidden = document.getElementById('otp-hidden');
    var form = hidden.closest('form');

    // Single-shot submit guard. Three paths can submit this form — typing
    // the 6th digit, pasting 6 digits, and pressing Enter — and they raced
    // each other badly enough that the first response set used=true and the
    // second hit "Invalid or expired." Now: whichever fires first wins; the
    // others early-exit. submitOnce() handles JS-initiated paths;
    // form.addEventListener('submit') handles the native Enter path.
    // (Programmatic form.submit() doesn't fire the submit event, so the
    // listener only catches the native case — which is exactly what we want.)
    var submitted = false;
    function submitOnce() {
        if (submitted) return;
        submitted = true;
        form.submit();
    }
    form.addEventListener('submit', function(e) {
        if (submitted) {
            e.preventDefault();
            return;
        }
        submitted = true;
    });

    function updateHidden() {
        hidden.value = Array.from(digits).map(function(d) { return d.value; }).join('');
    }

    digits.forEach(function(input, idx) {
        input.addEventListener('input', function(e) {
            this.value = this.value.replace(/[^0-9]/g, '').slice(0, 1);
            if (this.value) {
                this.classList.add('filled');
                updateHidden();
                if (idx < digits.length - 1) {
                    digits[idx + 1].focus();
                }
                if (hidden.value.length === 6) {
                    submitOnce();
                }
            } else {
                this.classList.remove('filled');
                updateHidden();
            }
        });

        input.addEventListener('keydown', function(e) {
            if (e.key === 'Backspace' && !this.value && idx > 0) {
                digits[idx - 1].focus();
                digits[idx - 1].value = '';
                digits[idx - 1].classList.remove('filled');
                updateHidden();
            }
        });

        input.addEventListener('paste', function(e) {
            e.preventDefault();
            var paste = (e.clipboardData || window.clipboardData)
                .getData('text')
                .replace(/[^0-9]/g, '')
                .slice(0, 6);
            for (var i = 0; i < paste.length && i + idx < digits.length; i++) {
                digits[i + idx].value = paste[i];
                digits[i + idx].classList.add('filled');
            }
            updateHidden();
            var focusIdx = Math.min(idx + paste.length, digits.length - 1);
            digits[focusIdx].focus();
            if (hidden.value.length === 6) {
                submitOnce();
            }
        });

        input.addEventListener('focus', function() {
            this.select();
        });
    });

    digits[0].focus();
})();
"""


def register_auth_routes(rt):

    @rt("/login")
    def login_page(request: Request, error: str = ""):
        """Login page — email input."""
        if getattr(request.state, "user", None):
            return RedirectResponse("/", status_code=302)

        error_div = Div(Span(error), cls="auth-error") if error else ""

        form = Form(
            error_div,
            Label("Email address", htmlfor="email", cls="auth-label"),
            Input(
                type="email",
                name="email",
                id="email",
                placeholder="you@example.com",
                required=True,
                cls="uk-input mb-md",
            ),
            Button("Send Login Code", type="submit", cls="auth-btn"),
            method="post",
            action="/auth/send-code",
        )

        card = Div(
            Div("NCTQ.ai", cls="auth-brand"),
            Div(cls="auth-divider"),
            Div("Sign in to your account", cls="auth-title"),
            P("Enter your email to receive a login code.", cls="auth-subtitle"),
            form,
            cls="auth-card",
        )
        content = Div(card, cls="auth-page")
        # The login/verify pages render outside Layout(), so the footer's GA tag
        # does not reach them. Add the same gtag here for analytics coverage.
        return (Title("Login — NCTQ.ai"), Main(content, *_google_analytics()))

    @rt("/auth/send-code", methods=["POST"])
    def send_code(request: Request, email: str = ""):
        """Generate and send OTP code."""
        ip = request.client.host if request.client else "unknown"
        result = send_otp(email, ip)

        if result.get("error"):
            return RedirectResponse(
                f"/login?error={quote(result['error'])}", status_code=302
            )

        return RedirectResponse(
            f"/auth/verify?email={quote(email)}", status_code=302
        )

    @rt("/auth/verify", methods=["GET"])
    def verify_page(email: str = "", error: str = ""):
        """OTP code entry page."""
        error_div = Div(Span(error), cls="auth-error") if error else ""

        digit_inputs = Div(
            *[
                Input(
                    type="text",
                    inputmode="numeric",
                    maxlength="1",
                    pattern="[0-9]",
                    cls="otp-digit",
                    data_index=str(i),
                    autocomplete="one-time-code" if i == 0 else "off",
                )
                for i in range(6)
            ],
            cls="otp-container",
        )
        hidden_email = Input(type="hidden", name="email", value=email)
        hidden_code = Input(type="hidden", name="code", id="otp-hidden")

        form = Form(
            error_div,
            hidden_email,
            hidden_code,
            digit_inputs,
            method="post",
            action="/auth/verify",
        )
        # Resend lives in its own sibling form. HTML5 forbids nested <form>
        # elements; browsers silently drop the inner form and the Resend
        # button ends up submitting the OUTER verify form with an empty code,
        # producing a confusing "Invalid or expired" error every time.
        resend = P(
            "Didn't receive the code? ",
            Form(
                Input(type="hidden", name="email", value=email),
                Button("Resend", type="submit", cls="btn-ghost"),
                method="post",
                action="/auth/send-code",
                style="display: inline;",
            ),
            cls="auth-footer",
        )

        otp_script = Script(_OTP_JS)

        card = Div(
            Div("NCTQ.ai", cls="auth-brand"),
            Div(cls="auth-divider"),
            Div("Enter your code", cls="auth-title"),
            P(f"We sent a 6-digit code to {email}", cls="auth-subtitle"),
            form,
            resend,
            cls="auth-card",
        )
        content = Div(card, cls="auth-page")
        return (Title("Verify — NCTQ.ai"), Main(content, otp_script, *_google_analytics()))

    @rt("/auth/verify", methods=["POST"])
    def verify_code(request: Request, email: str = "", code: str = ""):
        """Verify OTP and create session."""
        ip = request.client.host if request.client else "unknown"
        user = verify_otp(email, code, ip)

        if not user:
            return RedirectResponse(
                f"/auth/verify?email={quote(email)}&error={quote('Invalid or expired code. Try again.')}",
                status_code=302,
            )

        session_id = create_session(user.id, ip)

        response = RedirectResponse("/", status_code=302)
        # Cookie security posture is controlled by infra config (the ENV var),
        # not by the request URL hostname — a request to a dev-mode server
        # via a public-looking hostname should still drop the Secure flag.
        cookie_secure = Config().environment != "development"
        response.set_cookie(
            "nctqai_session",
            session_id,
            httponly=True,
            secure=cookie_secure,
            samesite="strict",
            max_age=7 * 24 * 60 * 60,
        )
        return response

    @rt("/auth/logout", methods=["POST"])
    def logout(request: Request):
        """Destroy session and redirect to login."""
        session_id = request.cookies.get("nctqai_session")
        if session_id:
            destroy_session(session_id)

        response = RedirectResponse("/login", status_code=302)
        response.delete_cookie("nctqai_session")
        return response
