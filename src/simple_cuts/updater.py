"""Check GitHub Releases for a newer Simple Cuts and update a per-user install.

Only installs made with ``simple-cuts.sh --install`` update themselves (they live in
~/.local/share/simple-cuts/app, which the user owns). Distro packages, the Flatpak and
the single-file script are told about new versions with a link to the release page,
because their package manager (or the user) owns those files.

No GUI code here, so it is unit-tested directly.
"""

from __future__ import annotations

import io
import json
import os
import re
import shutil
import tarfile
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from . import __version__

REPO = "plaintextlab/simple-cuts"
RELEASES_URL = f"https://github.com/{REPO}/releases"
ALLOWED_HOSTS = {"api.github.com", "github.com", "codeload.github.com"}
MAX_DOWNLOAD = 50 * 1024 * 1024
CHECK_INTERVAL = 24 * 3600


class UpdateError(Exception):
    pass


@dataclass
class Release:
    version: str
    tag: str
    page_url: str
    tarball_url: str
    notes: str = ""


# ---------------------------------------------------------------- versions
def parse_version(text: str) -> tuple[int, ...]:
    """'v1.2.10' -> (1, 2, 10). Non-numeric suffixes are ignored."""
    nums = re.findall(r"\d+", text.lstrip("vV").split("-")[0].split("+")[0])
    return tuple(int(n) for n in nums) or (0,)


def is_newer(candidate: str, current: str = __version__) -> bool:
    return parse_version(candidate) > parse_version(current)


# ---------------------------------------------------------------- install kind
def user_data_dir() -> Path:
    return Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share")


def user_app_dir() -> Path:
    """Where ``simple-cuts.sh --install`` puts the app (must match the launcher)."""
    return user_data_dir() / "simple-cuts" / "app"


def launcher_path() -> Path:
    return Path(os.environ.get("SIMPLE_CUTS_LAUNCHER") or Path.home() / ".local" / "bin" /
                "simple-cuts")


def install_kind(package_dir: Path | None = None) -> str:
    """'user' (self-updating), 'flatpak', 'dev' (git checkout), 'portable'
    (single-file script) or 'system' (distro package)."""
    pkg = (package_dir or Path(__file__).parent).resolve()
    if Path("/.flatpak-info").exists():
        return "flatpak"
    try:
        if pkg.parent == user_app_dir().resolve():
            return "user"
    except OSError:
        pass
    if pkg.parent.name == "src" and (pkg.parent.parent / ".git").exists():
        return "dev"
    cache = Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache") / "simple-cuts"
    if cache.resolve() in pkg.parents:
        return "portable"
    return "system"


def can_self_update(kind: str | None = None) -> bool:
    return (kind or install_kind()) == "user"


# ---------------------------------------------------------------- network
def _api_url() -> str:
    return os.environ.get("SIMPLE_CUTS_UPDATE_API") or \
        f"https://api.github.com/repos/{REPO}/releases/latest"


def _allowed(url: str) -> bool:
    host = urllib.parse.urlparse(url).hostname or ""
    override = os.environ.get("SIMPLE_CUTS_UPDATE_API")
    if override and host == urllib.parse.urlparse(override).hostname:
        return True                                   # test server
    return urllib.parse.urlparse(url).scheme == "https" and host in ALLOWED_HOSTS


def _open(url: str, timeout: float):
    if not _allowed(url):
        raise UpdateError(f"refusing to download from {url}")
    req = urllib.request.Request(url, headers={
        "Accept": "application/vnd.github+json",
        "User-Agent": f"simple-cuts/{__version__} (updater)",
    })
    resp = urllib.request.urlopen(req, timeout=timeout)       # noqa: S310 - host checked
    if not _allowed(resp.geturl()):                           # after redirects
        resp.close()
        raise UpdateError(f"redirected to an unexpected host: {resp.geturl()}")
    return resp


def check_for_update(timeout: float = 10.0) -> Release | None:
    """The latest release if it is newer than this version, else None."""
    try:
        with _open(_api_url(), timeout) as resp:
            data = json.load(resp)
    except urllib.error.HTTPError as exc:
        if exc.code == 404:          # no releases published yet
            return None
        raise UpdateError(f"GitHub returned {exc.code}") from exc
    except (urllib.error.URLError, OSError, ValueError) as exc:
        raise UpdateError(f"could not check for updates: {exc}") from exc
    tag = str(data.get("tag_name") or "")
    if not tag or data.get("draft") or data.get("prerelease"):
        return None
    version = tag.lstrip("vV")
    if not is_newer(version):
        return None
    return Release(version=version, tag=tag,
                   page_url=str(data.get("html_url") or RELEASES_URL),
                   tarball_url=str(data.get("tarball_url") or ""),
                   notes=str(data.get("body") or ""))


# ---------------------------------------------------------------- applying
ProgressFn = Callable[[str], None]


def _download(url: str, timeout: float = 60.0) -> bytes:
    with _open(url, timeout) as resp:
        data = resp.read(MAX_DOWNLOAD + 1)
    if len(data) > MAX_DOWNLOAD:
        raise UpdateError("update download is unexpectedly large")
    return data


def _safe_extract(data: bytes, dest: Path) -> None:
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:*") as tar:
        for m in tar.getmembers():
            target = (dest / m.name).resolve()
            if dest.resolve() not in target.parents and target != dest.resolve():
                raise UpdateError(f"unsafe path in update archive: {m.name}")
            if m.issym() or m.islnk() or m.isdev():
                raise UpdateError(f"unexpected link in update archive: {m.name}")
        if hasattr(tarfile, "data_filter"):
            tar.extractall(dest, filter="data")
        else:
            tar.extractall(dest)  # noqa: S202 - members validated above


def _find_source_root(extracted: Path) -> Path:
    for candidate in [extracted, *extracted.iterdir()]:
        if (candidate / "src" / "simple_cuts" / "app.py").is_file():
            return candidate
    raise UpdateError("the update archive does not contain Simple Cuts")


def _read_version(pkg: Path) -> str:
    m = re.search(r'__version__\s*=\s*"([^"]+)"', (pkg / "__init__.py").read_text())
    return m.group(1) if m else ""


def _install_launcher(new_script: Path, app_dir: Path) -> None:
    text = new_script.read_text()
    text = re.sub(r'^INSTALLED_APP_DIR=".*"$', f'INSTALLED_APP_DIR="{app_dir}"', text,
                  count=1, flags=re.M)
    target = launcher_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(target.name + ".tmp")
    tmp.write_text(text)
    tmp.chmod(0o755)
    os.replace(tmp, target)


def apply_update(release: Release, progress: ProgressFn | None = None,
                 package_dir: Path | None = None) -> Path:
    """Download the release and replace the per-user install. Returns the launcher."""
    progress = progress or (lambda _msg: None)
    app_dir = user_app_dir()
    if not can_self_update(install_kind(package_dir)):
        raise UpdateError("this copy of Simple Cuts is not a per-user install")
    if not release.tarball_url:
        raise UpdateError("the release has no source archive")

    progress(f"Downloading {release.version}…")
    data = _download(release.tarball_url)

    progress("Installing…")
    with tempfile.TemporaryDirectory(prefix="simple-cuts-update-") as tmp:
        tmp_path = Path(tmp)
        _safe_extract(data, tmp_path)
        root = _find_source_root(tmp_path)
        new_pkg = root / "src" / "simple_cuts"
        got = _read_version(new_pkg)
        if got != release.version:
            raise UpdateError(f"archive contains version {got or '?'}, "
                              f"expected {release.version}")

        # Swap the app directory: build app.new, then rename into place.
        new_dir = app_dir.with_name("app.new")
        old_dir = app_dir.with_name("app.old")
        shutil.rmtree(new_dir, ignore_errors=True)
        shutil.copytree(new_pkg, new_dir / "simple_cuts",
                        ignore=shutil.ignore_patterns("__pycache__"))
        shutil.rmtree(old_dir, ignore_errors=True)
        if app_dir.exists():
            os.replace(app_dir, old_dir)
        os.replace(new_dir, app_dir)
        shutil.rmtree(old_dir, ignore_errors=True)

        launcher = root / "simple-cuts.sh"
        if launcher.is_file():
            _install_launcher(launcher, app_dir)
        icon = app_dir / "simple_cuts" / "data" / "io.github.simplecuts.SimpleCuts.svg"
        icon_dest = user_data_dir() / "icons" / "hicolor" / "scalable" / "apps" / icon.name
        if icon.is_file() and icon_dest.parent.is_dir():
            shutil.copyfile(icon, icon_dest)

    progress(f"Updated to {release.version}")
    return launcher_path()
