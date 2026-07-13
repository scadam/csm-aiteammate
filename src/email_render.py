"""
branded HTML email renderer.

Turns the plain-text outreach draft (written in the CSM's voice and grounded in
the manager's Microsoft 365 activity) into a polished, professional HTML email
that follows the brand treatment used across the product — the brand
wordmark, the navy/teal palette, and the teal→green→amber accent bar — with a
clear call-to-action and a proper footer.

Design notes (deliberate, for real email clients):

* **Table-based layout + inline styles.** Email clients (Outlook, Gmail, Apple
  Mail) strip ``<style>``/external CSS and flexbox/grid. Everything here is a
  nested ``<table>`` with inline ``style=`` attributes so it renders the same
  everywhere, including Outlook's Word engine.
* **No external/remote images.** Remote images are blocked by default in most
  clients and a real-world brand logo would be trademarked, so the brand is expressed with
  a styled **wordmark** (the same treatment the dashboards use) rather than an
  ``<img>`` — nothing to download, nothing to block.
* **Bulletproof button.** The CTA is a padded, table-wrapped anchor so it works
  in Outlook (which ignores ``padding`` on ``<a>``).
"""

from __future__ import annotations

import html
import re

# brand palette (matches the control-plane dashboards — L2Q look).
NAVY = "#0A2540"
NAVY_2 = "#001B2B"
TEAL = "#00A3A1"
TEAL_BRIGHT = "#00C2C0"
INK = "#0A2540"
MUTED = "#64748B"
BG = "#F7F9FB"
BORDER = "#E2E8F0"
ACCENT = "linear-gradient(90deg,#00A3A1 0%,#00C2C0 100%)"


def _first_name(name: str) -> str:
    return (name or "").strip().split(" ")[0] if name else ""


def _paragraphs(body_text: str, greeting_present: bool) -> str:
    """Convert the plain-text draft into inline-styled HTML paragraphs.

    Blank lines separate paragraphs; single newlines become ``<br>``. The text is
    HTML-escaped first so a draft can never inject markup.
    """
    text = (body_text or "").strip()
    # Collapse 3+ newlines to a paragraph break.
    blocks = re.split(r"\n\s*\n", text)
    p_style = f"margin:0 0 16px;font-size:15px;line-height:1.65;color:{INK};"
    out: list[str] = []
    for block in blocks:
        block = block.strip()
        if not block:
            continue
        out.append(f'<p style="{p_style}">{html.escape(block).replace(chr(10), "<br>")}</p>')
    return "\n".join(out)


def render_email_html(
    *,
    subject: str,
    body_text: str,
    manager_name: str = "Your Customer Success Manager",
    manager_role: str = "Customer Success Manager",
    manager_email: str = "",
    recipient_name: str = "",
    account_name: str = "",
    cta_label: str = "Book 20 minutes",
    cta_url: str = "https://www.example.com/contact-us",
    grounded_note: str = "Personalised from your recent conversations and grounded in approved content.",
) -> str:
    """Render a branded HTML email around the plain-text ``body_text``."""
    safe_subject = html.escape(subject or "A note from your Customer Success team")
    initials = "".join(
        p[0] for p in (manager_name or "CSM").split(" ")[:2] if p
    ).upper() or "CS"
    body_html = _paragraphs(body_text, greeting_present=bool(recipient_name))
    role_line = html.escape(manager_role or "Customer Success Manager")
    mgr = html.escape(manager_name or "Your Customer Success Manager")
    mgr_email = html.escape(manager_email or "customer.success@example.com")
    cta = html.escape(cta_label)
    cta_href = html.escape(cta_url, quote=True)

    return f"""\
<!DOCTYPE html>
<html lang="en" xmlns="http://www.w3.org/1999/xhtml">
<head>
<meta charset="utf-8" />
<meta name="viewport" content="width=device-width, initial-scale=1.0" />
<meta name="x-apple-disable-message-reformatting" />
<title>{safe_subject}</title>
</head>
<body style="margin:0;padding:0;background:{BG};-webkit-text-size-adjust:100%;">
  <div style="display:none;max-height:0;overflow:hidden;opacity:0;">{safe_subject} — from {mgr}.</div>
  <table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background:{BG};padding:24px 12px;">
    <tr>
      <td align="center">
        <table role="presentation" width="600" cellpadding="0" cellspacing="0" style="width:600px;max-width:600px;background:#ffffff;border:1px solid {BORDER};border-radius:14px;overflow:hidden;font-family:'Segoe UI',-apple-system,BlinkMacSystemFont,Arial,sans-serif;">
          <!-- accent bar -->
          <tr><td style="height:6px;line-height:6px;font-size:0;background:{TEAL};background-image:{ACCENT};">&nbsp;</td></tr>
          <!-- header / wordmark -->
          <tr>
            <td style="background:{NAVY};padding:20px 28px;">
              <table role="presentation" width="100%" cellpadding="0" cellspacing="0">
                <tr>
                  <td style="vertical-align:middle;">
                    <span style="font-size:26px;font-weight:800;letter-spacing:.14em;color:#ffffff;">CSM Autopilot</span>
                    <div style="font-size:12px;font-weight:600;color:{TEAL_BRIGHT};letter-spacing:.02em;margin-top:3px;">Customer Success</div>
                  </td>
                  <td align="right" style="vertical-align:middle;">
                    <span style="display:inline-block;width:64px;height:5px;border-radius:3px;background:{TEAL};background-image:{ACCENT};">&nbsp;</span>
                  </td>
                </tr>
              </table>
            </td>
          </tr>
          <!-- body -->
          <tr>
            <td style="padding:28px 28px 8px;">
              <h1 style="margin:0 0 18px;font-size:19px;line-height:1.35;font-weight:750;color:{NAVY};">{safe_subject}</h1>
              {body_html}
            </td>
          </tr>
          <!-- CTA -->
          <tr>
            <td style="padding:6px 28px 24px;">
              <table role="presentation" cellpadding="0" cellspacing="0">
                <tr>
                  <td align="center" style="border-radius:8px;background:{TEAL};">
                    <a href="{cta_href}" target="_blank" style="display:inline-block;padding:12px 26px;font-size:14px;font-weight:700;color:#ffffff;text-decoration:none;border-radius:8px;">{cta} &rarr;</a>
                  </td>
                </tr>
              </table>
            </td>
          </tr>
          <!-- signature -->
          <tr>
            <td style="padding:4px 28px 22px;">
              <table role="presentation" cellpadding="0" cellspacing="0">
                <tr>
                  <td style="vertical-align:middle;padding-right:12px;">
                    <span style="display:inline-block;width:46px;height:46px;border-radius:10px;background:{NAVY};background-image:linear-gradient(135deg,{NAVY},{TEAL});color:#ffffff;font-weight:800;font-size:15px;text-align:center;line-height:46px;">{html.escape(initials)}</span>
                  </td>
                  <td style="vertical-align:middle;">
                    <div style="font-size:14px;font-weight:750;color:{NAVY};">{mgr}</div>
                    <div style="font-size:12px;color:{MUTED};">{role_line}</div>
                    <div style="font-size:12px;color:{TEAL};">{mgr_email}</div>
                  </td>
                </tr>
              </table>
            </td>
          </tr>
          <!-- footer -->
          <tr>
            <td style="background:{NAVY_2};padding:18px 28px;">
              <p style="margin:0 0 8px;font-size:11px;line-height:1.6;color:#9fb4c2;">
                Sent by {mgr} ({role_line}){(' regarding your relationship with ' + html.escape(account_name)) if account_name else ''}.
                <br>{html.escape(grounded_note)}
              </p>
              <p style="margin:0 0 8px;font-size:10.5px;line-height:1.6;color:#7c91a3;">
                London Stock Exchange Group plc · 10 Paternoster Square, London EC4M 7LS, United Kingdom.
                This message and any attachments are confidential and intended for the named recipient.
              </p>
              <p style="margin:0;font-size:10.5px;color:#7c91a3;">
                <a href="https://www.example.com/privacy" target="_blank" style="color:{TEAL_BRIGHT};text-decoration:underline;">Privacy</a>
                &nbsp;·&nbsp;
                <a href="https://www.example.com" target="_blank" style="color:{TEAL_BRIGHT};text-decoration:underline;">example.com</a>
                &nbsp;·&nbsp;
                <a href="#" target="_blank" style="color:{TEAL_BRIGHT};text-decoration:underline;">Unsubscribe</a>
              </p>
            </td>
          </tr>
        </table>
        <table role="presentation" width="600" cellpadding="0" cellspacing="0" style="width:600px;max-width:600px;">
          <tr><td align="center" style="padding:14px 8px;font-family:'Segoe UI',Arial,sans-serif;font-size:10.5px;color:{MUTED};">
            © London Stock Exchange Group plc. All rights reserved.
          </td></tr>
        </table>
      </td>
    </tr>
  </table>
</body>
</html>"""
