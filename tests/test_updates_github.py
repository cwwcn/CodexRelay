from __future__ import annotations

from typing import Any

import httpx

from codexrelay.updates import GitHubReleaseUpdateProvider


class FakeResponse:
    def __init__(self, payload: Any) -> None:
        self.payload = payload

    def raise_for_status(self) -> None:
        return

    def json(self) -> Any:
        return self.payload


def test_provider_skips_drafts_and_prereleases(monkeypatch: Any) -> None:
    payload = [
        {"draft": True, "tag_name": "v9.0.0"},
        {"prerelease": True, "tag_name": "v8.0.0"},
        {"tag_name": "v0.2.0", "html_url": "https://example.test/release"},
    ]
    monkeypatch.setattr(httpx, "get", lambda *args, **kwargs: FakeResponse(payload))

    state = GitHubReleaseUpdateProvider().check_for_updates()

    assert state.available_version == "0.2.0"
    assert state.release_url == "https://example.test/release"
    assert "发现新版本" in state.message


def test_provider_does_not_offer_prerelease_when_no_formal_release_exists(
    monkeypatch: Any,
) -> None:
    payload = [
        {
            "prerelease": True,
            "tag_name": "v0.2.0",
            "html_url": "https://example.test/prerelease",
        }
    ]
    monkeypatch.setattr(httpx, "get", lambda *args, **kwargs: FakeResponse(payload))

    state = GitHubReleaseUpdateProvider().check_for_updates()

    assert state.available_version is None
    assert "暂未找到公开发行版" in state.message


def test_provider_reports_latest_when_release_is_not_newer(monkeypatch: Any) -> None:
    payload = [{"tag_name": "v0.1.0", "html_url": "https://example.test/current"}]
    monkeypatch.setattr(httpx, "get", lambda *args, **kwargs: FakeResponse(payload))

    state = GitHubReleaseUpdateProvider().check_for_updates()

    assert state.available_version is None
    assert "最新版本" in state.message
