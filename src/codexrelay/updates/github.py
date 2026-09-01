from __future__ import annotations

import os
import re
from dataclasses import replace
from typing import Any

import httpx

from codexrelay.updates.base import UpdateState
from codexrelay.version import __version__


def _version_tuple(value: str) -> tuple[int, ...]:
    numbers = re.findall(r"\d+", value)
    return tuple(int(number) for number in numbers) or (0,)


class GitHubReleaseUpdateProvider:
    """Read-only GitHub Releases update provider.

    It deliberately never downloads or replaces the app. The first release
    phase only discovers a signed release and opens its official page; a
    Sparkle-backed installer can be plugged in later without changing the UI.
    """

    def __init__(
        self,
        *,
        owner: str | None = None,
        repository: str | None = None,
        timeout: float = 8.0,
    ) -> None:
        self.owner = owner or os.environ.get("CODEXRELAY_GITHUB_OWNER", "cwwcn")
        self.repository = repository or os.environ.get("CODEXRELAY_GITHUB_REPOSITORY", "CodexRelay")
        self.timeout = timeout
        self._state = UpdateState(
            enabled=True,
            message="尚未检查更新",
        )

    @property
    def state(self) -> UpdateState:
        return self._state

    @property
    def releases_url(self) -> str:
        return f"https://github.com/{self.owner}/{self.repository}/releases"

    def check_for_updates(self) -> UpdateState:
        self._state = replace(self._state, checking=True, message="正在检查 GitHub Releases…")
        try:
            payload = self._fetch_release()
            release = self._select_release(payload)
            if release is None:
                self._state = replace(
                    self._state,
                    checking=False,
                    message="暂未找到公开发行版",
                    available_version=None,
                    release_url=self.releases_url,
                    published_at=None,
                    release_notes=None,
                )
                return self._state
            tag = str(release.get("tag_name") or release.get("name") or "").strip()
            version = tag.removeprefix("v")
            is_newer = _version_tuple(version) > _version_tuple(__version__)
            self._state = replace(
                self._state,
                checking=False,
                available_version=version if is_newer else None,
                message=(
                    f"发现新版本 {version}"
                    if is_newer
                    else f"当前已是最新版本（{__version__}）"
                ),
                release_url=str(release.get("html_url") or self.releases_url),
                published_at=str(release.get("published_at") or "") or None,
                release_notes=str(release.get("body") or "") or None,
            )
        except (httpx.HTTPError, ValueError, TypeError, KeyError) as error:
            self._state = replace(
                self._state,
                checking=False,
                message=f"检查更新失败：{type(error).__name__}",
            )
        return self._state

    def _fetch_release(self) -> Any:
        endpoint = f"https://api.github.com/repos/{self.owner}/{self.repository}/releases"
        response = httpx.get(
            endpoint,
            params={"per_page": 20},
            headers={"Accept": "application/vnd.github+json", "User-Agent": "CodexRelay"},
            timeout=self.timeout,
        )
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, list):
            raise ValueError("GitHub Releases response is not a list")
        return payload

    def _select_release(self, payload: list[Any]) -> dict[str, Any] | None:
        for item in payload:
            if not isinstance(item, dict) or bool(item.get("draft")):
                continue
            if bool(item.get("prerelease")):
                continue
            return item
        return None
