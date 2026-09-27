# SPDX-License-Identifier: AGPL-3.0-only
"""Bounded subscription utilization from Claude's SDK events or local cache."""
import math

WINDOWS = ("five_hour", "seven_day", "seven_day_opus", "seven_day_sonnet")
STATUSES = ("allowed", "allowed_warning", "rejected")


def sdk_limit(message):
    """Project only reported numeric/status fields; never copy the raw payload."""
    info = getattr(message, "rate_limit_info", None)
    window = getattr(info, "rate_limit_type", None)
    status = getattr(info, "status", None)
    if window not in WINDOWS or status not in STATUSES:
        return None
    result = {"window": window, "status": status}
    utilization = getattr(info, "utilization", None)
    if isinstance(utilization, (int, float)) and not isinstance(utilization, bool):
        if not math.isfinite(utilization) or not 0 <= utilization <= 1:
            return None
        result["percent"] = int(round(utilization * 100))
    resets = getattr(info, "resets_at", None)
    if isinstance(resets, int) and not isinstance(resets, bool) and 0 <= resets <= 253402300799:
        result["resets_at"] = resets
    return result


def quota_text(limits):
    labels = {"five_hour": "5h", "seven_day": "week", "seven_day_opus": "opus", "seven_day_sonnet": "sonnet"}
    rows = [(labels[window], limits[window]) for window in WINDOWS
            if isinstance(limits.get(window, {}).get("percent"), int)]
    if not rows:
        return None
    stale = any(row.get("stale", False) for _, row in rows)
    return " ".join(f"{label}:{row['percent']}%" for label, row in rows) + ("~" if stale else "")


def cached_quota(usage):
    limits = {}
    stale = usage.is_stale() if usage else False
    entries = [("five_hour", getattr(usage, "session", None)),
               ("seven_day", getattr(usage, "weekly", None))]
    label = getattr(usage, "scope_label", "").lower()
    if "opus" in label:
        entries.append(("seven_day_opus", getattr(usage, "scoped", None)))
    elif "sonnet" in label:
        entries.append(("seven_day_sonnet", getattr(usage, "scoped", None)))
    for window, entry in entries:
        percent = getattr(entry, "percent", None)
        if isinstance(percent, int) and not isinstance(percent, bool) and 0 <= percent <= 100:
            limits[window] = {"percent": percent, "stale": stale, "source": "claude_cli_cache"}
    return {"quota": quota_text(limits), "quota_limits": limits, "quota_source": "claude_cli_cache", "quota_stale": stale}


class BillingQuota:
    """Current session's SDK reports override matching cached window values."""
    def __init__(self):
        self.billing = None
        self.reported = {}

    def refresh(self, billing):
        self.billing = billing
        return self.snapshot()

    def update(self, limit):
        if not isinstance(limit, dict) or limit.get("window") not in WINDOWS:
            return None
        self.reported.setdefault(limit["window"], {}).update(
            {key: value for key, value in limit.items() if key != "window"})
        return self.snapshot()

    def snapshot(self):
        if not self.billing:
            return None  # API auth never acquires a subscription claim from a usage event.
        result = dict(self.billing)
        limits = dict(result.get("quota_limits") or {})
        for window, value in self.reported.items():
            prior = dict(limits.get(window) or {})
            prior.update(value)
            if "percent" in value:
                prior.update(source="sdk", stale=False)
            limits[window] = prior
        if self.reported:
            rows = [(window, limits[window]) for window in WINDOWS
                    if isinstance(limits.get(window, {}).get("percent"), int)]
            stale = any(row.get("stale", False) for _, row in rows)
            result.update(quota=quota_text(limits),
                          quota_limits=limits, quota_source="sdk" if all(row.get("source") == "sdk" for _, row in rows) else "mixed",
                          quota_stale=stale)
        return result
