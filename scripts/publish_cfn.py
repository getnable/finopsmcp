#!/usr/bin/env python3
"""Publish the one-click read-only CloudFormation template to S3.

The AWS console's quick-create flow only loads templates from an S3 URL, so the
read-only-key template (the thing behind the one-click connect link) has to live
in a public S3 object. This script regenerates that template from the single
source of truth (_REQUIRED_ACTIONS in iam_setup.py), writes the committed copy in
web/cloudformation/ for auditing, uploads it to the bucket you name, and prints
the live quick-create URL.

Run once (and again whenever _REQUIRED_ACTIONS changes):

    python scripts/publish_cfn.py --bucket nable-public --key cloudformation/readonly-key.json

Then set the live URL so the wizard and site use it:

    export NABLE_CFN_TEMPLATE_URL="https://<bucket>.s3.amazonaws.com/<key>"

or update CFN_KEY_TEMPLATE_S3_URL's default in src/finops/security/iam_setup.py.

The bucket must allow public reads of this object (the console fetches it
unauthenticated). A single public object with a tight bucket policy is enough;
you do not need a fully public bucket.
"""
from __future__ import annotations

import argparse
import pathlib
import sys

REPO = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))

from finops.security.iam_setup import generate_cloudformation_key, quick_create_url  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description="Publish the read-only CFN template to S3.")
    ap.add_argument("--bucket", required=True, help="S3 bucket name")
    ap.add_argument("--key", default="cloudformation/readonly-key.json", help="S3 object key")
    ap.add_argument("--region", default="us-east-1", help="Region for the quick-create URL")
    ap.add_argument("--dry-run", action="store_true", help="Write the local copy, skip the upload")
    args = ap.parse_args()

    body = generate_cloudformation_key() + "\n"

    # Always refresh the committed, auditable copy so the repo never drifts.
    committed = REPO / "web" / "cloudformation" / "readonly-key.json"
    committed.parent.mkdir(parents=True, exist_ok=True)
    committed.write_text(body)
    print(f"wrote {committed} ({len(body)} bytes)")

    s3_url = f"https://{args.bucket}.s3.amazonaws.com/{args.key}"
    if args.dry_run:
        print("dry run: skipping upload")
    else:
        import boto3

        boto3.client("s3").put_object(
            Bucket=args.bucket,
            Key=args.key,
            Body=body.encode(),
            ContentType="application/json",
            CacheControl="public, max-age=300",
        )
        print(f"uploaded to {s3_url}")

    print()
    print("Set this so the wizard and site use the live template:")
    print(f'  export NABLE_CFN_TEMPLATE_URL="{s3_url}"')
    print()
    print("One-click URL (with that env set):")
    print(f"  {quick_create_url(region=args.region)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
