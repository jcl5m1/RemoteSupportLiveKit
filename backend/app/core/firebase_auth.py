"""Firebase ID token verification for the support role.

Support operators sign in with Google SSO through Firebase Auth. Firebase
issues its own ID token carrying custom claims (``role``, ``admin``) that we
set server-side -- which is what lets docs/04's admin gate be a single JWT
field rather than a database join on every request.

Two tokens, two jobs, do not confuse them:
  * this token  -- authenticates a support operator to *our backend*
  * LiveKit JWT -- minted by us afterwards, authorizes a room join

Verification follows Google's published requirements for Firebase ID tokens:
RS256, signed by a key from the securetoken JWKS, ``iss`` =
https://securetoken.google.com/<project>, ``aud`` = <project>, and unexpired.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

import httpx
import jwt
from jwt import PyJWKClient

JWKS_URL = (
    "https://www.googleapis.com/service_accounts/v1/jwk/"
    "securetoken@system.gserviceaccount.com"
)
ALGORITHMS = ["RS256"]
# Small tolerance for clock skew between us and Google. Not large enough to
# meaningfully extend an expired token's life.
LEEWAY_SECONDS = 30


class AuthError(Exception):
    """Raised for any verification failure. The router maps this to 401."""


@dataclass(frozen=True)
class SupportPrincipal:
    """An authenticated support operator."""

    user_id: str          # Firebase uid -> sessions.support_user_id
    email: str
    display_name: str | None
    is_admin: bool
    hosted_domain: str | None

    @property
    def livekit_identity(self) -> str:
        return f"support-{self.user_id}"


class FirebaseTokenVerifier:
    """Verifies Firebase ID tokens.

    The JWKS client caches signing keys and refreshes them on rotation, so a
    steady-state request does no outbound network call.
    """

    def __init__(
        self,
        *,
        project_id: str,
        allowed_domains: frozenset[str] = frozenset(),
        allowed_emails: frozenset[str] = frozenset(),
        admin_emails: frozenset[str] = frozenset(),
    ) -> None:
        self._project_id = project_id
        self._issuer = f"https://securetoken.google.com/{project_id}"
        self._allowed_domains = allowed_domains
        self._allowed_emails = allowed_emails
        self._admin_emails = admin_emails
        self._jwks = PyJWKClient(JWKS_URL, cache_keys=True, lifespan=3600)

    def verify(self, token: str) -> SupportPrincipal:
        claims = self._decode(token)
        self._authorize(claims)
        return self._to_principal(claims)

    # -- steps ----------------------------------------------------------

    def _decode(self, token: str) -> dict[str, Any]:
        try:
            signing_key = self._jwks.get_signing_key_from_jwt(token)
            return jwt.decode(
                token,
                signing_key.key,
                algorithms=ALGORITHMS,
                audience=self._project_id,
                issuer=self._issuer,
                leeway=LEEWAY_SECONDS,
                options={"require": ["exp", "iat", "sub", "aud", "iss"]},
            )
        except jwt.ExpiredSignatureError as exc:
            raise AuthError("token_expired") from exc
        except jwt.InvalidTokenError as exc:
            raise AuthError(f"invalid_token: {exc}") from exc
        except httpx.HTTPError as exc:  # JWKS fetch failed
            raise AuthError("jwks_unavailable") from exc

    def _authorize(self, claims: dict[str, Any]) -> None:
        """Domain / allowlist gate.

        Without this, *any* Google account could sign in and claim the trusted
        support role -- which means joining live calls and toggling the AI. See
        docs/08 § trust model.
        """
        if not claims.get("sub"):
            raise AuthError("missing_subject")

        email = (claims.get("email") or "").lower()
        if not email:
            raise AuthError("missing_email")
        if not claims.get("email_verified", False):
            raise AuthError("email_not_verified")

        if not self._allowed_domains and not self._allowed_emails:
            raise AuthError("no_allowlist_configured")

        if email in self._allowed_emails:
            return

        domain = email.rpartition("@")[2]
        if domain in self._allowed_domains:
            # Belt and braces: for a Workspace account Google also sets `hd`.
            # We gate on the verified email domain rather than `hd` alone,
            # because `hd` is not enumerated in Google's claims_supported and
            # is absent for consumer accounts.
            return

        raise AuthError("domain_not_allowed")

    def _to_principal(self, claims: dict[str, Any]) -> SupportPrincipal:
        email = (claims.get("email") or "").lower()
        # Custom claim set via the Admin SDK is authoritative; the config
        # allowlist is the bootstrap path for the first admin.
        is_admin = bool(claims.get("admin", False)) or email in self._admin_emails
        return SupportPrincipal(
            user_id=claims["sub"],
            email=email,
            display_name=claims.get("name"),
            is_admin=is_admin,
            hosted_domain=claims.get("hd"),
        )


def seconds_until_expiry(claims: dict[str, Any]) -> int:
    return max(0, int(claims.get("exp", 0)) - int(time.time()))
