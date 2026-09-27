# SPDX-License-Identifier: AGPL-3.0-only
"""Real SDK rate-limit dataclasses, fake transport, isolated memory/account data."""
from datetime import datetime, timedelta, timezone
import pytest
from claude_agent_sdk import RateLimitEvent, RateLimitInfo
from doxa.claude_quota import BillingQuota, cached_quota, sdk_limit
from doxa.identity import Usage, UsageLimit


def report(window, utilization, status="allowed"):
    return RateLimitEvent(RateLimitInfo(status=status, rate_limit_type=window,
                         utilization=utilization, resets_at=1900000000,
                         raw={"secret": "must never cross the boundary"}), "event", "session")


def test_real_sdk_windows_merge_with_stale_cache_without_inventing_missing_values():
    usage=Usage(UsageLimit("session",9,"normal",""),UsageLimit("weekly_all",48,"normal",""),
                None,"",datetime.now(timezone.utc)-timedelta(hours=7))
    state=BillingQuota()
    assert state.refresh({"mode":"subscription","type":"max 20x","quota":usage.chip(),**cached_quota(usage)})["quota"]=="5h:9% week:48%~"
    partial=state.update(sdk_limit(report("five_hour",.23,"allowed_warning")))
    assert partial["quota"]=="5h:23% week:48%~" and partial["quota_source"]=="mixed"
    complete=state.update(sdk_limit(report("seven_day",.61)))
    assert complete["quota"]=="5h:23% week:61%" and complete["quota_stale"] is False
    assert complete["quota_limits"]["five_hour"]["status"]=="allowed_warning"
    assert "secret" not in str(complete)
    assert state.update(sdk_limit(report("five_hour",None,"rejected")))["quota"]=="5h:23% week:61%"
    # Older cache re-read at turn_done must never regress a reported SDK window.
    assert state.refresh({"mode":"subscription","type":"max 20x","quota":usage.chip(),**cached_quota(usage)})["quota"]=="5h:23% week:61%"
    assert BillingQuota().update(sdk_limit(report("five_hour",.23))) is None


def test_missing_utilization_stays_unknown_and_malformed_windows_are_refused():
    state=BillingQuota(); state.refresh({"mode":"subscription","type":"pro","quota":None,**cached_quota(None)})
    result=state.update(sdk_limit(report("five_hour",None,"rejected")))
    assert result["quota"] is None and "percent" not in result["quota_limits"]["five_hour"]
    for window,value in [("five_hour",float("nan")),("seven_day",float("inf")),("five_hour",1.01),("unknown",.25)]:
        assert sdk_limit(report(window,value)) is None
    assert sdk_limit(report("five_hour",True)) == {"window":"five_hour","status":"allowed","resets_at":1900000000}


@pytest.mark.asyncio
async def test_canonical_engine_forwards_actual_sdk_rate_limit_event(tmp_path):
    from doxa.engine import SessionEngine
    from tests.fakes import factory_with_script
    factory,_=factory_with_script([report("five_hour",.17),report("seven_day",.66)])
    engine=SessionEngine(cwd=str(tmp_path),client_factory=factory,lore=False,peer_presence=False)
    await engine.start()
    try:
        events=[event async for event in engine.send("isolated scripted turn")]
        limits=[event.data for event in events if event.type=="rate_limit"]
        assert limits==[{"window":"five_hour","status":"allowed","percent":17,"resets_at":1900000000},
                        {"window":"seven_day","status":"allowed","percent":66,"resets_at":1900000000}]
        assert "secret" not in str(limits)
    finally:
        await engine.finalize()
