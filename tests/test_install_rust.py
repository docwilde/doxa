# SPDX-License-Identifier: AGPL-3.0-only
"""Offline native installer checks: local Git source, stub Cargo, compiled carriers."""
import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
INSTALL_SH = ROOT / "scripts/install.sh"


def _compile_native_stub(directory):
    """Use an ELF fixture so installed launch checks require no Python runtime."""
    source = directory / "carrier.c"
    source.write_text(r'''#include <stdio.h>
#include <stdlib.h>
#include <string.h>
static void string(FILE *out, const char *value) {
    if (!value) { fputs("null",out); return; }
    fputc('"',out);
    for (;*value;value++) {
        unsigned char c=*value;
        if (c=='"'||c=='\\') { fputc('\\',out); fputc(c,out); }
        else if (c<32) fprintf(out,"\\u%04x",c);
        else fputc(c,out);
    }
    fputc('"',out);
}
static void reply(FILE *out,int argc,char **argv) {
    fputs("{\"args\":[",out);
    for (int i=1;i<argc;i++) { if(i>1) fputc(',',out); string(out,argv[i]); }
    fputs("],\"lore\":",out); string(out,getenv("DOXA_LORE_RS"));
    fputs(",\"daemon\":",out); string(out,getenv("DOXA_DAEMON_BIN"));
    fputs(",\"python\":",out); string(out,getenv("DOXA_LORE_PYTHON"));
    fputs("}\n",out);
}
int main(int argc,char **argv) {
    const char *log=getenv("DOXA_TEST_FRONTEND_LOG");
    if (log) { FILE *out=fopen(log,"a"); if (!out) return 61; reply(out,argc,argv); fclose(out); }
    if (argc>1 && !strcmp(argv[1],"install-launcher")) {
        if (argc!=3 || strchr(argv[2],'\n') || getenv("DOXA_TEST_FAIL_SHORTCUT")) return 65;
        const char *path=getenv("DOXA_TEST_SHORTCUT_PATH");
        if (!path) return 66;
        FILE *out=fopen(path,"w"); if(!out) return 67;
        fputs(argv[2],out); fclose(out); return 0;
    }
    reply(stdout,argc,argv); return 0;
}
''')
    binary = directory / "native-carrier"
    subprocess.run(["cc", "-O0", str(source), "-o", str(binary)], check=True)
    return binary


def _commit(repo, message="test: native installer fixture"):
    subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
    subprocess.run(["git", "-C", str(repo), "-c", "user.name=Test", "-c",
                    "user.email=test@example.invalid", "commit", "-qm", message], check=True)
    return subprocess.check_output(["git", "-C", str(repo), "rev-parse", "HEAD"], text=True).strip()


def _source_repo(tmp_path):
    repo = tmp_path / "source"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    for name in ("rust/doxa-tui/Cargo.toml", "rust/doxa-daemon/Cargo.toml", "Cargo.lock"):
        path = repo / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("# native fixture\n")
    script = repo / "scripts/install_codex_protected.py"
    script.parent.mkdir()
    script.write_text("""import json,os,sys
from pathlib import Path
args=sys.argv[1:]
launcher=Path(args[args.index('--launcher')+1])
assert launcher.is_absolute() and os.access(launcher,os.X_OK)
with open(os.environ['DOXA_TEST_PROVIDER_LOG'],'a') as out:
    out.write(json.dumps({'args':args,'cache':os.environ.get('DOXA_CODEX_PROTECTED_CACHE')})+'\\n')
if os.environ.get('DOXA_TEST_FAIL_PROVIDER')=='1': sys.exit(74)
""")
    _commit(repo)
    subprocess.run(["git", "-C", str(repo), "branch", "-M", "main"], check=True)
    return repo


def _run(tmp_path, repo, *args, fail_install_name=None, cargo=True, codex=False, env_overrides=None):
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    fakebin = tmp_path / "fakebin"
    fakebin.mkdir(exist_ok=True)
    native = tmp_path / "native-carrier"
    if not native.exists():
        _compile_native_stub(tmp_path)
    move = fakebin / "mv"
    if fail_install_name:
        move.write_text('#!/bin/sh\ncase $2 in */.doxa-install.*/' + fail_install_name +
                        ') exit 73 ;; esac\nexec /usr/bin/mv "$@"\n')
        move.chmod(0o700)
    elif move.exists():
        move.unlink()
    rustc = fakebin / "rustc"
    rustc.write_text("#!/bin/sh\nprintf 'host: test-host-target\\n'\n")
    rustc.chmod(0o700)
    if cargo:
        cargo_script = fakebin / "cargo"
        cargo_script.write_text("""#!/usr/bin/python3
import json,os,shutil,sys
from pathlib import Path
args=sys.argv[1:]
with open(os.environ['DOXA_TEST_LOG'],'a') as out: out.write(json.dumps(args)+'\\n')
binary=args[args.index('--bin')+1] if '--bin' in args else 'doxa-daemon'
if os.environ.get('DOXA_TEST_FAIL_BUILD')==binary: sys.exit(72)
target=Path(os.environ['CARGO_TARGET_DIR'])/args[args.index('--target')+1]/'release'
target.mkdir(parents=True,exist_ok=True)
shutil.copyfile(os.environ['DOXA_TEST_NATIVE'],target/binary)
(target/binary).chmod(0o700)
""")
        cargo_script.chmod(0o700)
    official = fakebin / "codex"
    if codex:
        official.write_text("#!/bin/sh\nexit 0\n")
        official.chmod(0o700)
    elif official.exists():
        official.unlink()
    utilities = tmp_path / "utilities"
    utilities.mkdir(exist_ok=True)
    for name in ("git", "mktemp", "cp", "mv", "rm", "mkdir", "chmod", "python3", "sed", "dirname", "cat", "sh", "cc"):
        path = utilities / name
        if not path.exists():
            path.symlink_to(shutil.which(name))
    log = tmp_path / "cargo.jsonl"
    env = {**os.environ, "HOME": str(home), "DOXA_HOME": str(home / ".doxa"),
           "PATH": f"{fakebin}:{utilities}", "TMPDIR": str(tmp_path),
           "DOXA_RUST_REPO_URL": str(repo), "DOXA_TEST_LOG": str(log),
           "DOXA_TEST_NATIVE": str(native), "DOXA_TEST_FRONTEND_LOG": str(tmp_path / "frontend.jsonl"),
           "DOXA_TEST_SHORTCUT_PATH": str(tmp_path / "shortcut.request"),
           "DOXA_TEST_PROVIDER_LOG": str(tmp_path / "provider.jsonl"),
           "DOXA_INSTALL_CACHE_DIR": str(home / ".cache/doxa/install"),
           "XDG_DATA_HOME": str(home / ".local/share"), **(env_overrides or {})}
    proc = subprocess.run(["sh", str(INSTALL_SH), *args], cwd=tmp_path, env=env,
                          text=True, capture_output=True, timeout=20)
    return proc, home, log


def _calls(path):
    return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


def test_default_installs_native_carriers_and_runs_from_bare_path(tmp_path):
    repo = _source_repo(tmp_path)
    proc, home, log = _run(tmp_path, repo)
    assert proc.returncode == 0, proc.stderr
    bin_dir = home / ".local/bin"
    for name in ("doxa", "doxa-rs", "doxa-daemon-rs", "lore-rs"):
        assert os.access(bin_dir / name, os.X_OK)
    assert (bin_dir / "doxa-rs").read_bytes().startswith(b"\x7fELF")
    builds = _calls(log)
    assert len(builds) == 3 and all("--locked" in call for call in builds)
    assert builds[-1][-4:] == ["--package", "lore-core", "--bin", "lore-rs"]
    assert not (bin_dir / "doxa-claude-sidecar.py").exists()
    assert not (bin_dir / ".doxa-sidecar-current").is_symlink()
    run = subprocess.run([str(bin_dir / "doxa"), "list", "a b"], cwd="/",
                         env={"PATH": "/usr/bin:/bin"}, text=True, capture_output=True, timeout=3)
    assert run.returncode == 0, run.stderr
    reply = json.loads(run.stdout)
    assert reply["args"] == ["list", "a b"]
    assert reply["lore"] == str(bin_dir / "lore-rs") and reply["python"] is None
    assert (tmp_path / "shortcut.request").read_text() == str(bin_dir / "doxa")
    assert (bin_dir / ".doxa-install-sha").read_text().strip() == subprocess.check_output(
        ["git", "-C", str(repo), "rev-parse", "HEAD"], text=True).strip()
    assert not list((home / ".cache/doxa/install").glob("checkout.*"))


def test_shortcut_passes_exact_installed_path_and_refreshes_on_reinstall(tmp_path):
    repo = _source_repo(tmp_path)
    custom_bin = tmp_path / "bin dir $draft %two"
    options = {"DOXA_RUST_BIN_DIR": str(custom_bin)}
    for _ in range(2):
        proc, _, _ = _run(tmp_path, repo, env_overrides=options)
        assert proc.returncode == 0, proc.stderr
        assert (tmp_path / "shortcut.request").read_text() == str(custom_bin / "doxa")
        (tmp_path / "shortcut.request").write_text("stale request")
    requests = [call for call in _calls(tmp_path / "frontend.jsonl") if call["args"][0] == "install-launcher"]
    assert [call["args"] for call in requests] == [["install-launcher", str(custom_bin / "doxa")]] * 2


@pytest.mark.parametrize("options", [{"DOXA_NO_LAUNCHER": "1"}, {"DOXA_TEST_FAIL_SHORTCUT": "1"}])
def test_shortcut_skip_or_failure_preserves_native_install(tmp_path, options):
    proc, home, _ = _run(tmp_path, _source_repo(tmp_path), env_overrides=options)
    assert proc.returncode == 0, proc.stderr
    assert (home / ".local/bin/doxa").exists()
    assert not (tmp_path / "shortcut.request").exists()
    if "DOXA_TEST_FAIL_SHORTCUT" in options:
        assert "could not install desktop shortcut" in proc.stderr
    else:
        assert all(call["args"][0] != "install-launcher" for call in _calls(tmp_path / "frontend.jsonl"))


def test_upgrade_removes_legacy_sidecars_and_replaces_directory_symlinks(tmp_path):
    repo = _source_repo(tmp_path)
    bin_dir = tmp_path / "home/.local/bin"
    bin_dir.mkdir(parents=True)
    outside = tmp_path / "old-target"
    outside.mkdir()
    (outside / "sentinel").write_text("unchanged")
    for name in ("doxa", "doxa-rs", "doxa-daemon-rs", "lore-rs"):
        (bin_dir / name).symlink_to(outside, target_is_directory=True)
    (bin_dir / ".doxa-sidecar-current").symlink_to("old-env")
    (bin_dir / "doxa-claude-sidecar.py").write_text("legacy oracle")
    proc, _, _ = _run(tmp_path, repo)
    assert proc.returncode == 0, proc.stderr
    assert all(not (bin_dir / name).is_symlink() for name in ("doxa", "doxa-rs", "doxa-daemon-rs", "lore-rs"))
    assert sorted(path.name for path in outside.iterdir()) == ["sentinel"]
    assert not (bin_dir / ".doxa-sidecar-current").is_symlink()
    assert not (bin_dir / "doxa-claude-sidecar.py").exists()


@pytest.mark.parametrize("failed_name", ["doxa-rs", "doxa-daemon-rs", "lore-rs", ".doxa-install-sha", "doxa"])
def test_failed_publication_restores_all_previous_files_and_legacy_pointer(tmp_path, failed_name):
    repo = _source_repo(tmp_path)
    bin_dir = tmp_path / "home/.local/bin"
    bin_dir.mkdir(parents=True)
    names = ("doxa", "doxa-rs", "doxa-daemon-rs", "lore-rs", ".doxa-install-sha", "doxa-claude-sidecar.py")
    before = {name: ("old " + name).encode() for name in names}
    for name, data in before.items():
        (bin_dir / name).write_bytes(data)
    (bin_dir / ".doxa-sidecar-current").symlink_to("old-env")
    proc, _, _ = _run(tmp_path, repo, fail_install_name=failed_name)
    assert proc.returncode != 0
    assert {name: (bin_dir / name).read_bytes() for name in names} == before
    assert os.readlink(bin_dir / ".doxa-sidecar-current") == "old-env"
    assert not list(bin_dir.glob(".doxa-install.*"))
    assert not (tmp_path / "shortcut.request").exists()


def test_failed_native_build_leaves_existing_launcher_untouched(tmp_path):
    repo = _source_repo(tmp_path)
    bin_dir = tmp_path / "home/.local/bin"
    bin_dir.mkdir(parents=True)
    (bin_dir / "doxa").write_text("old launcher")
    proc, _, _ = _run(tmp_path, repo, env_overrides={"DOXA_TEST_FAIL_BUILD": "lore-rs"})
    assert proc.returncode != 0
    assert (bin_dir / "doxa").read_text() == "old launcher"
    assert not (bin_dir / "doxa-rs").exists()


@pytest.mark.parametrize("ref", ["--upload-pack=evil", "../../etc", "rust/2.0;evil", "@{upstream}"])
def test_rejects_unsafe_refs(tmp_path, ref):
    proc, home, _ = _run(tmp_path, _source_repo(tmp_path), ref)
    assert proc.returncode != 0 and "invalid ref" in proc.stderr
    assert not (home / ".local/bin").exists()


def test_missing_cargo_fails_before_mutation(tmp_path):
    proc, home, _ = _run(tmp_path, _source_repo(tmp_path), cargo=False)
    assert proc.returncode != 0 and "cargo is required" in proc.stderr
    assert not (home / ".local/bin").exists()


@pytest.mark.parametrize("variable", ["DOXA_INSTALL_CACHE_DIR", "DOXA_INSTALL_TARGET_DIR"])
def test_rejects_symlinked_build_caches(tmp_path, variable):
    outside = tmp_path / "outside"
    outside.mkdir()
    link = tmp_path / "cache-link"
    link.symlink_to(outside, target_is_directory=True)
    proc, home, _ = _run(tmp_path, _source_repo(tmp_path), env_overrides={variable: str(link)})
    assert proc.returncode != 0 and "symlink" in proc.stderr
    assert not (home / ".local/bin/doxa").exists()
    assert not list(outside.iterdir())


def test_default_builds_native_protected_dispatcher_and_calls_owned_builder(tmp_path):
    cache = tmp_path / "private provider cache"
    proc, home, log = _run(tmp_path, _source_repo(tmp_path), codex=True,
                           env_overrides={"DOXA_CODEX_PROTECTED_CACHE": str(cache)})
    assert proc.returncode == 0, proc.stderr
    builds = _calls(log)
    assert len(builds) == 4
    assert builds[-1][-6:] == ["--package", "doxa-engines", "--bin", "doxa-codex-protected", "-j", "1"]
    call, = _calls(tmp_path / "provider.jsonl")
    launcher = Path(call["args"][1])
    assert call["args"][0] == "--launcher" and launcher.read_bytes().startswith(b"\x7fELF")
    assert call["cache"] == str(cache)
    assert (home / ".local/bin/doxa").exists()


def test_protected_opt_out_skips_builder_even_with_official_codex(tmp_path):
    proc, home, log = _run(tmp_path, _source_repo(tmp_path), codex=True,
                           env_overrides={"DOXA_INSTALL_CODEX_PROTECTED": "0"})
    assert proc.returncode == 0, proc.stderr
    assert len(_calls(log)) == 3
    assert not (tmp_path / "provider.jsonl").exists()
    assert (home / ".local/bin/doxa").exists()


def test_protected_builder_failure_preserves_existing_launcher(tmp_path):
    bin_dir = tmp_path / "home/.local/bin"
    bin_dir.mkdir(parents=True)
    (bin_dir / "doxa").write_text("old launcher")
    proc, _, _ = _run(tmp_path, _source_repo(tmp_path), codex=True,
                      env_overrides={"DOXA_TEST_FAIL_PROVIDER": "1"})
    assert proc.returncode != 0
    assert (bin_dir / "doxa").read_text() == "old launcher"
    assert not (bin_dir / "doxa-rs").exists()
