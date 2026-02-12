#!/usr/bin/env python3
"""
merge_config.py  –  DPMP config migration helper

Merges new default fields from config_v2_example.json into an existing
user config_v2.json WITHOUT overwriting any values the user has already set.

Usage (called from entrypoint.sh):
    python3 /app/dpmp/merge_config.py \
        /app/dpmp/config_v2_example.json \
        /data/config_v2.json

How it works:
    1. Reads the "template" (example) and the user's existing config.
    2. Walks every key in the template.
       - If the key is MISSING from the user config, insert it with the
         template's default value.
       - If both sides are dicts, recurse (so nested sections like
         "scheduler", "pools.poolA", etc. are handled correctly).
       - If the key already EXISTS in the user config, leave it alone –
         even if the value differs from the template.
    3. Writes the merged result back to the user config path.
    4. Prints a summary of what was added (if anything) so the container
       logs show exactly what changed.

This script is safe to run on every container start – if nothing is
missing it simply exits with no changes.
"""

import json
import sys
import os
import shutil
from datetime import datetime, timezone


def deep_merge(template: dict, user: dict, path: str = "") -> list[str]:
    """
    Recursively add missing keys from 'template' into 'user'.

    Returns a list of human-readable strings describing each key that
    was added, e.g. "scheduler.auto_balance = false".
    """
    added = []

    for key, default_value in template.items():
        full_key = f"{path}.{key}" if path else key

        if key not in user:
            # ---- Key is missing from user config: insert default ----
            user[key] = default_value
            # Format the value for the log message
            if isinstance(default_value, dict):
                added.append(f"  + {full_key} = {{...}}  (new section)")
            else:
                added.append(f"  + {full_key} = {json.dumps(default_value)}")

        elif isinstance(default_value, dict) and isinstance(user[key], dict):
            # ---- Both sides are dicts: recurse into the section ----
            added.extend(deep_merge(default_value, user[key], full_key))

        # else: key exists and is not a nested dict — leave user's value alone

    return added


def main():
    if len(sys.argv) != 3:
        print(f"Usage: {sys.argv[0]} <template.json> <user_config.json>",
              file=sys.stderr)
        sys.exit(1)

    template_path = sys.argv[1]
    user_path = sys.argv[2]

    # --- Load template (example config shipped with the image) ---
    if not os.path.isfile(template_path):
        print(f"[merge_config] ERROR: template not found: {template_path}",
              file=sys.stderr)
        sys.exit(1)

    with open(template_path, "r", encoding="utf-8") as f:
        template = json.load(f)

    # --- Load user config ---
    if not os.path.isfile(user_path):
        print(f"[merge_config] No user config at {user_path}, skipping merge.")
        sys.exit(0)

    with open(user_path, "r", encoding="utf-8") as f:
        user_cfg = json.load(f)

    # --- Merge ---
    added = deep_merge(template, user_cfg)

    if not added:
        print("[merge_config] Config is up to date — no new fields needed.")
        sys.exit(0)

    # --- Backup the original before writing ---
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backup_path = f"{user_path}.backup.{timestamp}"
    shutil.copy2(user_path, backup_path)
    print(f"[merge_config] Backup saved: {backup_path}")

    # --- Write merged config ---
    with open(user_path, "w", encoding="utf-8") as f:
        json.dump(user_cfg, f, indent=2, ensure_ascii=False)
        f.write("\n")

    print(f"[merge_config] Added {len(added)} new field(s) to {user_path}:")
    for line in added:
        print(line)


if __name__ == "__main__":
    main()
