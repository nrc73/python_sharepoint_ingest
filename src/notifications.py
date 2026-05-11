from __future__ import annotations

import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from typing import Iterable, Optional

from src.config import EmailSettings


class EmailNotifier:
    def __init__(self, settings: EmailSettings):
        self._settings = settings

    def send(
        self,
        to_address: Optional[str],
        subject: str,
        body: str,
    ) -> bool:
        if not self._settings.enabled:
            return False
        if not to_address:
            return False
        if not self._settings.host:
            return False

        msg = MIMEMultipart()
        msg["From"] = self._settings.from_address
        msg["To"] = to_address
        msg["Subject"] = subject
        msg.attach(MIMEText(body, "plain"))

        with smtplib.SMTP(self._settings.host, self._settings.port) as smtp:
            if self._settings.use_tls:
                smtp.starttls()
            if self._settings.username and self._settings.password:
                smtp.login(self._settings.username, self._settings.password)
            smtp.send_message(msg)
        return True


def build_validation_email_body(process_name: str, issues: Iterable[str]) -> str:
    lines = [f"Ingestion validation issues for process: {process_name}", "", "Issues:"]
    for issue in issues:
        lines.append(f"- {issue}")
    return "\n".join(lines)
