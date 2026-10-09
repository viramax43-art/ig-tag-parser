"""Small command-line control plane for the durable scheduler settings."""
from __future__ import annotations

import argparse
import json

from paths import DATA_DIR

CONFIG_FILE = DATA_DIR / "settings.json"
DEFAULTS = {
    "max_accounts": 5,
    "retry_unfinished": False,
    "only_ru": True,
    "report_every_accounts": 10,
    "export_interval_sec": 15,
}


def load_settings() -> dict:
    if not CONFIG_FILE.exists():
        return dict(DEFAULTS)
    try:
        data = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        data = {}
    out = dict(DEFAULTS)
    out.update(data)
    out["max_accounts"] = max(1, int(out["max_accounts"]))
    out["report_every_accounts"] = max(1, int(out.get("report_every_accounts", 10)))
    out["export_interval_sec"] = max(5, int(out.get("export_interval_sec", 10)))
    return out


def save_settings(data: dict) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    tmp = CONFIG_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(CONFIG_FILE)


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--max-accounts", type=int)
    p.add_argument("--show", action="store_true")
    args = p.parse_args()
    settings = load_settings()
    if args.max_accounts is not None:
        settings["max_accounts"] = max(1, args.max_accounts)
        save_settings(settings)
    if args.show or args.max_accounts is None:
        print(json.dumps(settings, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
