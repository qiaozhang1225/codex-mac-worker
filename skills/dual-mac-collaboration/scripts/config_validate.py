#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

from duomac_contracts import ContractError
from duomac_scheduled import load_scheduled_config


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Validate Mac mini Scheduled repository configuration"
    )
    parser.add_argument("--config", required=True, type=Path)
    args = parser.parse_args()
    try:
        config = load_scheduled_config(args.config)
    except (ContractError, OSError) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    print(
        json.dumps(
            {
                "valid": True,
                "max_parallel_tasks": config.max_parallel_tasks,
                "poll_interval_minutes": config.poll_interval_minutes,
                "repositories": [
                    {"github": item.github, "local_path": str(item.local_path)}
                    for item in config.repositories
                ],
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
