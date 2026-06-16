"""Email notification helpers for validation and ingestion failures.

Transport layer (:class:`EmailNotifier`) lives here.  Plain-text email body
builders are in :mod:`sharepoint_ingest._email_templates` and re-exported below
so existing callers that do ``from sharepoint_ingest.notifications import
build_validation_email_body`` continue to work unchanged.
"""

from __future__ import annotations

import logging
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from typing import Iterable, Optional

from sharepoint_ingest.config import EmailSettings

# Re-export body builders — keeps the public API of this module stable while
# their implementation lives in the transport-free _email_templates module.
from sharepoint_ingest._email_templates import (  # noqa: F401
    build_failure_email_body,
    build_pk_violation_email_body,
    build_validation_email_body,
)


logger = logging.getLogger(__name__)


class EmailNotifier:
    def __init__(self, settings: EmailSettings):
        self._settings = settings

    @staticmethod
    def _normalize_recipients(recipients: Optional[str | Iterable[str]]) -> list[str]:
        if recipients is None:
            return []

        if isinstance(recipients, str):
            raw_items = [recipients]
        else:
            raw_items = list(recipients)

        resolved: list[str] = []
        for item in raw_items:
            if item is None:
                continue
            for part in str(item).replace(";", ",").split(","):
                value = part.strip()
                if value:
                    resolved.append(value)
        return resolved

    def send(
        self,
        to_address: Optional[str | Iterable[str]],
        subject: str,
        body: str,
        cc_addresses: Optional[str | Iterable[str]] = None,
    ) -> bool:
        to_recipients = self._normalize_recipients(to_address)
        cc_recipients = self._normalize_recipients(cc_addresses)

        if not self._settings.enabled:
            return False
        if not to_recipients:
            return False
        if not self._settings.host:
            return False

        msg = MIMEMultipart()
        msg["From"] = self._settings.from_address
        msg["To"] = ", ".join(to_recipients)
        if cc_recipients:
            msg["Cc"] = ", ".join(cc_recipients)
        msg["Subject"] = subject
        msg.attach(MIMEText(body, "plain"))

        all_recipients = [*to_recipients, *cc_recipients]

        try:
            with smtplib.SMTP(self._settings.host, self._settings.port) as smtp:
                if self._settings.use_tls:
                    smtp.starttls()
                if self._settings.username and self._settings.password:
                    smtp.login(self._settings.username, self._settings.password)
                smtp.send_message(msg, to_addrs=all_recipients)
        except (OSError, smtplib.SMTPException) as exc:
            logger.warning(
                "Email notification could not be sent via %s:%s: %s",
                self._settings.host,
                self._settings.port,
                exc,
            )
            return False
        return True
