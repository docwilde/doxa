#!/usr/bin/env python3
"""Explicit offline Linux renderer provisioning; never changes DOXA settings."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import platform
import shutil
import stat
import subprocess

HERE = Path(__file__).resolve().parent
BROWSER_VERSION = "Google Chrome for Testing 131.0.6778.85"


def reviewed_inputs(root, browser, cache):
    if platform.system() != "Linux" or platform.machine() != "x86_64":
        raise ValueError("This pinned browser fixture supports Linux x86-64 only")
    root = root.expanduser().absolute()
    if root.exists() or root.is_symlink():
        raise ValueError("Choose a new package root; existing paths are never overwritten")
    # Resolve ancestors now, so a symlink cannot redirect the fresh installation.
    if root.parent.resolve() != root.parent:
        raise ValueError("Package root ancestors must not be symlinks")
    state = Path(os.environ.get("DOXA_HOME", str(Path.home() / ".doxa"))).expanduser().resolve()
    if root.is_relative_to(HERE.parents[1]) or root.is_relative_to(state):
        raise ValueError("Package root must be outside the repository and DOXA state")
    for ancestor in root.parents:
        if (ancestor / ".git").exists():
            raise ValueError("Package root must be outside session repositories")
    browser, cache = browser.expanduser().resolve(strict=True), cache.expanduser().resolve(strict=True)
    if not browser.is_dir() or not cache.is_dir():
        raise ValueError("A cached browser directory and populated npm cache are required")
    for item in browser.rglob("*"):
        if item.is_symlink() or not (item.is_file() or item.is_dir()):
            raise ValueError("Cached browser must contain only ordinary files and directories")
    chrome = browser / "chrome"
    if not chrome.is_file() or not os.access(chrome, os.X_OK):
        raise ValueError("Cached browser executable is missing")
    version = subprocess.check_output([str(chrome), "--version"], text=True, timeout=10).strip()
    if version != BROWSER_VERSION:
        raise ValueError("Cached browser does not match pinned Chrome for Testing 131.0.6778.85")
    lock = json.loads((HERE / "package-lock.json").read_text())
    packages = lock["packages"]
    for name, entry in packages.items():
        if name and (not entry.get("resolved", "").startswith("https://registry.npmjs.org/")
                     or not entry.get("integrity", "").startswith("sha512-")):
            raise ValueError("Every locked package must have an npm registry URL and SHA-512 integrity")
    expected = {"@mermaid-js/mermaid-cli": "12.0.0", "puppeteer": "25.13.0"}
    for name, version in expected.items():
        if packages["node_modules/" + name]["version"] != version:
            raise ValueError("Renderer lock does not match the reviewed fixture")
    return root, browser, cache, len(packages) - 1


def provision(root, browser, cache):
    root.mkdir(mode=0o700, parents=True, exist_ok=False)
    os.chmod(root, 0o700)
    for name in ("package.json", "package-lock.json", "renderer.sh", "puppeteer-config.json"):
        shutil.copyfile(HERE / name, root / name)
        os.chmod(root / name, 0o700 if name == "renderer.sh" else 0o600)
    npmrc, global_npmrc = root / "empty.npmrc", root / "empty-global.npmrc"
    for path in (npmrc, global_npmrc):
        path.touch(mode=0o600)
    # npm verifies tarballs against package-lock integrity. Offline mode refuses
    # missing archives; scripts and Puppeteer's browser installation stay off.
    env = {"HOME": str(root), "PATH": "/usr/bin:/bin", "TMPDIR": str(root),
           "PUPPETEER_SKIP_DOWNLOAD": "1", "NPM_CONFIG_GLOBALCONFIG": str(global_npmrc)}
    subprocess.run(["/usr/bin/npm", "ci", "--prefix", str(root), "--cache", str(cache),
                    "--userconfig", str(npmrc), "--offline", "--ignore-scripts",
                    "--omit=dev", "--no-audit", "--no-fund"], env=env, check=True)
    shutil.copytree(browser, root / "chrome")
    receipt = {"offline": True, "lifecycle_scripts": False, "browser_download": False,
               "configured_doxa": False, "source_commit": subprocess.check_output(
                   ["git", "-C", str(HERE), "rev-parse", "HEAD"], text=True).strip(),
               "lock_sha256": hashlib.sha256((root / "package-lock.json").read_bytes()).hexdigest(),
               "browser_sha256": hashlib.sha256((root / "chrome/chrome").read_bytes()).hexdigest(),
               "browser_version": BROWSER_VERSION}
    (root / "setup-receipt.json").write_text(json.dumps(receipt, indent=2) + "\n")
    os.chmod(root / "setup-receipt.json", stat.S_IRUSR | stat.S_IWUSR)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True, help="new dedicated package root")
    parser.add_argument("--browser", type=Path, required=True, help="existing pinned chrome-linux64 directory")
    parser.add_argument("--npm-cache", type=Path, required=True, help="existing cache containing the locked archives")
    parser.add_argument("--provision", action="store_true", help="perform the reviewed offline copy/install; otherwise print plan")
    args = parser.parse_args()
    try:
        root, browser, cache, count = reviewed_inputs(args.root, args.browser, args.npm_cache)
        print(f"Pinned packages: {count}; Chrome: 131.0.6778.85; installed size: approximately 850 MiB")
        print("Uses installed /usr/bin/node and npm; no network, lifecycle scripts, browser download or DOXA config changes")
        print(f"New package root: {root}")
        if not args.provision:
            print("Plan only. Add --provision after reviewing the wrapper, config and lockfile.")
            return
        provision(root, browser, cache)
        print("Provisioned. Run validate.sh, then Settings → Appearance → Mermaid → Ctrl+P before saving.")
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        parser.exit(1, f"Mermaid setup unavailable: {error}\n")


if __name__ == "__main__":
    main()
