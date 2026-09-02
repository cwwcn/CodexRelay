from __future__ import annotations

import hashlib
import os
import platform
import re
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

from codexrelay.updates.base import UpdateState
from codexrelay.version import __build_time__, __version__


def _version_tuple(value: str) -> tuple[int, ...]:
    numbers = re.findall(r"\d+", value)
    return tuple(int(number) for number in numbers) or (0,)


def _architecture() -> str:
    machine = platform.machine().casefold()
    if machine in {"arm64", "aarch64"}:
        return "arm64"
    if machine in {"x86_64", "amd64", "x64"}:
        return "x86_64"
    return machine or "unknown"


class GitHubReleaseUpdateProvider:
    """GitHub Releases provider with architecture-aware, verified downloads.

    Checking is read-only. Downloading is explicit and writes only to the
    application's private update cache; installation remains user-confirmed
    because an ad-hoc signed app cannot safely replace itself in the background.
    """

    def __init__(
        self,
        *,
        owner: str | None = None,
        repository: str | None = None,
        timeout: float = 8.0,
        architecture: str | None = None,
        download_directory: Path | None = None,
    ) -> None:
        self.owner = owner or os.environ.get("CODEXRELAY_GITHUB_OWNER", "cwwcn")
        self.repository = repository or os.environ.get("CODEXRELAY_GITHUB_REPOSITORY", "CodexRelay")
        self.timeout = timeout
        self.architecture = architecture or _architecture()
        self.download_directory = download_directory or (
            Path.home() / "Library" / "Application Support" / "CodexRelay" / "updates"
        )
        self._state = UpdateState(
            enabled=True,
            architecture=self.architecture,
            message="尚未检查更新",
        )
        self._release: dict[str, Any] | None = None

    @property
    def state(self) -> UpdateState:
        return self._state

    @property
    def releases_url(self) -> str:
        return f"https://github.com/{self.owner}/{self.repository}/releases"

    def check_for_updates(self) -> UpdateState:
        self._state = replace(
            self._state,
            checking=True,
            message="正在检查 GitHub Releases…",
        )
        try:
            payload = self._fetch_release()
            release = self._select_release(payload)
            self._release = release
            if release is None:
                self._state = replace(
                    self._state,
                    checking=False,
                    message="暂未找到公开发行版",
                    available_version=None,
                    release_url=self.releases_url,
                    published_at=None,
                    release_notes=None,
                    asset_name=None,
                    asset_url=None,
                    asset_digest=None,
                )
                return self._state

            tag = str(release.get("tag_name") or release.get("name") or "").strip()
            version = tag.removeprefix("v")
            is_newer = _version_tuple(version) > _version_tuple(__version__)
            asset = self._select_asset(release)
            same_version_fix = bool(
                asset
                and not is_newer
                and _version_tuple(version) == _version_tuple(__version__)
                and self._asset_is_newer_build(asset[3])
            )
            update_available = is_newer or same_version_fix
            available = version if update_available else None
            downloaded_path = self._state.downloaded_path
            if asset is not None and downloaded_path is not None:
                if Path(downloaded_path).name != asset[0] or not Path(downloaded_path).is_file():
                    downloaded_path = None
            if is_newer and asset is None:
                message = f"发现新版本 {version}，但暂未提供当前 Mac 的安装包"
            elif is_newer:
                message = f"发现新版本 {version} · {self.architecture} 安装包可用"
            elif same_version_fix:
                message = f"发现当前版本的修复版 {version} · {self.architecture} 安装包可用"
            else:
                message = f"当前已是最新版本（{__version__}）"
            self._state = replace(
                self._state,
                checking=False,
                available_version=available,
                message=message,
                release_url=str(release.get("html_url") or self.releases_url),
                published_at=str(release.get("published_at") or "") or None,
                release_notes=str(release.get("body") or "") or None,
                asset_name=None if asset is None else asset[0],
                asset_url=None if asset is None else asset[1],
                asset_digest=None if asset is None else asset[2],
                downloaded_path=downloaded_path if update_available else None,
            )
        except httpx.HTTPStatusError as error:
            status = error.response.status_code
            if status == 404:
                message = "无法访问 GitHub Releases：仓库尚未公开或地址不存在"
            elif status == 403:
                message = "无法访问 GitHub Releases：请求频率受限"
            else:
                message = f"检查更新失败：GitHub 返回 HTTP {status}"
            self._state = replace(self._state, checking=False, message=message)
        except httpx.TimeoutException:
            self._state = replace(
                self._state,
                checking=False,
                message="检查更新失败：连接 GitHub 超时",
            )
        except (httpx.HTTPError, ValueError, TypeError, KeyError) as error:
            self._state = replace(
                self._state,
                checking=False,
                message=f"检查更新失败：{type(error).__name__}",
            )
        return self._state

    def download_update(self) -> UpdateState:
        state = self._state
        if not state.available_version or not state.asset_url or not state.asset_name:
            self._state = replace(state, message="当前没有可下载的更新", downloading=False)
            return self._state
        self.download_directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        destination = self.download_directory / state.asset_name
        temporary = destination.with_name(f".{destination.name}.part")
        self._state = replace(
            state,
            downloading=True,
            downloaded_bytes=0,
            total_bytes=None,
            message=f"正在下载 {state.asset_name}…",
        )
        try:
            digest = state.asset_digest or self._fetch_checksum(state.asset_name)
            if digest is None:
                raise ValueError("Release 未提供 SHA-256 校验值")
            if destination.is_file():
                cached_digest = hashlib.sha256(destination.read_bytes()).hexdigest()
                if cached_digest.casefold() == digest.casefold():
                    cached_size = destination.stat().st_size
                    self._state = replace(
                        self._state,
                        downloaded_path=str(destination),
                        downloading=False,
                        downloaded_bytes=cached_size,
                        total_bytes=cached_size,
                        message=f"更新包已准备好（{state.asset_name}）",
                    )
                    return self._state
            calculated = hashlib.sha256()
            with httpx.stream(
                "GET",
                state.asset_url,
                headers={"User-Agent": "CodexRelay"},
                timeout=httpx.Timeout(self.timeout, read=self.timeout + 60),
                follow_redirects=True,
            ) as response:
                response.raise_for_status()
                content_length = response.headers.get("Content-Length")
                total_bytes = (
                    int(content_length)
                    if content_length is not None and content_length.isdigit()
                    else None
                )
                self._state = replace(self._state, total_bytes=total_bytes)
                with temporary.open("wb") as output:
                    for chunk in response.iter_bytes():
                        calculated.update(chunk)
                        output.write(chunk)
                        self._state = replace(
                            self._state,
                            downloaded_bytes=self._state.downloaded_bytes + len(chunk),
                        )
            actual = calculated.hexdigest()
            if actual.casefold() != digest.casefold():
                raise ValueError("下载包 SHA-256 校验失败")
            temporary.replace(destination)
        except (httpx.HTTPError, OSError, ValueError) as error:
            temporary.unlink(missing_ok=True)
            self._state = replace(
                self._state,
                downloading=False,
                message=f"下载更新失败：{error}",
            )
            return self._state
        self._state = replace(
            self._state,
            downloaded_path=str(destination),
            downloading=False,
            downloaded_bytes=destination.stat().st_size,
            total_bytes=destination.stat().st_size,
            message=f"更新包已下载（{state.asset_name}）",
        )
        return self._state

    def _fetch_release(self) -> list[Any]:
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

    def _select_asset(
        self, release: dict[str, Any]
    ) -> tuple[str, str, str | None, str | None] | None:
        assets = release.get("assets")
        if not isinstance(assets, list):
            return None
        version = str(release.get("tag_name") or "").removeprefix("v")
        expected_name = f"CodexRelay-macos-{self.architecture}-v{version}.dmg"
        for item in assets:
            if not isinstance(item, dict) or item.get("name") != expected_name:
                continue
            name = str(item.get("name"))
            url = str(item.get("browser_download_url") or "")
            if not url:
                return None
            digest = item.get("digest")
            normalized_digest = (
                str(digest).removeprefix("sha256:") if isinstance(digest, str) else None
            )
            updated_at = item.get("updated_at")
            return name, url, normalized_digest, str(updated_at) if updated_at else None
        return None

    @staticmethod
    def _asset_is_newer_build(updated_at: str | None) -> bool:
        if not updated_at or __build_time__ == "Source build":
            return False
        try:
            asset_time = datetime.fromisoformat(updated_at.replace("Z", "+00:00"))
            build_time = datetime.strptime(__build_time__, "%Y-%m-%d %H:%M UTC").replace(
                tzinfo=UTC
            )
        except ValueError:
            return False
        return asset_time > build_time

    def _fetch_checksum(self, asset_name: str) -> str | None:
        if self._release is None:
            return None
        assets = self._release.get("assets")
        if not isinstance(assets, list):
            return None
        checksum_url = next(
            (
                str(item.get("browser_download_url"))
                for item in assets
                if isinstance(item, dict) and item.get("name") == "SHA256SUMS.txt"
            ),
            None,
        )
        if checksum_url is None:
            return None
        response = httpx.get(
            checksum_url,
            headers={"User-Agent": "CodexRelay"},
            timeout=self.timeout,
        )
        response.raise_for_status()
        for line in response.text.splitlines():
            match = re.fullmatch(r"([0-9a-fA-F]{64})\s+\*?(.+)", line.strip())
            if match is not None and match.group(2) == asset_name:
                digest = match.group(1)
                return digest
        return None
