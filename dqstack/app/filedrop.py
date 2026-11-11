"""The nightly file drop by SFTP (section 14: "A nightly file drop (SFTP or email-in) for
systems that cannot call an API"). Email-in is not built.

A billing system that cannot call the POS API uploads the same CSV that
POST /api/pos/v1/files takes, over SFTP, into its own folder. The worker looks every
half-minute and records each finished file through the same code as the API
(routes/pos.py `process_file`: the same parsing, the same per-bill repeat protection,
the same activity-log entries for every bill), then moves it aside with a report:

    <root>/<folder>/              the account's chroot: owned by root, nobody else writes
    <root>/<folder>/upload/       where the client puts files (the account's own)
    <root>/<folder>/processing/   claimed and being recorded           } the worker's;
    <root>/<folder>/processed/    every bill recorded, now or before    } the client can
    <root>/<folder>/failed/       something not recorded; <file>.errors.txt says what }  read

A failed file raises a "Nightly file not recorded" alert for the company's Owners and
Admins. Sending the corrected file again is safe: bills already recorded are recognised
by type, store and bill number and are not counted twice.

Who owns what matters, because a client can make symbolic links in its own folder. The
SFTP account has a user id of its own (DQ_SFTP_UID, 10002) in the worker's group
(DQ_SFTP_GID, 10001): the worker can read and move what is uploaded, but the client
cannot write where the worker writes. Were they the same user, a client could swap
processed/ for a link into another company's folder and have the worker deliver its
file there.

A folder's name is not authority. Only folders an operator has added with

    python -m dqstack.app.manage sftp-add-folder --folder NAME --api-key-id ID --public-key "ssh-ed25519 ..."

are read, and each acts as the API key it names: that key's company, that key's store
when the key is limited to one, nothing else. Revoking the key under Settings >
Integrations stops the folder on the next look. The mapping is <root>/.dqstack/folders.json,
outside every SFTP account's chroot; it also writes <root>/.dqstack/users.conf for the
SFTP server (atmoz/sftp in docker-compose.yml, key-based login only, one chrooted account
per folder).

A file is read only once nobody has written to it for DQ_SFTP_SETTLE_SECONDS (60), and
names starting with "." or ending in .part, .tmp or .filepart are left alone, so a file
still uploading is never read. Symbolic links are never followed: a chrooted account
could otherwise make one that points into another company's folder.

    DQ_SFTP_ROOT              the drop's root folder; unset, the file drop is off
    DQ_SFTP_SETTLE_SECONDS    quiet time before a file is read (60)
    DQ_SFTP_EVERY_SECONDS     how often the worker looks (30)
    DQ_SFTP_UID               the SFTP accounts' user id (10002)
    DQ_SFTP_GID               the group they share with the worker: the worker's (10001)
    DQ_SFTP_WORKER_UID        the worker's user id (10001, the image's dqstack user)
"""

from __future__ import annotations

import csv
import io
import json
import logging
import os
import re
import stat
import threading
import time
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

from sqlalchemy import select

from . import audit
from .db import SessionLocal, utcnow
from .formats import ist
from .models import Alert, ApiKey, Company

log = logging.getLogger("dqstack.filedrop")

FOLDER_RE = re.compile(r"[a-z][a-z0-9-]{2,31}")
# Linux accounts the SFTP container (Alpine) or a server usually has already.
RESERVED = {"root", "bin", "daemon", "adm", "lp", "sync", "shutdown", "halt", "mail", "news",
            "uucp", "operator", "man", "postmaster", "cron", "ftp", "sshd", "at", "squid",
            "xfs", "games", "cyrus", "vpopmail", "ntp", "smmsp", "guest", "nobody", "www-data",
            "backup", "list", "irc", "gnats", "proxy", "postgres", "dqstack", "sftp", "users"}
KEY_RE = re.compile(r"(ssh-ed25519|ssh-rsa|ecdsa-sha2-nistp(?:256|384|521)|"
                    r"sk-ssh-ed25519@openssh\.com|sk-ecdsa-sha2-nistp256@openssh\.com) "
                    r"[A-Za-z0-9+/]+={0,3}( [^\r\n]{0,200})?")
SKIP_SUFFIXES = (".part", ".tmp", ".filepart", ".partial", ".crdownload")
# The columns a file must name for its rows to become bills (see routes/pos.py).
NEEDED = ("store_code", "bill_number")
COLUMNS = ("type, store_code, bill_number, occurred_at, sku, qty, price, discount, "
           "payment_method, original_bill_number")
STAMP_RE = re.compile(r"\d{8}-\d{6}-")

_busy = threading.Lock()


class FolderError(ValueError):
    """A problem with adding or removing a folder. The message is for the operator."""


class _Problem(Exception):
    pass


def root() -> Path | None:
    r = (os.environ.get("DQ_SFTP_ROOT") or "").strip()
    return Path(r) if r else None


def _seconds(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name) or default)
    except ValueError:
        return default


# ===================================================================== folders

def _config(r: Path) -> Path:
    return r / ".dqstack"


def load_folders(r: Path) -> dict[str, dict]:
    """The operator's folder -> API key mapping. Anything malformed is ignored."""
    try:
        data = json.loads((_config(r) / "folders.json").read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as e:
        log.error("SFTP folder list %s could not be read: %s", _config(r) / "folders.json", e)
        return {}
    folders = data.get("folders") if isinstance(data, dict) else None
    return {k: v for k, v in (folders or {}).items()
            if isinstance(k, str) and FOLDER_RE.fullmatch(k) and isinstance(v, dict)}


def _write(path: Path, text: str, mode: int = 0o644) -> None:
    """Replace a file in one step, so a reader never sees half of it."""
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_text(text, encoding="utf-8", newline="\n")
    os.chmod(tmp, mode)
    os.replace(tmp, path)


def _as_root() -> bool:
    return hasattr(os, "geteuid") and os.geteuid() == 0


def _own(path: Path, uid: int, gid: int, mode: int) -> None:
    os.chmod(path, mode)
    if _as_root():
        os.chown(path, uid, gid)


def _save(r: Path, folders: dict[str, dict], uid: int, gid: int) -> None:
    cfg = _config(r)
    cfg.mkdir(parents=True, exist_ok=True)
    _own(cfg, 0, 0, 0o755)
    _write(cfg / "folders.json", json.dumps({"folders": folders}, indent=2, sort_keys=True) + "\n")
    # atmoz/sftp's users.conf: name:password:uid:gid:dirs. No password: key login only.
    lines = ["# Made by python -m dqstack.app.manage sftp-add-folder. Do not edit by hand."]
    lines += [f"{name}::{uid}:{gid}:upload" for name in sorted(folders)]
    _write(cfg / "users.conf", "\n".join(lines) + "\n")


def _ids() -> tuple[int, int, int]:
    """(the accounts' user id, the shared group, the worker's user id)."""
    env = lambda name, default: int(os.environ.get(name) or default)  # noqa: E731
    return env("DQ_SFTP_UID", 10002), env("DQ_SFTP_GID", 10001), env("DQ_SFTP_WORKER_UID", 10001)


WORK = ("processing", "processed", "failed")


def add_folder(r: Path, folder: str, api_key_id: str, public_key: str) -> dict:
    """Map a new SFTP folder to an API key and let one SSH key log in to it."""
    folder = (folder or "").strip()
    if not FOLDER_RE.fullmatch(folder) or folder in RESERVED:
        raise FolderError("A folder name is 3 to 32 lower-case letters, digits and hyphens, "
                          "starting with a letter, and not a system account name.")
    key_line = " ".join((public_key or "").split())
    if not KEY_RE.fullmatch(key_line):
        raise FolderError("That is not an OpenSSH public key (one line, starting ssh-ed25519, "
                          "ssh-rsa or ecdsa-sha2-...). Passwords are not accepted.")
    folders = load_folders(r)
    if folder in folders:
        raise FolderError(f"The folder {folder} already exists. Remove it first to change it.")
    uid, gid, worker = _ids()
    if uid == worker:
        raise FolderError("DQ_SFTP_UID must differ from the worker's user id, or a client "
                          "could write where the worker writes.")
    ident = (api_key_id or "").strip().rstrip(".…")
    with SessionLocal() as db:
        k = db.get(ApiKey, ident)
        if k is None and ident.startswith("dqk_"):
            # The beginning Settings > Integrations shows ("Starts with dqk_...").
            same = list(db.scalars(select(ApiKey).where(ApiKey.prefix == ident,
                                                        ApiKey.revoked_at.is_(None))))
            if len(same) > 1:
                raise FolderError("More than one key starts that way; give the key's id.")
            k = same[0] if same else None
        if k is None:
            raise FolderError("No API key has that id. Make one under Settings > Integrations; "
                              "give its id, or the dqk_... beginning the screen shows.")
        if k.revoked_at is not None:
            raise FolderError(f"The API key {k.prefix}... has been revoked.")
        co = db.get(Company, k.company_id)
        if co is None or co.status != "active":
            raise FolderError("That key's company is not active.")
        home = r / folder
        for d in (home, home / ".ssh", home / ".ssh" / "keys", home / "upload",
                  *(home / w for w in WORK)):
            d.mkdir(parents=True, exist_ok=True)
        # sshd's chroot needs the home folder owned by root and writable by nobody else.
        # upload/ is the account's, and its group (the worker's) may move files out of it;
        # the worker's folders are the worker's, which the account can only read.
        for d in (home, home / ".ssh", home / ".ssh" / "keys"):
            _own(d, 0, 0, 0o755)
        _own(home / "upload", uid, gid, 0o775)
        for w in WORK:
            _own(home / w, worker, gid, 0o755)
        _write(home / ".ssh" / "keys" / "dqstack.pub", key_line + "\n")
        _own(home / ".ssh" / "keys" / "dqstack.pub", 0, 0, 0o644)
        folders[folder] = {"api_key_id": k.id, "company_id": k.company_id,
                           "key_prefix": k.prefix, "added_at": utcnow().isoformat()}
        _save(r, folders, uid, gid)
        audit.log(db, SimpleNamespace(company_id=k.company_id, person=None, device="manage",
                                      ip=None),
                  "integration.sftp_folder_added", "api_key", k.id,
                  after={"folder": folder, "key_prefix": k.prefix, "store_id": k.store_id})
        db.commit()
        return {"folder": folder, "company": co.name, "key_name": k.name, "key_prefix": k.prefix,
                "store_id": k.store_id}


def remove_folder(r: Path, folder: str) -> bool:
    """Stop reading a folder and stop its account logging in (once the SFTP service is
    recreated). Its files are left where they are."""
    folders = load_folders(r)
    entry = folders.pop(folder, None)
    if entry is None:
        return False
    uid, gid, _ = _ids()
    _save(r, folders, uid, gid)
    with SessionLocal() as db:
        k = db.get(ApiKey, str(entry.get("api_key_id") or ""))
        if k is not None:
            audit.log(db, SimpleNamespace(company_id=k.company_id, person=None, device="manage",
                                          ip=None),
                      "integration.sftp_folder_removed", "api_key", k.id, after={"folder": folder})
            db.commit()
    return True


# ===================================================================== pickup

def _real_dir(p: Path) -> bool:
    try:
        return stat.S_ISDIR(os.lstat(p).st_mode)
    except OSError:
        return False


def _files(d: Path, settle: float) -> list[Path]:
    """Plain files in `d` that have been left alone for `settle` seconds, by name."""
    try:
        entries = sorted(os.scandir(d), key=lambda e: e.name)
    except (FileNotFoundError, NotADirectoryError):
        return []
    now = time.time()
    out = []
    for e in entries:
        if e.name.startswith(".") or e.name.lower().endswith(SKIP_SUFFIXES):
            continue
        try:
            st = e.stat(follow_symlinks=False)
        except OSError:
            continue
        if not stat.S_ISREG(st.st_mode):
            continue                      # folders, and links, which are never followed
        if now - st.st_mtime < settle:
            continue                      # may still be uploading
        out.append(Path(e.path))
    return out


def _read(path: Path) -> bytes:
    from .routes.pos import FILE_LIMIT
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0)
    try:
        fd = os.open(path, flags)
    except PermissionError:
        raise _Problem("The file could not be read: it was uploaded readable by its owner "
                       "only. Upload it with read permission for others (mode 644).") from None
    except OSError as e:
        raise _Problem(f"The file could not be opened ({e.__class__.__name__}).") from None
    with os.fdopen(fd, "rb") as f:
        if not stat.S_ISREG(os.fstat(f.fileno()).st_mode):
            raise _Problem("This is not a plain file.")
        data = f.read(FILE_LIMIT + 1)
    if len(data) > FILE_LIMIT:
        raise _Problem("The file is larger than 25 MB. Split it into smaller files.")
    return data


def _header_problem(data: bytes) -> str | None:
    text = data.decode("utf-8-sig", errors="replace")
    first = next(csv.reader(io.StringIO(text.split("\n", 1)[0])), [])
    have = {c.strip().lower() for c in first}
    missing = [c for c in NEEDED if c not in have]
    if not text.strip():
        return "The file is empty."
    if missing:
        return (f"The first line must name the columns ({COLUMNS}); it has no "
                f"{' or '.join(missing)} column.")
    return None


def _key(db, entry: dict) -> tuple[ApiKey | None, str | None]:
    k = db.get(ApiKey, str(entry.get("api_key_id") or ""))
    if k is None or k.company_id != entry.get("company_id"):
        return None, ("This folder's API key no longer exists. An operator has to add the "
                      "folder again with a current key.")
    if k.revoked_at is not None:
        return k, (f"This folder's API key ({k.prefix}...) has been revoked, so its files are "
                   "no longer recorded. Make a new key under Settings > Integrations and ask "
                   "your operator to point the folder at it.")
    co = db.get(Company, k.company_id)
    if co is None or co.status != "active":
        return k, "This company is not active, so its files are not recorded."
    return k, None


def _report(folder: str, original: str, res: dict, refused: list, when: datetime) -> str:
    lines = ["DQ-Stack did not record all of this file." if res["error"]
             else "DQ-Stack recorded this file.", "",
             f"File:      {original}", f"Folder:    {folder}", f"Looked at: {ist(when)}"]
    if res["bills"]:
        lines.append(f"Bills:     {res['bills']} in the file: {res['accepted']} recorded now, "
                     f"{res['duplicates']} recorded before, {res['refused']} refused.")
    if res["error"]:
        lines += ["", "What went wrong: " + res["error"]]
    if refused:
        lines += ["", "Refused bills:"]
        for r in refused:
            kind, store, bill = (str(r.get("id") or "").split(":", 3)[1:] + ["", "", ""])[:3]
            lines.append(f"  {kind} {store} bill {bill}: {r.get('error')}")
        if res["refused"] > len(refused):
            lines.append(f"  and {res['refused'] - len(refused)} more.")
    if res["error"]:
        lines += ["", "Correct the file and upload all of it again. Bills already recorded "
                      "are recognised by type, store and bill number and are not counted twice."]
    return "\n".join(lines) + "\n"


def _alert(db, company_id: str, folder: str, original: str, stored: str, problem: str) -> None:
    dedupe = f"file_drop_failed:{folder}:{stored}"[:200]
    if db.scalar(select(Alert.id).where(Alert.company_id == company_id,
                                        Alert.dedupe_key == dedupe)):
        return
    db.add(Alert(company_id=company_id, kind="file_drop_failed", severity="risk",
                 message=(f"The nightly file {original} in SFTP folder {folder} was not fully "
                          f"recorded. {problem} The file and the list of what went wrong are "
                          f"in the folder's failed/ folder.")[:500],
                 dedupe_key=dedupe, link="#/settings/integrations"))


def _record(folder: str, entry: dict, claimed: Path) -> dict:
    """Record one claimed file, move it to processed/ or failed/ with its report, and
    note it in the company's activity log."""
    from .routes.pos import key_ctx, process_file
    stored = claimed.name
    original = stored[16:] if STAMP_RE.match(stored) else stored
    res = {"folder": folder, "file": original, "stored_as": stored, "outcome": "failed",
           "bills": 0, "accepted": 0, "duplicates": 0, "refused": 0, "error": None}
    refused: list = []
    with SessionLocal() as db:
        k, problem = _key(db, entry)
        try:
            if problem is None:
                data = _read(claimed)
                problem = _header_problem(data)
            if problem is None:
                out = process_file(db, key_ctx(k, f"sftp:{k.prefix}"), data)
                refused = out["refused"]
                res.update(bills=out["bills"], accepted=out["accepted"],
                           duplicates=out["duplicates"],
                           refused=out["bills"] - out["accepted"] - out["duplicates"])
                if not out["bills"]:
                    problem = "The file has no bills in it."
                elif res["refused"]:
                    problem = f"{res['refused']} of {out['bills']} bills were refused."
        except _Problem as e:
            problem = str(e)
        except Exception:  # noqa: BLE001 -- one bad file must not stop the folder
            db.rollback()
            log.exception("SFTP file %s/%s could not be recorded", folder, stored)
            problem = ("Something went wrong while recording this file; the details are in "
                       "the server's log.")
        res["error"] = problem
        res["outcome"] = "failed" if problem else "processed"
        dest = claimed.parent.parent / res["outcome"]      # checked by _folder: a real folder
        when = utcnow()
        try:
            os.replace(claimed, dest / stored)
            (dest / (stored + (".errors.txt" if problem else ".report.txt"))).write_text(
                _report(folder, original, res, refused, when), encoding="utf-8", newline="\n")
        except OSError:
            log.exception("SFTP file %s/%s could not be moved to %s", folder, stored, dest)
        if k is not None:
            kc = key_ctx(k, f"sftp:{k.prefix}")
            audit.log(db, kc, "pos.file_received", "api_key", k.id, after={
                "source": "sftp", "folder": folder, "file": original,
                "stored_as": f"{res['outcome']}/{stored}", "bills": res["bills"],
                "accepted": res["accepted"], "duplicates": res["duplicates"],
                "refused": res["refused"], "error": problem})
            if problem:
                _alert(db, k.company_id, folder, original, stored, problem)
            db.commit()
        elif problem:
            log.error("SFTP folder %s: %s (%s)", folder, problem, stored)
    return res


def _folder(r: Path, folder: str, entry: dict, settle: float) -> list[dict]:
    home, upload = r / folder, r / folder / "upload"
    if not (_real_dir(home) and _real_dir(upload)):
        return []
    for w in WORK:
        # Made by sftp-add-folder, owned by the worker; never a link (a link would be
        # followed when a file is moved into it).
        if not _real_dir(home / w):
            try:
                os.mkdir(home / w)
            except OSError:
                log.error("SFTP folder %s has no %s/ folder the worker can use; run "
                          "sftp-add-folder for it again", folder, w)
                return []
    processing = home / "processing"
    out = []
    # A pass that stopped half way left these claimed; recording again is safe.
    for p in _files(processing, 0):
        out.append(_record(folder, entry, p))
    for p in _files(upload, settle):
        stamped = f"{utcnow():%Y%m%d-%H%M%S}-{p.name}"
        try:
            os.replace(p, processing / stamped)
        except OSError:
            log.exception("SFTP file %s/%s could not be claimed", folder, p.name)
            continue
        out.append(_record(folder, entry, processing / stamped))
    return out


def pickup(r: Path | None = None, settle: float | None = None) -> list[dict]:
    """One look at every folder. Returns what happened to each file. Never runs twice
    at once in one process."""
    r = r or root()
    if r is None or not _real_dir(r):
        return []
    if not _busy.acquire(blocking=False):
        return []
    try:
        settle = _seconds("DQ_SFTP_SETTLE_SECONDS", 60) if settle is None else settle
        out = []
        for folder, entry in sorted(load_folders(r).items()):
            try:
                out += _folder(r, folder, entry, settle)
            except Exception:  # noqa: BLE001 -- one folder's trouble never stops the rest
                log.exception("SFTP folder %s could not be read", folder)
        for res in out:
            log.info("SFTP %s/%s: %s, %s bills, %s new, %s refused", res["folder"], res["file"],
                     res["outcome"], res["bills"], res["accepted"], res["refused"])
        return out
    finally:
        _busy.release()


def start() -> threading.Thread | None:
    """Look every DQ_SFTP_EVERY_SECONDS in a thread of its own, so a long training run
    in the scheduler does not hold up the night's files. Off without DQ_SFTP_ROOT."""
    if root() is None:
        return None
    every = max(_seconds("DQ_SFTP_EVERY_SECONDS", 30), 1.0)

    def loop() -> None:
        while True:
            try:
                pickup()
            except Exception:  # noqa: BLE001 -- the loop itself must keep going
                log.exception("file drop pass failed")
            time.sleep(every)
    t = threading.Thread(target=loop, name="dq-file-drop", daemon=True)
    t.start()
    log.info("SFTP file drop: looking in %s every %ss", root(), every)
    return t
