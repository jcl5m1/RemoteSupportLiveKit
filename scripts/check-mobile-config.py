#!/usr/bin/env python3
"""Validate mobile platform configuration and report missing production artifacts.

Checks Android/iOS bundle identifiers, universal-link domains, entitlements, and
Firebase config files. Does not modify files.

Example:
    python scripts/check-mobile-config.py --bundle-id com.mycompany.remote.support
"""

from __future__ import annotations

import argparse
import pathlib
import re
import sys


PROJECT_ROOT = pathlib.Path(__file__).resolve().parent.parent

ANDROID_BUILD_GRADLE = PROJECT_ROOT / "mobile/android/app/build.gradle"
ANDROID_MANIFEST = PROJECT_ROOT / "mobile/android/app/src/main/AndroidManifest.xml"
ANDROID_GOOGLE_SERVICES = PROJECT_ROOT / "mobile/android/app/google-services.json"
IOS_PBXPROJ = PROJECT_ROOT / "mobile/ios/Runner.xcodeproj/project.pbxproj"
IOS_ENTITLEMENTS = PROJECT_ROOT / "mobile/ios/Runner/Runner.entitlements"
IOS_GOOGLE_SERVICES = PROJECT_ROOT / "mobile/ios/Runner/GoogleService-Info.plist"
IOS_INFO_PLIST = PROJECT_ROOT / "mobile/ios/Runner/Info.plist"


PLACEHOLDER_PATTERNS = [
    re.compile(r"example\.com", re.IGNORECASE),
    re.compile(r"com\.example", re.IGNORECASE),
    re.compile(r"your-", re.IGNORECASE),
]


def _looks_placeholder(value: str) -> bool:
    return any(p.search(value) for p in PLACEHOLDER_PATTERNS)


def _read(path: pathlib.Path) -> str:
    if not path.exists():
        return ""
    return path.read_text(encoding="utf-8")


def _check_android(bundle_id: str | None, domain: str | None) -> list[str]:
    issues: list[str] = []
    gradle = _read(ANDROID_BUILD_GRADLE)
    manifest = _read(ANDROID_MANIFEST)

    ns_match = re.search(r'namespace\s*=\s*"([^"]+)"', gradle)
    app_id_match = re.search(r'applicationId\s*=\s*"([^"]+)"', gradle)
    namespace = ns_match.group(1) if ns_match else None
    application_id = app_id_match.group(1) if app_id_match else None

    if not namespace:
        issues.append("Android: namespace not found in app/build.gradle")
    elif _looks_placeholder(namespace):
        issues.append(f"Android: namespace '{namespace}' looks like a placeholder")

    if not application_id:
        issues.append("Android: applicationId not found in app/build.gradle")
    elif _looks_placeholder(application_id):
        issues.append(f"Android: applicationId '{application_id}' looks like a placeholder")

    if bundle_id and application_id and application_id != bundle_id:
        issues.append(f"Android: applicationId '{application_id}' does not match --bundle-id '{bundle_id}'")

    hosts = re.findall(r'android:host="([^"]+)"', manifest)
    if not hosts:
        issues.append("Android: no android:host entries found in AndroidManifest.xml")
    for host in hosts:
        if _looks_placeholder(host):
            issues.append(f"Android: host '{host}' looks like a placeholder")
        if domain and host != domain:
            issues.append(f"Android: host '{host}' does not match --universal-link-domain '{domain}'")

    if not ANDROID_GOOGLE_SERVICES.exists():
        issues.append("Android: google-services.json missing (drop from Firebase Console)")

    return issues


def _check_ios(bundle_id: str | None, domain: str | None) -> list[str]:
    issues: list[str] = []
    pbxproj = _read(IOS_PBXPROJ)
    entitlements = _read(IOS_ENTITLEMENTS)
    info_plist = _read(IOS_INFO_PLIST)

    bundle_ids = set(re.findall(r'PRODUCT_BUNDLE_IDENTIFIER\s*=\s*([^;]+);', pbxproj))
    if not bundle_ids:
        issues.append("iOS: PRODUCT_BUNDLE_IDENTIFIER not found in project.pbxproj")
    else:
        for bid in bundle_ids:
            bid = bid.strip()
            if _looks_placeholder(bid):
                issues.append(f"iOS: bundle id '{bid}' looks like a placeholder")
            if bundle_id and not bid.startswith(bundle_id):
                issues.append(f"iOS: bundle id '{bid}' does not start with --bundle-id '{bundle_id}'")

    applinks = re.findall(r'<string>applinks:([^<]+)</string>', entitlements)
    if not applinks:
        issues.append("iOS: no applinks entry found in Runner.entitlements")
    for link in applinks:
        if _looks_placeholder(link):
            issues.append(f"iOS: applinks domain '{link}' looks like a placeholder")
        if domain and link != domain:
            issues.append(f"iOS: applinks domain '{link}' does not match --universal-link-domain '{domain}'")

    if not IOS_GOOGLE_SERVICES.exists():
        issues.append("iOS: GoogleService-Info.plist missing (drop from Firebase Console)")

    if info_plist and "GoogleService-Info" not in info_plist and not IOS_GOOGLE_SERVICES.exists():
        # The Firebase Flutter plugin reads GoogleService-Info.plist by name;
        # no Info.plist reference is required, but warn if neither exists.
        pass

    return issues


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Validate mobile platform configuration."
    )
    parser.add_argument(
        "--bundle-id",
        help="Expected production bundle id (e.g. com.mycompany.remote.support)",
    )
    parser.add_argument(
        "--universal-link-domain",
        help="Expected universal link domain (e.g. support.mycompany.com)",
    )
    args = parser.parse_args(argv)

    issues: list[str] = []
    issues.extend(_check_android(args.bundle_id, args.universal_link_domain))
    issues.extend(_check_ios(args.bundle_id, args.universal_link_domain))

    print("Mobile platform configuration check")
    if args.bundle_id:
        print(f"  expected bundle id:   {args.bundle_id}")
    if args.universal_link_domain:
        print(f"  expected domain:      {args.universal_link_domain}")

    if issues:
        print(f"\nFound {len(issues)} issue(s):")
        for issue in issues:
            print(f"  - {issue}")
        print("\nFix the issues above, then re-run this script.")
        return 1

    print("\nMobile configuration looks complete.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
