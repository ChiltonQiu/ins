"""Whether setup is finished, worked out from the settings in force.

Computed on every request rather than stored: a "setup complete" flag is one
more thing that can disagree with the file it describes.
"""

from __future__ import annotations

from dataclasses import dataclass

from renewal.config import Settings
from renewal.providers import is_configured


@dataclass(frozen=True)
class SetupStatus:
    model: bool
    mail: bool
    summary: bool

    @property
    def done(self) -> int:
        return int(self.model) + int(self.mail)

    @property
    def complete(self) -> bool:
        return self.model and self.mail


def status_of(settings: Settings, model_client) -> SetupStatus:
    return SetupStatus(
        model=is_configured(model_client),
        mail=bool(
            settings.imap_host and settings.imap_user and settings.imap_password
        ),
        summary=bool(settings.smtp_host and settings.notify_to),
    )
