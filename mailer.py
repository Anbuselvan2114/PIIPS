"""
Outbound email for PIIPS: user lifecycle notifications (welcome, password
reset, activation/deactivation) sent through the SMTP server configured on
the "Mail Server Setting" screen (Super Admin only).

No third-party package is used - stdlib smtplib/email only, matching the
rest of this codebase's preference for no unnecessary dependencies.
"""

import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

import database


class MailError(Exception):
    """Raised when a mail could not be sent (not configured, or the SMTP
    server rejected the connection/login/send)."""


def send_mail(to_addr, subject, html_body):
    """Send one HTML email via the configured SMTP server. Raises MailError
    on any failure (not configured, connection, auth, or send). Sample:
    send_mail('jsmith@precisionit.co.in', 'Welcome', '<html>...</html>')"""
    settings = database.get_mail_settings()
    if not settings or not settings.get("SMTPHost") or not settings.get("EmailID"):
        raise MailError("Mail server is not configured. Set it up under Mail Server Setting.")

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = settings["EmailID"]
    msg["To"] = to_addr
    msg.attach(MIMEText(html_body, "html"))

    try:
        with smtplib.SMTP(settings["SMTPHost"], int(settings["SMTPPort"] or 587), timeout=15) as smtp:
            smtp.starttls()
            if settings.get("Password"):
                smtp.login(settings["EmailID"], settings["Password"])
            smtp.sendmail(settings["EmailID"], [to_addr], msg.as_string())
    except (smtplib.SMTPException, OSError) as exc:
        raise MailError(str(exc)) from exc


# ---------------------------------------------------------------------------
# Shared visual shell (inline CSS - email clients ignore <style> blocks and
# strip external stylesheets/images, so every rule lives on the element).
# Palette matches the app's own brand mark (components.jsx Logo): teal
# gradient #22d3ee -> #0e7490, success green #16a34a, danger red for the
# deactivation notice.
# ---------------------------------------------------------------------------

_FONT = "font-family:-apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif;"


def _shell(accent, heading, body_html, footer_note="", base_url=""):
    logo_cell = (
        f'<img src="{base_url}/icon-192.png" width="36" height="36" '
        f'style="display:block;border-radius:9px;" alt="PIIPS">'
        if base_url else "🧾"
    )
    return f"""\
<!doctype html>
<html>
  <body style="margin:0;padding:32px 16px;background:#f1f5f9;{_FONT}">
    <table role="presentation" width="100%" cellpadding="0" cellspacing="0">
      <tr>
        <td align="center">
          <table role="presentation" width="480" cellpadding="0" cellspacing="0"
                 style="max-width:480px;width:100%;background:#ffffff;border-radius:14px;
                        overflow:hidden;box-shadow:0 2px 10px rgba(15,23,42,0.08);">
            <tr>
              <td style="background:linear-gradient(135deg,#4c1d95,#c026d3);padding:28px 32px;">
                <table role="presentation" cellpadding="0" cellspacing="0">
                  <tr>
                    <td style="width:36px;height:36px;background:#ffffff;border-radius:9px;
                               text-align:center;vertical-align:middle;font-size:18px;">{logo_cell}</td>
                    <td style="padding-left:12px;color:#ffffff;font-size:20px;font-weight:700;{_FONT}">
                      PIIPS
                    </td>
                  </tr>
                </table>
              </td>
            </tr>
            <tr>
              <td style="padding:32px;">
                <h1 style="margin:0 0 16px;color:#0f172a;font-size:19px;font-weight:700;{_FONT}">
                  {heading}
                </h1>
                <div style="color:#334155;font-size:14.5px;line-height:1.6;{_FONT}">
                  {body_html}
                </div>
              </td>
            </tr>
            <tr>
              <td style="padding:0 32px 28px;">
                <div style="height:1px;background:#e2e8f0;margin-bottom:18px;"></div>
                <div style="color:#94a3b8;font-size:12px;line-height:1.6;{_FONT}">
                  Precision Intelligence Invoice Processing Suite
                  {f"<br>{footer_note}" if footer_note else ""}
                  <br>This is an automated message - please do not reply to this email.
                </div>
              </td>
            </tr>
          </table>
        </td>
      </tr>
    </table>
  </body>
</html>
"""


def _password_chip(password):
    return f"""\
<table role="presentation" cellpadding="0" cellspacing="0" style="margin:18px 0;width:100%;">
  <tr>
    <td style="background:#faf5ff;border:1px dashed #7c3aed;border-radius:10px;
               padding:14px 18px;text-align:center;">
      <span style="font-family:Consolas,Menlo,monospace;font-size:18px;
                    letter-spacing:1px;color:#6d28d9;font-weight:700;">{password}</span>
    </td>
  </tr>
</table>
"""


def _button(label, url):
    if not url:
        return ""
    # ?signout=1 tells the app (App.jsx) to sign out whatever session is
    # already active in this browser before showing the login screen - the
    # link is often opened on the same machine/browser an admin just used
    # to create the account, which would otherwise land on the admin's own
    # dashboard instead of a login prompt for the new user.
    sep = "&" if "?" in url else "?"
    href = f"{url}{sep}signout=1"
    return f"""\
<table role="presentation" cellpadding="0" cellspacing="0" style="margin:22px 0 4px;">
  <tr>
    <td style="background:#7c3aed;border-radius:8px;">
      <a href="{href}" style="display:inline-block;padding:11px 24px;color:#ffffff;
                              font-size:14px;font-weight:600;text-decoration:none;{_FONT}">
        {label}
      </a>
    </td>
  </tr>
</table>
"""


def welcome_email_html(username, password, login_url="", user_type=""):
    type_row = f"""\
  <tr><td style="color:#64748b;font-size:13px;padding-bottom:2px;padding-top:10px;">Account type</td></tr>
  <tr><td style="font-weight:700;color:#0f172a;font-size:15px;">{user_type}</td></tr>
""" if user_type else ""
    body = f"""\
Your PIIPS account has been created. Sign in with the temporary password
below - you'll be asked to set your own password the first time you log in.
<table role="presentation" cellpadding="0" cellspacing="0" style="margin-top:14px;">
  <tr><td style="color:#64748b;font-size:13px;padding-bottom:2px;">Username</td></tr>
  <tr><td style="font-weight:700;color:#0f172a;font-size:15px;">{username}</td></tr>
{type_row}
</table>
{_password_chip(password)}
{_button("Sign in to PIIPS", login_url)}
"""
    return _shell("#7c3aed", "Your PIIPS account is ready", body, base_url=login_url)


def password_reset_email_html(username, password, login_url=""):
    body = f"""\
A new temporary password has been generated for your PIIPS account
(<b>{username}</b>). Sign in with it below - you'll be asked to set your
own password the first time you log in.
{_password_chip(password)}
{_button("Sign in to PIIPS", login_url)}
<p style="margin-top:18px;color:#94a3b8;font-size:12.5px;">
  If you did not request this, contact your administrator immediately.
</p>
"""
    return _shell("#7c3aed", "Your PIIPS password was reset", body, base_url=login_url)


def deactivation_email_html(username, login_url=""):
    body = f"""\
Your PIIPS account (<b>{username}</b>) has been <b style="color:#dc2626;">deactivated</b>
by an administrator. You will not be able to sign in until it is reactivated.
<p style="margin-top:12px;color:#94a3b8;font-size:12.5px;">
  If you believe this is a mistake, contact your administrator.
</p>
"""
    return _shell("#dc2626", "Account deactivated", body, base_url=login_url)


def activation_email_html(username, login_url=""):
    body = f"""\
Your PIIPS account (<b>{username}</b>) has been <b style="color:#16a34a;">reactivated</b>.
You can sign in again.
{_button("Sign in to PIIPS", login_url)}
"""
    return _shell("#16a34a", "Account reactivated", body, base_url=login_url)
