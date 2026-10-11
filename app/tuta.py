"""Tuta (tuta.com) account factory -- thin wrapper over camoufox_tasks'
in-process browser automation (was an HTTP call to a separate sidecar;
now this service owns the camoufox browser directly).

Usage
-----
from app.tuta import TutaFactory

factory = TutaFactory()
email, password = await factory.create()
# -> ("pl7x9k2m4q1r@tutamail.com", "Xk9!f3w2qmN5")

url = await factory.find_activation(email, password, timeout=120)
# -> "https://pornolab.net/forum/profile.php?mode=activate&..."
"""

from . import camoufox_tasks as ct


class TutaError(Exception):
    pass


class TutaFactory:
    async def create(self) -> tuple[str, str]:
        """Create a new free @tutamail.com account. Returns (email, password)."""
        d = await ct.tuta_create()
        if not d.ok:
            raise TutaError(d.error or "tuta account creation failed")
        return d.email, d.password

    async def find_activation(
        self,
        email: str,
        password: str,
        timeout: int = 120,
        sender_pattern: str = "pornolab",
    ) -> str | None:
        """Log into the Tuta webmail UI and return the activation link, or
        None if nothing matching arrived within `timeout` seconds.

        Raises TutaError on a real failure (bad login) rather than silently
        returning None for that case too -- conflating "logged in fine,
        nothing arrived yet" with "couldn't log in at all" hid a real bug
        here once already.
        """
        d = await ct.tuta_inbox(ct.TutaInboxRequest(
            email=email, password=password, sender_pattern=sender_pattern, max_timeout=timeout,
        ))
        if not d.ok:
            raise TutaError(d.error or "tuta inbox check failed")
        return d.url or None

    async def list_inbox(self, email: str, password: str) -> list[dict]:
        d = await ct.tuta_list(ct.TutaListRequest(email=email, password=password))
        if not d.ok:
            raise TutaError(d.error or "tuta inbox listing failed")
        return d.messages
