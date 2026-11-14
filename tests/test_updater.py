import io
import json
import re
import shutil
import tarfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

import simple_cuts
from simple_cuts import updater

ROOT = Path(__file__).resolve().parents[1]


# ---------------------------------------------------------------- helpers
def make_tarball(version: str, extra: dict[str, bytes] | None = None) -> bytes:
    """A GitHub-style source tarball of this repo, with __version__ set to ``version``."""
    buf = io.BytesIO()
    top = f"plaintextlab-simple-cuts-{version}"
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        def add(name: str, data: bytes, mode: int = 0o644):
            info = tarfile.TarInfo(f"{top}/{name}")
            info.size, info.mode = len(data), mode
            tar.addfile(info, io.BytesIO(data))
        for f in (ROOT / "src" / "simple_cuts").rglob("*"):
            if f.is_file() and "__pycache__" not in f.parts:
                data = f.read_bytes()
                if f.name == "__init__.py" and f.parent.name == "simple_cuts":
                    data = re.sub(rb'__version__ = "[^"]+"',
                                  f'__version__ = "{version}"'.encode(), data)
                add(str(f.relative_to(ROOT)), data)
        script = (ROOT / "simple-cuts.sh").read_bytes()
        script = re.sub(rb'^VERSION="[^"]+"', f'VERSION="{version}"'.encode(), script,
                        flags=re.M)
        add("simple-cuts.sh", script, 0o755)
        for name, data in (extra or {}).items():
            add(name, data)
    return buf.getvalue()


class FakeGitHub:
    """Serves /latest (release JSON) and /tarball over HTTP on localhost."""

    def __init__(self, release: dict | None, tarball: bytes = b""):
        self.release, self.tarball = release, tarball
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                if self.path == "/latest":
                    if outer.release is None:
                        self.send_response(404); self.end_headers(); return
                    body = json.dumps(outer.release).encode()
                elif self.path == "/tarball":
                    body = outer.tarball
                else:
                    self.send_response(404); self.end_headers(); return
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *a):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.base = f"http://127.0.0.1:{self.server.server_port}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def release_json(self, version: str) -> dict:
        return {"tag_name": f"v{version}", "html_url": f"{self.base}/page",
                "tarball_url": f"{self.base}/tarball", "body": "notes",
                "draft": False, "prerelease": False}

    def close(self):
        self.server.shutdown()


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.setenv("SIMPLE_CUTS_LAUNCHER", str(tmp_path / "bin" / "simple-cuts"))
    return tmp_path


@pytest.fixture
def github(monkeypatch):
    gh = FakeGitHub(None)
    monkeypatch.setenv("SIMPLE_CUTS_UPDATE_API", f"{gh.base}/latest")
    yield gh
    gh.close()


def bump(v: str) -> str:
    parts = list(updater.parse_version(v)) + [0, 0]
    return f"{parts[0]}.{parts[1]}.{parts[2] + 1}"


# ---------------------------------------------------------------- tests
def test_versions_agree():
    """The app, launcher, pyproject and packages must carry the same version."""
    v = simple_cuts.__version__
    assert f'VERSION="{v}"' in (ROOT / "simple-cuts.sh").read_text()
    assert f'version = "{v}"' in (ROOT / "pyproject.toml").read_text()
    assert f"pkgver={v}" in (ROOT / "packaging/arch/PKGBUILD").read_text()
    assert f"app_version:{v}" in (ROOT / "packaging/rpm/simple-cuts.spec").read_text()
    meta = (ROOT / "src/simple_cuts/data/io.github.simplecuts.SimpleCuts.metainfo.xml")
    assert f'<release version="{v}"' in meta.read_text()


def test_version_compare():
    assert updater.parse_version("v1.10.2") == (1, 10, 2)
    assert updater.is_newer("1.10.0", "1.9.9")
    assert not updater.is_newer("v0.3.0", "0.3.0")
    assert not updater.is_newer("0.2.9", "0.3.0")


def test_check_newer_same_and_none(github):
    cur = simple_cuts.__version__
    assert updater.check_for_update() is None                 # no releases yet (404)
    github.release = github.release_json(cur)
    assert updater.check_for_update() is None                 # up to date
    github.release = github.release_json(bump(cur))
    rel = updater.check_for_update()
    assert rel is not None and rel.version == bump(cur) and rel.tag == f"v{bump(cur)}"
    github.release = {**github.release_json(bump(cur)), "prerelease": True}
    assert updater.check_for_update() is None                 # prereleases are ignored


def test_refuses_foreign_hosts(monkeypatch):
    monkeypatch.delenv("SIMPLE_CUTS_UPDATE_API", raising=False)
    assert updater._allowed("https://api.github.com/repos/x/y/releases/latest")
    assert updater._allowed("https://codeload.github.com/x/y/legacy.tar.gz/v1")
    assert not updater._allowed("http://api.github.com/insecure")
    assert not updater._allowed("https://evil.example.com/simple-cuts.tar.gz")
    with pytest.raises(updater.UpdateError):
        updater._download("https://evil.example.com/x.tar.gz")


def test_install_kind(home, tmp_path):
    app = updater.user_app_dir() / "simple_cuts"
    app.mkdir(parents=True)
    assert updater.install_kind(app) == "user"
    assert updater.install_kind(ROOT / "src" / "simple_cuts") == "dev"
    assert updater.install_kind(tmp_path / "usr/lib/simple-cuts/simple_cuts") == "system"


def install_current(home) -> Path:
    """Simulate `simple-cuts.sh --install` of the current source."""
    app = updater.user_app_dir()
    shutil.copytree(ROOT / "src" / "simple_cuts", app / "simple_cuts",
                    ignore=shutil.ignore_patterns("__pycache__"))
    return app / "simple_cuts"


def test_apply_update_replaces_install(home, github):
    new = bump(simple_cuts.__version__)
    github.release = github.release_json(new)
    github.tarball = make_tarball(new)
    pkg = install_current(home)
    rel = updater.check_for_update()
    msgs = []
    launcher = updater.apply_update(rel, msgs.append, package_dir=pkg)

    init = (updater.user_app_dir() / "simple_cuts" / "__init__.py").read_text()
    assert f'__version__ = "{new}"' in init
    text = launcher.read_text()
    assert f'VERSION="{new}"' in text
    assert f'INSTALLED_APP_DIR="{updater.user_app_dir()}"' in text
    assert launcher.stat().st_mode & 0o111
    assert not updater.user_app_dir().with_name("app.new").exists()
    assert not updater.user_app_dir().with_name("app.old").exists()
    assert msgs[-1] == f"Updated to {new}"


def test_apply_refuses_non_user_installs(home, github):
    new = bump(simple_cuts.__version__)
    rel = updater.Release(new, f"v{new}", "", f"{github.base}/tarball")
    with pytest.raises(updater.UpdateError, match="not a per-user install"):
        updater.apply_update(rel, package_dir=ROOT / "src" / "simple_cuts")


def test_apply_rejects_wrong_version_and_keeps_install(home, github):
    new = bump(simple_cuts.__version__)
    github.release = github.release_json(new)
    github.tarball = make_tarball("9.9.9")            # archive doesn't match the tag
    pkg = install_current(home)
    with pytest.raises(updater.UpdateError, match="expected"):
        updater.apply_update(updater.check_for_update(), package_dir=pkg)
    init = (updater.user_app_dir() / "simple_cuts" / "__init__.py").read_text()
    assert simple_cuts.__version__ in init            # old install untouched


def test_apply_rejects_path_traversal(home, github):
    new = bump(simple_cuts.__version__)
    github.release = github.release_json(new)
    github.tarball = make_tarball(new, {"../../escape.txt": b"x"})
    pkg = install_current(home)
    with pytest.raises(updater.UpdateError, match="unsafe path"):
        updater.apply_update(updater.check_for_update(), package_dir=pkg)
    assert not (home.parent / "escape.txt").exists()
