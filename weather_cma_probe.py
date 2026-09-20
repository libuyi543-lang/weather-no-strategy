#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen


ROOT = Path(__file__).resolve().parent
UTC = timezone.utc
PRODUCTS = {
    "cma_meso": {
        "catalog_code": "interface_mapping_required",
        "catalog_url": "https://data.cma.cn/",
    },
    "automatic_station": {
        "catalog_code": "surface_observation",
        "catalog_url": "https://data.cma.cn/data/cdcindex/cid/0b9164954813c573.html",
    },
    "radar_composite_reflectivity": {
        "catalog_code": "J.0019.0010.S001",
        "catalog_url": "https://data.cma.cn/data/detail/dataCode/J.0019.0010.S001.html",
    },
    "radar_vil": {
        "catalog_code": "J.0019.0010.S002",
        "catalog_url": "https://data.cma.cn/data/detail/dataCode/J.0019.0010.S002.html",
    },
    "radar_echo_top": {
        "catalog_code": "J.0017.0003.S002",
        "catalog_url": "https://data.cma.cn/data/detail/dataCode/J.0017.0003.S002.html",
    },
    "lightning": {
        "catalog_code": "M.0001.0047.S001",
        "catalog_url": "https://data.cma.cn/data/detail/dataCode/M.0001.0047.S001.html",
    },
    "surface_solar_radiation": {
        "catalog_code": "SK.0143.001",
        "catalog_url": "https://data.cma.cn/data/detail/dataCode/SK.0143.001.html",
    },
}


def iso_utc() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def load_credentials(path_text: str) -> dict[str, Any] | None:
    path = Path(os.path.expanduser(path_text))
    if not path.exists():
        return None
    if path.stat().st_mode & 0o077:
        raise RuntimeError(f"CMA credential file must have mode 600: {path}")
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise RuntimeError("CMA credential file must contain a JSON object")
    return payload


def probe_api(
    base_url: str, credentials: dict[str, Any], product: str, timeout: float
) -> tuple[str, float | None, str]:
    mappings = credentials.get("products") if isinstance(credentials.get("products"), dict) else {}
    mapping = mappings.get(product) if isinstance(mappings.get(product), dict) else None
    if not mapping or not mapping.get("interfaceId"):
        return "unsupported", None, "credential file has no tested interfaceId mapping for this product"
    params = dict(mapping.get("params") or {})
    params.update(
        {
            "userId": credentials.get("userId"),
            "pwd": credentials.get("pwd"),
            "dataFormat": params.get("dataFormat", "json"),
            "interfaceId": mapping["interfaceId"],
        }
    )
    started = time.monotonic()
    request = Request(f"{base_url}?{urlencode(params)}", headers={"User-Agent": "weather-market-monitor/1.0"})
    try:
        with urlopen(request, timeout=timeout) as response:
            body = response.read(4096).decode("utf-8", errors="replace")
    except HTTPError as exc:
        status = "auth_required" if exc.code in (401, 403) else "error"
        return status, round((time.monotonic() - started) * 1000, 1), f"HTTP {exc.code}"
    except (URLError, TimeoutError, OSError) as exc:
        return "error", round((time.monotonic() - started) * 1000, 1), str(exc)[:500]
    latency = round((time.monotonic() - started) * 1000, 1)
    lowered = body.casefold()
    if any(token in lowered for token in ("invalid user", "password", "unauthorized", "权限", "认证")):
        return "auth_required", latency, body[:500]
    try:
        payload = json.loads(body)
    except json.JSONDecodeError:
        return "error", latency, body[:500]
    code = str(payload.get("returnCode") or payload.get("code") or "") if isinstance(payload, dict) else ""
    if code and code not in ("0", "200", "200.0"):
        return "error", latency, json.dumps(payload, ensure_ascii=False)[:500]
    return "available", latency, "authenticated API returned structured data"


def run_probe(config: dict[str, Any]) -> dict[str, Any]:
    cma = config.get("cma") if isinstance(config.get("cma"), dict) else {}
    credential_path = str(cma.get("credentialFile", "~/.config/weather-market-monitor/cma_api.json"))
    credentials = load_credentials(credential_path)
    checked_at = iso_utc()
    rows = []
    for product, definition in PRODUCTS.items():
        if credentials is None:
            status, latency, detail = (
                "auth_required",
                None,
                "CMA catalog is reachable; automated API access requires an approved account and product interface mapping",
            )
        else:
            status, latency, detail = probe_api(
                str(cma.get("apiBaseUrl", "http://api.data.cma.cn:8090/api")),
                credentials,
                product,
                float(cma.get("timeoutSeconds", 20)),
            )
        rows.append(
            {
                "source": "cma",
                "product": product,
                "catalog_code": definition["catalog_code"],
                "status": status,
                "endpoint": definition["catalog_url"],
                "latency_ms": latency,
                "detail": detail,
            }
        )

    db = sqlite3.connect(ROOT / config["databasePath"], timeout=60)
    try:
        db.execute("PRAGMA busy_timeout=60000")
        db.execute(
            """
            CREATE TABLE IF NOT EXISTS source_access_probes (
                source TEXT NOT NULL,product TEXT NOT NULL,checked_at_utc TEXT NOT NULL,
                status TEXT NOT NULL,endpoint TEXT,latency_ms REAL,detail TEXT,
                PRIMARY KEY(source,product,checked_at_utc)
            )
            """
        )
        db.executemany(
            """
            INSERT OR REPLACE INTO source_access_probes(
                source,product,checked_at_utc,status,endpoint,latency_ms,detail
            ) VALUES(?,?,?,?,?,?,?)
            """,
            [
                (
                    row["source"], row["product"], checked_at, row["status"],
                    row["endpoint"], row["latency_ms"], row["detail"],
                )
                for row in rows
            ],
        )
        db.commit()
    finally:
        db.close()
    result = {
        "checked_at_utc": checked_at,
        "credential_file": credential_path,
        "credentials_configured": credentials is not None,
        "products": rows,
    }
    report = ROOT / config["reportDirectory"] / "cma_access_status_latest.json"
    report.parent.mkdir(parents=True, exist_ok=True)
    report.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description="Probe configured CMA weather data interfaces")
    parser.add_argument("--once", action="store_true")
    parser.parse_args()
    config = json.loads((ROOT / "monitor_config.json").read_text(encoding="utf-8"))
    result = run_probe(config)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
