"""
Optional email delivery of the finished dataset.

Config-gated and off by default: when ``email.enabled`` is false, nothing here
runs, no credentials are read, and no optional dependency needs to be installed.

Two transports are supported and both are documented in README.md:

* ``smtp`` - Gmail SMTP on port 587 with STARTTLS and an **app password**.
  Simplest to set up; needs ``CRAWLER_SMTP_USER`` and ``CRAWLER_SMTP_PASSWORD``.
* ``gmail_api`` - the Gmail REST API with OAuth. Needs ``credentials.json``
  (an OAuth client) and produces ``token.json`` on first authorisation.

Credentials are **only** ever read from environment variables or from the token
files named in the config. Nothing is hard-coded, and no secret is ever written
to a log record - :func:`_redact` guards the one place a password could leak.

Failure policy, per the brief: a send failure logs an ERROR and returns ``False``.
It never raises into the caller and never fails the crawl, because the dataset on
disk is the deliverable and the email is a convenience.
"""

from __future__ import annotations

import base64
import mimetypes
import os
import smtplib
import ssl
from email.message import EmailMessage
from pathlib import Path
from typing import Any, Sequence

#: Environment variables read for the SMTP transport. Documented in README.md.
ENV_SMTP_USER = "CRAWLER_SMTP_USER"
ENV_SMTP_PASSWORD = "CRAWLER_SMTP_PASSWORD"

#: OAuth scope for the Gmail API path. ``gmail.send`` is the narrowest scope
#: that can send mail - it grants no read access to the mailbox at all.
GMAIL_SCOPES = ("https://www.googleapis.com/auth/gmail.send",)


class MailError(RuntimeError):
    """Raised internally by the transports; caught and logged by :func:`send_output`."""


def _redact(text: str) -> str:
    """
    Strip anything password-shaped out of a string before it reaches a log.

    Defensive: SMTP libraries sometimes include the failed credential in an
    exception message, and a log file is exactly the wrong place for it.
    """
    password = os.environ.get(ENV_SMTP_PASSWORD, "")
    cleaned = str(text)
    if password:
        cleaned = cleaned.replace(password, "***redacted***")
    return cleaned[:500]


def _build_message(
    sender: str,
    recipients: Sequence[str],
    subject: str,
    body: str,
    attachments: Sequence[Path],
) -> EmailMessage:
    """
    Build a MIME message with the dataset attached.

    The MIME type is guessed from the file extension and falls back to
    ``application/octet-stream``, which is correct for anything a mail client
    should download rather than render.
    """
    message = EmailMessage()
    message["From"] = sender
    message["To"] = ", ".join(recipients)
    message["Subject"] = subject
    message.set_content(body)

    for path in attachments:
        if not path.is_file():
            continue
        guessed, _ = mimetypes.guess_type(path.name)
        maintype, _, subtype = (guessed or "application/octet-stream").partition("/")
        message.add_attachment(
            path.read_bytes(),
            maintype=maintype,
            subtype=subtype or "octet-stream",
            filename=path.name,
        )
    return message


def _send_via_smtp(message: EmailMessage, config: Any, log: Any) -> None:
    """
    Send *message* over Gmail SMTP with STARTTLS and an app password.

    Uses an app password rather than an account password because Google requires
    it for SMTP on accounts with 2FA, which is the normal case. Setup steps are
    in README.md.
    """
    user = os.environ.get(ENV_SMTP_USER, "") or config.email.sender
    password = os.environ.get(ENV_SMTP_PASSWORD, "")
    if not user:
        raise MailError(
            f"SMTP transport needs a sender: set ${ENV_SMTP_USER} or email.sender"
        )
    if not password:
        raise MailError(
            f"SMTP transport needs an app password in ${ENV_SMTP_PASSWORD} "
            "(create one at https://myaccount.google.com/apppasswords)"
        )
    if not message["From"]:
        message["From"] = user

    context = ssl.create_default_context()
    log.info(
        "email.connecting",
        f"connecting to {config.email.smtp_host}:{config.email.smtp_port} as {user}",
        host=config.email.smtp_host, port=config.email.smtp_port, transport="smtp",
    )
    with smtplib.SMTP(config.email.smtp_host, config.email.smtp_port, timeout=60) as server:
        server.ehlo()
        server.starttls(context=context)
        server.ehlo()
        server.login(user, password)
        server.send_message(message)


def _send_via_gmail_api(message: EmailMessage, config: Any, log: Any) -> None:
    """
    Send *message* through the Gmail REST API using OAuth.

    Credentials flow: ``email.gmail_credentials_file`` holds the OAuth client
    (downloaded from Google Cloud Console); ``email.gmail_token_file`` caches the
    user token after the first consent and is refreshed automatically. Both paths
    are config-driven, so no secret path is baked into the source.

    The interactive consent step only happens when no valid token exists, and it
    requires a browser - which is why README.md tells you to run it once
    manually before relying on it in an unattended run.
    """
    try:
        from google.auth.transport.requests import Request
        from google.oauth2.credentials import Credentials
        from google_auth_oauthlib.flow import InstalledAppFlow
        from googleapiclient.discovery import build
    except ImportError as exc:
        raise MailError(
            "the gmail_api transport needs 'google-api-python-client', 'google-auth' "
            "and 'google-auth-oauthlib' (pip install -r requirements.txt), or set "
            f"email.smtp_or_gmail_api: smtp instead ({exc})"
        ) from exc

    token_path = config.resolve(config.email.gmail_token_file)
    credentials_path = config.resolve(config.email.gmail_credentials_file)
    credentials = None

    if token_path.is_file():
        try:
            credentials = Credentials.from_authorized_user_file(
                str(token_path), list(GMAIL_SCOPES)
            )
        except (ValueError, OSError) as exc:
            log.warn(
                "email.token_unreadable",
                f"{token_path} could not be read ({exc}); re-authorising",
                path=str(token_path),
            )

    if credentials is not None and credentials.expired and credentials.refresh_token:
        try:
            credentials.refresh(Request())
        except Exception as exc:  # google auth raises a broad family here
            raise MailError(f"refreshing the Gmail token failed: {exc}") from exc

    if credentials is None or not credentials.valid:
        if not credentials_path.is_file():
            raise MailError(
                f"no valid Gmail token and no OAuth client at {credentials_path}. "
                "Download an OAuth desktop client from Google Cloud Console, save it "
                "there, and run the crawler once interactively to authorise."
            )
        flow = InstalledAppFlow.from_client_secrets_file(
            str(credentials_path), list(GMAIL_SCOPES)
        )
        # run_local_server opens a browser; unattended runs must already have a
        # token, which is why README.md documents the one-time interactive step.
        credentials = flow.run_local_server(port=0)
        token_path.parent.mkdir(parents=True, exist_ok=True)
        token_path.write_text(credentials.to_json(), encoding="utf-8")
        log.info("email.token_saved", f"stored a Gmail token at {token_path}",
                 path=str(token_path))

    service = build("gmail", "v1", credentials=credentials, cache_discovery=False)
    encoded = base64.urlsafe_b64encode(message.as_bytes()).decode("ascii")
    service.users().messages().send(userId="me", body={"raw": encoded}).execute()


def send_output(
    config: Any,
    log: Any,
    attachments: Sequence[Path],
    run_id: str,
    summary_text: str = "",
) -> bool:
    """
    Email the finished dataset, if enabled. Returns ``True`` when a message was sent.

    Behaviour by configuration:

    * ``email.enabled`` false -> returns ``False`` immediately, with a DEBUG note
      and no side effects at all (no credential lookup, no imports).
    * enabled, send succeeds -> logs ``email.sent`` and returns ``True``.
    * enabled, send fails -> logs ``email.failed`` at ERROR with the reason and
      returns ``False``. The crawl run is unaffected, per the brief.
    """
    if not config.email.enabled:
        log.debug(
            "email.disabled",
            "email.enabled is false; skipping delivery (no credentials required)",
        )
        return False

    recipients = [address for address in config.email.recipients if address.strip()]
    if not recipients:
        log.error(
            "email.failed",
            "email.enabled is true but no recipients are configured; nothing sent",
            reason="no_recipients",
        )
        return False

    existing = [Path(path) for path in attachments if Path(path).is_file()]
    if not existing:
        log.error(
            "email.failed",
            "no output file exists to attach; nothing sent",
            reason="no_attachment",
        )
        return False

    subject = config.email.subject_template.format(run_id=run_id)
    sender = config.email.sender or os.environ.get(ENV_SMTP_USER, "") or "crawler@localhost"
    body_lines = [
        f"Payer policy document discovery run {run_id} has completed.",
        "",
        f"Attached: {', '.join(path.name for path in existing)}",
        "",
    ]
    if summary_text:
        body_lines.extend(["Per-payer summary:", "", summary_text, ""])
    body_lines.append(
        "This message was generated by the payer-policy-crawler. "
        "Documents were discovered from publicly available pages only."
    )
    message = _build_message(sender, recipients, subject, "\n".join(body_lines), existing)

    transport = str(config.email.smtp_or_gmail_api).lower()
    try:
        if transport == "smtp":
            _send_via_smtp(message, config, log)
        else:
            _send_via_gmail_api(message, config, log)
    except MailError as exc:
        log.error("email.failed", _redact(str(exc)), transport=transport,
                  recipients=len(recipients))
        return False
    except (smtplib.SMTPException, OSError, ssl.SSLError) as exc:
        log.error(
            "email.failed",
            f"{type(exc).__name__}: {_redact(str(exc))}",
            transport=transport, recipients=len(recipients),
        )
        return False
    except Exception as exc:  # pragma: no cover - third-party client errors
        # Broad by design: an unexpected error inside a mail client library must
        # not be allowed to fail a completed crawl.
        log.error(
            "email.failed",
            f"unexpected {type(exc).__name__}: {_redact(str(exc))}",
            transport=transport, recipients=len(recipients), exc_info=True,
        )
        return False

    log.info(
        "email.sent",
        f"sent {', '.join(path.name for path in existing)} to "
        f"{len(recipients)} recipient(s) via {transport}",
        transport=transport, recipients=len(recipients),
        attachments=[path.name for path in existing], subject=subject,
    )
    return True
