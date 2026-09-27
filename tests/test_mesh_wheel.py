"""Serve the real mesh assets from an unpacked production wheel, outside the repo."""
import os
from pathlib import Path
import shutil
import subprocess
import sys
import zipfile


def test_installed_wheel_serves_token_gated_mesh_assets(tmp_path):
    root = Path(__file__).resolve().parents[1]
    uv = shutil.which("uv")
    assert uv, "installed-wheel gate requires uv"
    output = tmp_path / "wheels"
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    subprocess.run([uv, "build", "--wheel", "--out-dir", str(output)], cwd=root,
                   env={**os.environ, "TMPDIR": str(scratch)}, check=True,
                   stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=90)
    wheel, = output.glob("doxa-*.whl")
    installed = tmp_path / "installed"
    with zipfile.ZipFile(wheel) as archive:
        for name in ("index.html", "mesh.js", "mesh.css"):
            assert archive.read(f"doxa/assets/mesh/{name}") == (root / "assets/mesh" / name).read_bytes()
        archive.extractall(installed)
    script = '''
import sys
from pathlib import Path
from urllib.request import urlopen
from urllib.error import HTTPError
sys.path.insert(0, sys.argv[1])
from doxa.meshgraph import MeshServer, assets_dir
assert assets_dir() == Path(sys.argv[1]) / "doxa/assets/mesh"
with MeshServer(path=Path(sys.argv[2])) as server:
    for route, mime in [("", "text/html"), ("mesh.js", "text/javascript"), ("mesh.css", "text/css")]:
        with urlopen(server.url + route, timeout=3) as response:
            assert response.status == 200
            assert response.headers.get_content_type() == mime
            assert response.read()
    for route in ["../index.html", "../mesh.js", "../mesh.css"]:
        try:
            urlopen(server.url + route, timeout=3)
        except HTTPError as error:
            assert error.code == 404
        else:
            raise AssertionError("asset bypassed token")
'''
    subprocess.run([sys.executable, "-I", "-c", script, str(installed), str(tmp_path / "empty-ledger.jsonl")],
                   cwd=tmp_path, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=15)
