"""
One-time export of config.json with db_connection decrypted, for copying
credentials to a new machine by hand. Re-run after any config change.

The app itself never reads config_nonencrypted.json - config.json (DPAPI
encrypted) is the only file the running app uses. This is gitignored.

Sample: python export_config_plain.py
"""

import json
import os

import config_store

OUT_FILE = os.path.join(config_store.BASE_DIR, "config_nonencrypted.json")

if __name__ == "__main__":
    config = config_store.load_config()
    with open(OUT_FILE, "w", encoding="utf-8") as f:
        json.dump(config, f, indent=4, ensure_ascii=False)
    print(f"Wrote {OUT_FILE}")
