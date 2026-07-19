#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

from duomac_contracts import ContractError, load_project_config, parse_issue_body, validate_task
from duomac_github import GhClient, GhError, IssueRef


def main() -> int:
    parser = argparse.ArgumentParser(description="Validate a dual-Mac task contract")
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--body-file", type=Path)
    source.add_argument("issue_url", nargs="?")
    parser.add_argument("--project-config", required=True, type=Path)
    args = parser.parse_args()

    try:
        if args.body_file is not None:
            body = args.body_file.read_text(encoding="utf-8")
        else:
            body = GhClient().issue_body(IssueRef.parse(args.issue_url))
        spec = parse_issue_body(body)
        validate_task(spec, load_project_config(args.project_config))
    except (ContractError, GhError, OSError) as exc:
        print(str(exc), file=sys.stderr)
        return 1

    print(
        json.dumps(
            {
                "valid": True,
                "revision": spec.revision,
                "delivery_mode": spec.delivery_mode,
                "verification_profile": spec.verification_profile,
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

