#!/usr/bin/env python3
"""Structured index for the immutable evidence supplied to one AI review."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from weather_data_store import iso_utc


TIMELINE_VERSION = "weather_evidence_timeline_v1"


class WeatherEvidenceTimeline:
    """Index current evidence while the daily Markdown retains full history."""

    @staticmethod
    def build(
        context: dict[str, Any],
        universe: list[dict[str, Any]],
        as_of: datetime,
        *,
        document_path: str | None,
        document_hash: str | None,
    ) -> dict[str, Any]:
        metar = context.get("metar") or {}
        process = context.get("weatherProcess") or {}
        models = context.get("modelUpdates") or {}
        markets = {row.get("marketId"): row for row in context.get("markets") or []}
        return {
            "version": TIMELINE_VERSION,
            "knownThroughUtc": iso_utc(as_of),
            "chronological": True,
            "immutableForThisReview": True,
            "fullHistory": {
                "format": "MARKDOWN",
                "includedAfterStructuredInput": True,
                "documentPath": document_path,
                "documentHash": document_hash,
            },
            "latestWeather": {
                "previousMetar": metar.get("previous"),
                "currentMetar": metar.get("current"),
                "processSnapshotUtc": process.get("snapshotSlotUtc"),
                "detectedProcesses": process.get("detectedProcesses") or [],
                "meteoblueVersion": (models.get("meteoblue") or {}).get("modelVersion"),
                "ecmwfVersion": (models.get("ecmwf") or {}).get("modelVersion"),
            },
            "marketReactions": [
                {
                    "marketId": row.get("marketId"),
                    "bucketC": row.get("bucketC"),
                    "snapshotUtc": (markets.get(row.get("marketId")) or {}).get("snapshotUtc"),
                    "previousNoBestAsk": (markets.get(row.get("marketId")) or {}).get("previousNoBestAsk"),
                    "currentNoBestAsk": (markets.get(row.get("marketId")) or {}).get("noBestAsk"),
                    "noAllInCostPerShare5": row.get("noAllInCostPerShare5"),
                }
                for row in universe
            ],
            "dataQuality": context.get("metarCoverage") or {},
        }
