#!/usr/bin/env python3
"""Patch mobile platform manifests with production bundle id, app group, and domain.

This is a one-command helper to move the project from placeholder values
(`com.example.remote_support` / `support.example.com`) to production values
before building release binaries or configuring Firebase / Apple / Google app
links.

Example:
    python scripts/apply-mobile-config.py \
        --bundle-id com.mycompany.remote.support \
        --app-group group.com.mycompany.remote.support \
        --universal-link-domain support.mycompany.com
"""

from __future__ import annotations

import argparse
import pathlib
import re
import sys


PROJECT_ROOT = pathlib.Path(__file__).resolve().parent.parent

FILES = {
    "android_build_gradle": PROJECT_ROOT / "mobile/android/app/build.gradle",
    "android_manifest": PROJECT_ROOT / "mobile/android/app/src/main/AndroidManifest.xml",
    "ios_entitlements": PROJECT_ROOT / "mobile/ios/Runner/Runner.entitlements",
    "ios_pbxproj": PROJECT_ROOT / "mobile/ios/Runner.xcodeproj/project.pbxproj",
}

DEFAULT_BUNDLE_ID = "com.example.remote_support"
DEFAULT_IOS_BUNDLE_ID = "com.example.remoteSupport"
DEFAULT_DOMAIN = "support.example.com"


def _validate_bundle_id(bundle_id: str) -> str:
    # Android application ids allow underscores; iOS bundle IDs allow hyphens.
    if not re.fullmatch(r"[a-zA-Z0-9_]+(\.[a-zA-Z0-9_]+)*", bundle_id):
        raise argparse.ArgumentTypeError(
            "bundle id must be a reverse-DNS string of alphanumeric/underscore segments separated by dots"
        )
    return bundle_id


def _validate_domain(domain: str) -> str:
    if not re.fullmatch(r"[a-zA-Z0-9][-a-zA-Z0-9]*(\.[a-zA-Z0-9][-a-zA-Z0-9]*)*", domain):
        raise argparse.ArgumentTypeError("domain must be a valid hostname")
    return domain


def _validate_app_group(app_group: str | None) -> str | None:
    if app_group is None:
        return None
    if not re.fullmatch(r"group\.[a-zA-Z0-9_]+(\.[a-zA-Z0-9_]+)*", app_group):
        raise argparse.ArgumentTypeError(
            "app group must start with 'group.' followed by a reverse-DNS identifier"
        )
    return app_group


def _patch_android_build_gradle(content: str, bundle_id: str) -> str:
    # namespace = "com.example.remote_support"
    content = re.sub(
        r'namespace\s*=\s*"[^"]+"',
        f'namespace = "{bundle_id}"',
        content,
    )
    # applicationId = "com.example.remote_support"
    content = re.sub(
        r'applicationId\s*=\s*"[^"]+"',
        f'applicationId = "{bundle_id}"',
        content,
    )
    return content


def _patch_android_manifest(content: str, domain: str) -> str:
    # Only patch the Android App Links (https) host, leaving the custom
    # scheme host ("join") untouched.
    return re.sub(
        r'(<data android:scheme="https" android:host=")([^"]+)(" android:pathPrefix)',
        rf'\g<1>{domain}\g<3>',
        content,
    )


def _patch_ios_entitlements(content: str, domain: str, app_group: str | None) -> str:
    # Replace existing applinks entry.
    content = re.sub(
        r'<string>applinks:[^<]+</string>',
        f'<string>applinks:{domain}</string>',
        content,
    )

    if app_group:
        # Ensure a com.apple.security.application-groups key exists.
        if "com.apple.security.application-groups" not in content:
            # Insert right before closing </dict>.
            content = re.sub(
                r"(</dict>\s*</plist>)",
                f"\t<key>com.apple.security.application-groups</key>\n"
                f"\t<array>\n"
                f"\t\t<string>{app_group}</string>\n"
                f"\t</array>\n"
                r"\1",
                content,
            )
        else:
            content = re.sub(
                r"(<key>com\.apple\.security\.application-groups</key>\s*<array>\s*)(<string>[^<]+</string>)(\s*</array>)",
                rf"\1<string>{app_group}</string>\3",
                content,
                flags=re.DOTALL,
            )
    return content


def _patch_ios_pbxproj(content: str, bundle_id: str) -> str:
    # Main target identifiers are exactly the old placeholder. Test target uses
    # the placeholder plus ".RunnerTests"; preserve that suffix.
    def _replace_bundle_id(match: re.Match) -> str:
        key = match.group(1)
        value = match.group(2)
        if value.endswith(".RunnerTests"):
            new_value = f"{bundle_id}.RunnerTests"
        else:
            new_value = bundle_id
        return f"{key} = {new_value};"

    return re.sub(
        r'(PRODUCT_BUNDLE_IDENTIFIER)\s*=\s*([^;]+);',
        _replace_bundle_id,
        content,
    )


def _patch(path: pathlib.Path, new_content: str, dry_run: bool) -> bool:
    old_content = path.read_text(encoding="utf-8")
    if old_content == new_content:
        print(f"  unchanged: {path}")
        return False

    if dry_run:
        print(f"  would patch: {path}")
        return True

    path.write_text(new_content, encoding="utf-8")
    print(f"  patched: {path}")
    return True


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Apply production bundle id, app group, and universal link domain to mobile manifests."
    )
    parser.add_argument(
        "--bundle-id",
        required=True,
        type=_validate_bundle_id,
        help="Reverse-DNS bundle id (e.g. com.mycompany.remote.support)",
    )
    parser.add_argument(
        "--app-group",
        type=_validate_app_group,
        help="iOS app group identifier (e.g. group.com.mycompany.remote.support)",
    )
    parser.add_argument(
        "--universal-link-domain",
        required=True,
        type=_validate_domain,
        help="Domain for Apple Universal Links / Android App Links (e.g. support.mycompany.com)",
    )
    parser.add_argument(
        "--android-only",
        action="store_true",
        help="Patch only Android files (skip iOS entitlements and project.pbxproj)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print what would change without writing files",
    )
    args = parser.parse_args(argv)

    required_files = [
        "android_build_gradle",
        "android_manifest",
    ]
    if not args.android_only:
        required_files.extend(["ios_entitlements", "ios_pbxproj"])

    missing = [name for name in required_files if not FILES[name].exists()]
    if missing:
        print(f"Missing expected files: {missing}", file=sys.stderr)
        return 1

    print(
        f"Applying mobile config: bundle_id={args.bundle_id} "
        f"app_group={args.app_group or '(none)'} "
        f"domain={args.universal_link_domain} "
        f"platforms={'android' if args.android_only else 'android+ios'}"
    )
    if args.dry_run:
        print("(dry run — no files modified)")

    changed = False

    gradle = FILES["android_build_gradle"].read_text(encoding="utf-8")
    changed |= _patch(
        FILES["android_build_gradle"],
        _patch_android_build_gradle(gradle, args.bundle_id),
        args.dry_run,
    )

    manifest = FILES["android_manifest"].read_text(encoding="utf-8")
    changed |= _patch(
        FILES["android_manifest"],
        _patch_android_manifest(manifest, args.universal_link_domain),
        args.dry_run,
    )

    if not args.android_only:
        entitlements = FILES["ios_entitlements"].read_text(encoding="utf-8")
        changed |= _patch(
            FILES["ios_entitlements"],
            _patch_ios_entitlements(
                entitlements, args.universal_link_domain, args.app_group
            ),
            args.dry_run,
        )

        pbxproj = FILES["ios_pbxproj"].read_text(encoding="utf-8")
        changed |= _patch(
            FILES["ios_pbxproj"],
            _patch_ios_pbxproj(pbxproj, args.bundle_id),
            args.dry_run,
        )

    if not changed:
        print("No changes were necessary.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
