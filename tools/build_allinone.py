# -*- coding: utf-8 -*-
"""Rebuild the single-file QMT strategy without losing its config block.

``cp2server.bat`` syncs the multi-file deployment into the QMT python
directory, but it does not know about ``BIGQMT_REDIS_DRYRUN_ALL_IN_ONE.py``.
Regenerating that file with ``tools/build_single_file.py`` resets its config
block to ``YOUR_ACCOUNT_ID``, so a plain rebuild silently drops the account and
transport settings.

This script:
  1. optionally runs ``cp2server.bat`` (package files -> QMT python dir)
  2. rebuilds ``BigQmt/src/BIGQMT_REDIS_DRYRUN_ALL_IN_ONE.py``
  3. copies the config block from the previously deployed file over the new one
  4. compiles the result and deploys it to the QMT python dir

Usage:
    python D:\\delta43\\build_allinone.py
    python D:\\delta43\\build_allinone.py --no-sync
    python D:\\delta43\\build_allinone.py --no-deploy
"""

import argparse
import hashlib
import os
import shutil
import subprocess
import sys

REPO = r"D:\delta43\BigQmt"
BUILDER = os.path.join(REPO, "tools", "build_single_file.py")
SRC_OUT = os.path.join(REPO, "src", "BIGQMT_REDIS_DRYRUN_ALL_IN_ONE.py")
QMT_PYTHON = r"D:\Programs\国金证券QMT交易端\python"
DEPLOYED = os.path.join(QMT_PYTHON, "BIGQMT_REDIS_DRYRUN_ALL_IN_ONE.py")
SYNC_BAT = r"D:\delta43\BigQmt\tools\cp2server.bat"

START = "# =========================== config block ==========================="
END = "# ==================================================================="


def read_text(path):
    with open(path, "r", encoding="utf-8", newline="") as handle:
        return handle.read()


def write_text(path, text):
    with open(path, "w", encoding="utf-8", newline="") as handle:
        handle.write(text)


def extract_config(path):
    """Return (start, end, block) for the config section, or None."""
    text = read_text(path)
    start = text.find(START)
    if start < 0:
        return None
    end = text.find(END, start)
    if end < 0:
        return None
    end += len(END)
    return start, end, text[start:end]


def looks_qmt_encrypted(path):
    """QMT's editor can save a strategy as url-safe-base64 ciphertext."""
    with open(path, "rb") as handle:
        head = handle.read(4096)
    return bool(head) and b"#coding" not in head and head[:1] not in (b"#", b"\n", b"\r")


def resolve_config_source():
    """First readable config block wins; the deployed copy may be encrypted."""
    problems = []
    for path in (DEPLOYED, SRC_OUT):
        if not os.path.isfile(path):
            problems.append("%s (missing)" % path)
            continue
        found = extract_config(path)
        if found:
            return path, found
        reason = "QMT-encrypted, no config block" if looks_qmt_encrypted(path) \
            else "no config block"
        problems.append("%s (%s)" % (path, reason))
    raise SystemExit(
        "no readable config block found:\n  " + "\n  ".join(problems) + "\n"
        "configure BIGQMT_ACCOUNT_ID / BIGQMT_REDIS_CONFIG once by hand first.")


def run_sync():
    if not os.path.isfile(SYNC_BAT):
        raise SystemExit("sync script not found: %s" % SYNC_BAT)
    print("[1/4] syncing package -> %s" % QMT_PYTHON)
    # cp2server.bat ends with `pause`; an empty stdin keeps it non-blocking.
    completed = subprocess.run(
        ["cmd", "/c", SYNC_BAT],
        cwd=os.path.dirname(SYNC_BAT),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    output = completed.stdout.decode("utf-8", "replace")
    if completed.returncode != 0:
        sys.stderr.write(output)
        raise SystemExit("cp2server.bat failed (exit %d)" % completed.returncode)
    if "File(s) copied" not in output:
        sys.stderr.write(output)
        raise SystemExit("cp2server.bat finished but copied no files")
    print("      ok")


def run_build():
    print("[2/4] rebuilding %s" % SRC_OUT)
    completed = subprocess.run(
        [sys.executable, BUILDER],
        cwd=REPO,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    output = completed.stdout.decode("utf-8", "replace")
    if completed.returncode != 0:
        sys.stderr.write(output)
        raise SystemExit("build_single_file.py failed (exit %d)"
                         % completed.returncode)
    for line in output.splitlines():
        print("      " + line)


def restore_config(config_block):
    print("[3/4] restoring the current config block")
    text = read_text(SRC_OUT)
    try:
        start = text.index(START)
        end = text.index(END, start) + len(END)
    except ValueError:
        raise SystemExit("freshly built file has no config block markers")
    write_text(SRC_OUT, text[:start] + config_block + text[end:])


def verify_and_deploy(deploy):
    print("[4/4] verifying%s" % (" and deploying" if deploy else ""))
    with open(SRC_OUT, "rb") as handle:
        source = handle.read()
    compile(source, SRC_OUT, "exec")
    if not source.isascii():
        raise SystemExit("built file is not pure ASCII; QMT declares #coding:gbk")
    if deploy:
        os.makedirs(QMT_PYTHON, exist_ok=True)
        shutil.copy2(SRC_OUT, DEPLOYED)
        with open(DEPLOYED, "rb") as handle:
            deployed = handle.read()
        if deployed != source:
            raise SystemExit("deployed copy differs from the built file")
    digest = hashlib.sha256(source).hexdigest().upper()
    target = DEPLOYED if deploy else SRC_OUT
    print("      %s" % target)
    print("      size=%d sha256=%s" % (len(source), digest))


def main():
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--no-sync", action="store_true",
                        help="skip cp2server.bat")
    parser.add_argument("--no-deploy", action="store_true",
                        help="rebuild only; do not copy into the QMT python dir")
    args = parser.parse_args()

    config_source, found = resolve_config_source()
    config_block = found[2]
    print("config source: %s" % config_source)
    if config_source != DEPLOYED:
        print("note: deployed copy is not a readable plain script; "
              "using the last local build's config block")

    if not args.no_sync:
        run_sync()
    else:
        print("[1/4] sync skipped")
    run_build()
    restore_config(config_block)
    verify_and_deploy(not args.no_deploy)
    print("done.")


if __name__ == "__main__":
    main()
