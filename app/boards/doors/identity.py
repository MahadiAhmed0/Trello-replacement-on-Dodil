"""Who is calling. The Dodil gateway signs the person in through the app's user pool and passes
them as the X-Dodil-User header (JSON: sub, email, ...). It strips any copy a client sends, so the
app trusts it and ships no login code of its own.

Local development only: DEV_USER=you@example.com acts as that person without the gateway. It is
ignored whenever the app runs behind a user pool (DODIL_USER_POOL is set by .dodil/deploy.yaml), so
a stray DEV_USER in production can never turn anonymous requests into a signed-in person.
"""
from __future__ import annotations

import json
import os
from typing import Optional

from boards.domain.model import EMAIL, Person

BEHIND_GATEWAY = bool(os.environ.get("DODIL_USER_POOL", "").strip())
DEV_USER = "" if BEHIND_GATEWAY else os.environ.get("DEV_USER", "").strip().lower()
# Refuse people whose email the pool says is NOT verified. Off by default because a pool that does
# not verify emails may mark everyone unverified; see README "Security notes".
REQUIRE_VERIFIED = os.environ.get("REQUIRE_VERIFIED_EMAIL", "").strip().lower() in ("1", "true")


def person_from(headers) -> Optional[Person]:
    raw = headers.get("x-dodil-user")
    if raw:
        try:
            user = json.loads(raw)
        except ValueError:
            return None
        if not isinstance(user, dict):
            return None
        if REQUIRE_VERIFIED and user.get("email_verified") is False:
            return None
        email = str(user.get("email") or "").strip().lower()
        return Person(email=email) if EMAIL.match(email) else None
    return Person(email=DEV_USER) if DEV_USER else None
