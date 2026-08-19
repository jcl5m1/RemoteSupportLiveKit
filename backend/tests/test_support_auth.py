"""Authorization gate for the support role.

The signature-verification path is PyJWT's and is not re-tested here. What IS
tested is our own allowlist logic -- the thing standing between "any Google
account on earth" and the trusted support role.
"""

import pytest

from app.core.firebase_auth import AuthError, FirebaseTokenVerifier

PROJECT = "hermes-458420"


def verifier(**kw) -> FirebaseTokenVerifier:
    kw.setdefault("allowed_domains", frozenset({"lgitech.net"}))
    return FirebaseTokenVerifier(project_id=PROJECT, **kw)


def claims(**over):
    base = {
        "sub": "uid-123",
        "email": "dana@lgitech.net",
        "email_verified": True,
        "name": "Dana",
    }
    base.update(over)
    return base


# --- the gate ----------------------------------------------------------


def test_allows_workspace_domain():
    verifier()._authorize(claims())


def test_rejects_outside_domain():
    with pytest.raises(AuthError, match="domain_not_allowed"):
        verifier()._authorize(claims(email="randomer@gmail.com"))


def test_rejects_unverified_email():
    """An unverified email is trivially spoofable, so domain proves nothing."""
    with pytest.raises(AuthError, match="email_not_verified"):
        verifier()._authorize(claims(email_verified=False))


def test_rejects_missing_email():
    with pytest.raises(AuthError, match="missing_email"):
        verifier()._authorize(claims(email=""))


def test_rejects_missing_subject():
    with pytest.raises(AuthError, match="missing_subject"):
        verifier()._authorize(claims(sub=""))


def test_fails_closed_with_no_allowlist():
    """The most important test here.

    A misconfigured deployment must refuse everyone rather than admit
    everyone. An empty allowlist is a config error, not "allow all".
    """
    v = FirebaseTokenVerifier(project_id=PROJECT)
    with pytest.raises(AuthError, match="no_allowlist_configured"):
        v._authorize(claims())


def test_explicit_email_allowlist_bypasses_domain():
    v = FirebaseTokenVerifier(
        project_id=PROJECT,
        allowed_emails=frozenset({"contractor@gmail.com"}),
    )
    v._authorize(claims(email="contractor@gmail.com"))


def test_domain_match_is_exact_not_suffix():
    """`evil-lgitech.net` and `lgitech.net.evil.com` must not pass."""
    for bad in ("x@evil-lgitech.net", "x@lgitech.net.evil.com", "x@sub.lgitech.net"):
        with pytest.raises(AuthError, match="domain_not_allowed"):
            verifier()._authorize(claims(email=bad))


def test_email_comparison_is_case_insensitive():
    verifier()._authorize(claims(email="Dana@LGITech.net"))


# --- principal mapping -------------------------------------------------


def test_admin_from_custom_claim():
    p = verifier()._to_principal(claims(admin=True))
    assert p.is_admin is True


def test_admin_from_bootstrap_allowlist():
    v = verifier(admin_emails=frozenset({"johnny@lgitech.net"}))
    assert v._to_principal(claims(email="johnny@lgitech.net")).is_admin is True


def test_non_admin_by_default():
    assert verifier()._to_principal(claims()).is_admin is False


def test_livekit_identity_prefix():
    """Identity prefix is how the agent tells roles apart (docs/02)."""
    p = verifier()._to_principal(claims())
    assert p.livekit_identity == "support-uid-123"
    assert p.livekit_identity.startswith("support-")
