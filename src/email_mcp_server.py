"""First-party email MCP server (stdio).

A thin wrapper over SMTP (stdlib `smtplib`, STARTTLS) so agent-sentry can relay a briefing
by email. No third-party MCP package and no provider SDK: just FastMCP (transitive via
claude-agent-sdk) and the standard library.

SMTP settings are read from the environment (the launcher loads them from the repo .env)
and the password is never logged:
  SMTP_HOST, SMTP_PORT (default 587), SMTP_USER, SMTP_PASS, SMTP_FROM

Tools exposed (prefix `mcp__email__` once registered):
  - send(to, subject, body)  -> sends a UTF-8 plain-text email over STARTTLS

Run: `python src/email_mcp_server.py` (stdio transport). See email-launcher.sh.
"""

import os
import smtplib
import ssl
from email.message import EmailMessage

from mcp.server.fastmcp import FastMCP

# Fail fast: without host/credentials every send would fail. Surface it at startup.
SMTP_HOST = os.environ.get("SMTP_HOST", "").strip()
SMTP_PORT = int(os.environ.get("SMTP_PORT", "587"))
SMTP_USER = os.environ.get("SMTP_USER", "").strip()
SMTP_PASS = os.environ.get("SMTP_PASS", "")
SMTP_FROM = os.environ.get("SMTP_FROM", "").strip() or SMTP_USER

if not (SMTP_HOST and SMTP_USER and SMTP_PASS):
    raise SystemExit(
        "SMTP_HOST, SMTP_USER and SMTP_PASS must be set in the repo .env for the email "
        "relay (SMTP_PORT defaults to 587, SMTP_FROM defaults to SMTP_USER)."
    )

mcp = FastMCP("email")


@mcp.tool()
def send(to: list[str], subject: str, body: str) -> dict:
    """Send a plain-text email to one or more recipients over STARTTLS.

    Args:
        to: Recipient email addresses (non-empty list). These come from the calling
            skill's frontmatter (`email_to`), never from free-form user input.
        subject: The email subject line.
        body: The message body (plain text / markdown source). Sent as UTF-8 text.

    Returns a dict: {ok: bool, recipients: [str]}.
    """
    if not to:
        raise ValueError("`to` must contain at least one recipient address.")

    msg = EmailMessage()
    msg["From"] = SMTP_FROM
    msg["To"] = ", ".join(to)
    msg["Subject"] = subject
    msg.set_content(body)

    # STARTTLS upgrade with certificate verification (no plaintext auth on the wire).
    context = ssl.create_default_context()
    with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=30) as server:
        server.starttls(context=context)
        server.login(SMTP_USER, SMTP_PASS)
        server.send_message(msg, from_addr=SMTP_FROM, to_addrs=to)

    return {"ok": True, "recipients": to}


if __name__ == "__main__":
    mcp.run(transport="stdio")
