from __future__ import annotations

import hashlib
from contextlib import contextmanager
from pathlib import Path
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


def test_provider_detects_same_version_rebuilt_asset(monkeypatch: Any) -> None:
    payload = [
        {
            "tag_name": "v0.1.1",
            "html_url": "https://example.test/release",
            "assets": [
                {
                    "name": "CodexRelay-macos-arm64-v0.1.1.dmg",
                    "browser_download_url": "https://example.test/fixed.dmg",
                    "digest": "sha256:" + "c" * 64,
                    "updated_at": "2026-09-02T09:52:24Z",
                }
            ],
        }
    ]
    monkeypatch.setattr(httpx, "get", lambda *args, **kwargs: FakeResponse(payload))
    monkeypatch.setattr("codexrelay.updates.github.__build_time__", "2026-09-02 09:10 UTC")

    state = GitHubReleaseUpdateProvider().check_for_updates()

    assert state.available_version == "0.1.1"
    assert state.asset_name == "CodexRelay-macos-arm64-v0.1.1.dmg"
    assert "修复版" in state.message


def test_provider_does_not_detect_older_same_version_asset(monkeypatch: Any) -> None:
    payload = [
        {
            "tag_name": "v0.1.1",
            "assets": [
                {
                    "name": "CodexRelay-macos-arm64-v0.1.1.dmg",
                    "browser_download_url": "https://example.test/old.dmg",
                    "digest": "sha256:" + "d" * 64,
                    "updated_at": "2026-09-02T09:00:00Z",
                }
            ],
        }
    ]
    monkeypatch.setattr(httpx, "get", lambda *args, **kwargs: FakeResponse(payload))
    monkeypatch.setattr("codexrelay.updates.github.__build_time__", "2026-09-02 09:10 UTC")

    state = GitHubReleaseUpdateProvider().check_for_updates()

    assert state.available_version is None
    assert "最新版本" in state.message


def test_provider_selects_matching_architecture_asset(monkeypatch: Any, tmp_path: Path) -> None:
    payload = [
        {
            "tag_name": "v0.2.0",
            "html_url": "https://example.test/release",
            "assets": [
                {
                    "name": "CodexRelay-macos-x86_64-v0.2.0.dmg",
                    "browser_download_url": "https://example.test/intel.dmg",
                    "digest": "sha256:" + "a" * 64,
                },
                {
                    "name": "CodexRelay-macos-arm64-v0.2.0.dmg",
                    "browser_download_url": "https://example.test/arm.dmg",
                    "digest": "sha256:" + "b" * 64,
                },
            ],
        }
    ]
    monkeypatch.setattr(httpx, "get", lambda *args, **kwargs: FakeResponse(payload))

    state = GitHubReleaseUpdateProvider(
        architecture="x86_64", download_directory=tmp_path
    ).check_for_updates()

    assert state.available_version == "0.2.0"
    assert state.asset_name == "CodexRelay-macos-x86_64-v0.2.0.dmg"
    assert state.asset_url == "https://example.test/intel.dmg"
    assert state.asset_digest == "a" * 64


def test_provider_downloads_and_verifies_update(
    monkeypatch: Any, tmp_path: Path
) -> None:
    content = b"codexrelay update"
    digest = hashlib.sha256(content).hexdigest()
    payload = [
        {
            "tag_name": "v0.2.0",
            "assets": [
                {
                    "name": "CodexRelay-macos-arm64-v0.2.0.dmg",
                    "browser_download_url": "https://example.test/update.dmg",
                    "digest": f"sha256:{digest}",
                }
            ],
        }
    ]
    monkeypatch.setattr(httpx, "get", lambda *args, **kwargs: FakeResponse(payload))

    class FakeStreamResponse:
        def __init__(self) -> None:
            self.headers = {"Content-Length": str(len(content))}

        def __enter__(self) -> FakeStreamResponse:
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def raise_for_status(self) -> None:
            return None

        def iter_bytes(self) -> list[bytes]:
            return [content[:7], content[7:]]

    @contextmanager
    def fake_stream(*args: Any, **kwargs: Any) -> Any:
        yield FakeStreamResponse()

    monkeypatch.setattr(httpx, "stream", fake_stream)
    provider = GitHubReleaseUpdateProvider(download_directory=tmp_path)
    provider.check_for_updates()
    (tmp_path / "CodexRelay-macos-arm64-v0.2.0.dmg").write_bytes(b"stale download")

    state = provider.download_update()

    assert state.downloaded_path is not None
    assert Path(state.downloaded_path).read_bytes() == content
    assert state.downloaded_bytes == len(content)
    assert state.total_bytes == len(content)
    assert not state.downloading


def test_provider_refuses_unverifiable_update(monkeypatch: Any, tmp_path: Path) -> None:
    payload = [
        {
            "tag_name": "v0.2.0",
            "assets": [
                {
                    "name": "CodexRelay-macos-arm64-v0.2.0.dmg",
                    "browser_download_url": "https://example.test/update.dmg",
                }
            ],
        }
    ]
    monkeypatch.setattr(httpx, "get", lambda *args, **kwargs: FakeResponse(payload))
    provider = GitHubReleaseUpdateProvider(download_directory=tmp_path)
    provider.check_for_updates()

    state = provider.download_update()

    assert state.downloaded_path is None
    assert "未提供 SHA-256" in state.message
    assert not state.downloading
