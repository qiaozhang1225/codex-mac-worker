#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

from duomac_contracts import ContractError, parse_issue_body, render_issue_body
from duomac_github import GhClient, GhError, IssueRef


def main() -> int:
    parser = argparse.ArgumentParser(description="Preview or create a dual-Mac task Issue")
    parser.add_argument("--repo", required=True)
    parser.add_argument("--spec", required=True, type=Path)
    parser.add_argument("--yes", action="store_true")
    args = parser.parse_args()

    try:
        spec = parse_issue_body(args.spec.read_text(encoding="utf-8"))
        contract = render_issue_body(spec)
        if not args.yes:
            print(
                json.dumps(
                    {
                        "created": False,
                        "title": spec.objective,
                        "contract": contract,
                    },
                    ensure_ascii=False,
                )
            )
            return 0
        client = GhClient()
        issue_url = client.create_issue(args.repo, spec.objective, contract)
        ref = IssueRef.parse(issue_url)
        client.set_state_label(ref, "duomac:ready")
    except (ContractError, GhError, OSError) as exc:
        print(str(exc), file=sys.stderr)
        return 1

    print(json.dumps({"created": True, "issue_url": issue_url}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

