"""The SFTP drop against the real SFTP server image (docker-compose.yml's atmoz/sftp,
deploy/sftp/sshd_config and start.sh). Opt-in: needs Docker and the image, so it runs
only with DQ_SFTP_DOCKER_TRIAL=1.

Real here: the accounts made from manage sftp-add-folder's files, key-only login, the
chroot (an account sees only its own folders and cannot write outside upload/), the
upload, and the permission split between the client's account (10002) and the worker
(10001). The drop lives in a Docker volume with the owners and modes manage sets when
it runs as root (a host folder on Windows or macOS cannot give sshd its root-owned
chroot), and the worker's pickup runs in this process on the file exactly as it was
uploaded."""

import os
import subprocess
import time
from datetime import timedelta
from pathlib import Path

import pytest

from dqstack.app import filedrop, manage

from .test_owner_reports import SALES, _pos_company, _rice, _today

IMAGE = "atmoz/sftp:alpine@sha256:c5fb9e4beb8b7054a4c0b65c5e11df47cc20da95ad6002e88aecff62bab06372"
VOL, NAME, PORT = "dqstack-sftp-trial", "dqstack-sftp-trial", "2299"
REPO = Path(__file__).resolve().parents[2]


def sh(*cmd, check=True, input=None):
    r = subprocess.run(cmd, capture_output=True, text=True, input=input)
    print("$", " ".join(cmd[:6]), "...\n", r.stdout[-1500:], r.stderr[-1500:])
    if check:
        assert r.returncode == 0, r.stderr
    return r


def sftp(key, batch, check=True):
    return sh("sftp", "-i", str(key), "-P", PORT, "-o", "StrictHostKeyChecking=no",
              "-o", "UserKnownHostsFile=/dev/null", "-o", "BatchMode=yes", "-b", "-",
              "acme-pos@127.0.0.1", check=check, input=batch)


@pytest.mark.skipif(os.environ.get("DQ_SFTP_DOCKER_TRIAL") != "1",
                    reason="needs Docker and the atmoz/sftp image; set DQ_SFTP_DOCKER_TRIAL=1")
def test_real_sftp_server(make_client, tmp_path, monkeypatch):
    drop = tmp_path / "drop"
    drop.mkdir()
    monkeypatch.setenv("DQ_SFTP_ROOT", str(drop))
    monkeypatch.setenv("DQ_SFTP_SETTLE_SECONDS", "0")
    owner, cid, sid, pid, key = _pos_company(make_client)
    k = tmp_path / "client_key"
    sh("ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(k))
    pub = (tmp_path / "client_key.pub").read_text().strip()
    assert manage.main(["sftp-add-folder", "--folder", "acme-pos", "--api-key-id", key["id"],
                        "--public-key", pub]) == 0

    sh("docker", "rm", "-f", NAME, check=False)
    sh("docker", "volume", "rm", VOL, check=False)
    sh("docker", "volume", "create", VOL)
    try:
        src = drop.as_posix()
        sh("docker", "run", "--rm", "-v", f"{VOL}:/home", "-v", f"{src}:/src:ro",
           "--entrypoint", "sh", IMAGE, "-c", # What add_folder sets when manage runs as root on Linux (filedrop._own).
           "cp -a /src/. /home/ && cd /home && chown 0:0 /home && chmod 755 /home && "
           "chown -R 0:0 .dqstack acme-pos && "
           "chmod 755 .dqstack acme-pos acme-pos/.ssh acme-pos/.ssh/keys && "
           "chmod 644 .dqstack/* acme-pos/.ssh/keys/dqstack.pub && "
           "chown 10002:10001 acme-pos/upload && chmod 775 acme-pos/upload && "
           "for w in processing processed failed; do chown 10001:10001 acme-pos/$w; "
           "chmod 755 acme-pos/$w; done && ls -la /home/acme-pos /home/acme-pos/.ssh")
        sh("docker", "run", "-d", "--name", NAME, "-p", f"{PORT}:22", "-v", f"{VOL}:/home",
           "-v", f"{(REPO / 'deploy/sftp/sshd_config').as_posix()}:/etc/ssh/sshd_config:ro",
           "-v", f"{(REPO / 'deploy/sftp/start.sh').as_posix()}:/usr/local/bin/dq-sftp-start:ro",
           "--entrypoint", "/bin/sh", IMAGE, "/usr/local/bin/dq-sftp-start")
        for _ in range(30):
            if "Server listening" in sh("docker", "logs", NAME, check=False).stderr:
                break
            time.sleep(1)
        day = (_today() - timedelta(days=1)).isoformat()
        (tmp_path / "sales.csv").write_text(SALES.format(day=day), encoding="utf-8")
        os.chdir(tmp_path)

        # Key login lands in upload/, inside the account's own chroot.
        r = sftp(k, "pwd\nput sales.csv\nls -la\n", check=False)
        if r.returncode:
            print("SERVER LOG\n", sh("docker", "logs", NAME, check=False).stderr[-3000:])
            sh("docker", "exec", NAME, "ls", "-la", "/home/acme-pos", "/home/acme-pos/.ssh",
               check=False)
            raise AssertionError("login failed")
        assert "Remote working directory: /upload" in r.stdout
        # The chroot: / is the account's home, owned by root, not writable.
        r = sftp(k, "cd /\nls\nput sales.csv /evil.csv\n", check=False)
        assert r.returncode != 0
        assert "upload" in r.stdout and "drop" not in r.stdout and ".dqstack" not in r.stdout
        # Another key, or no key, is refused.
        k2 = tmp_path / "stranger"
        sh("ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(k2))
        assert sftp(k2, "ls\n", check=False).returncode != 0
        # Password login is not offered at all.
        r = sh("ssh", "-p", PORT, "-o", "StrictHostKeyChecking=no", "-o",
               "UserKnownHostsFile=/dev/null", "-o", "PreferredAuthentications=password",
               "-o", "BatchMode=yes", "acme-pos@127.0.0.1", check=False)
        assert r.returncode != 0 and "publickey" in r.stderr
        # Who owns the uploaded file inside the container: the account's own user id.
        own = sh("docker", "exec", NAME, "stat", "-c", "%u:%g %a %n",
                 "/home/acme-pos", "/home/acme-pos/upload", "/home/acme-pos/upload/sales.csv")
        assert own.stdout.splitlines()[0].startswith("0:0 755")
        assert "10002:" in own.stdout.splitlines()[2]

        # The permission split, as the real users: the client's account (10002) cannot
        # write where the worker writes; the worker (10001, the shared group) can move
        # the upload into its own folders.
        r = sh("docker", "exec", "--user", "10002:10001", NAME, "sh", "-c",
               "touch /home/acme-pos/processed/x", check=False)
        assert r.returncode != 0 and "Permission denied" in r.stderr
        r = sh("docker", "exec", "--user", "10002:10001", NAME, "sh", "-c",
               "rm -rf /home/acme-pos/processed && ln -s /etc /home/acme-pos/processed",
               check=False)
        assert r.returncode != 0
        sh("docker", "exec", "--user", "10001:10001", NAME, "sh", "-c",
           "cp /home/acme-pos/upload/sales.csv /tmp/s.csv && "
           "mv /home/acme-pos/upload/sales.csv /home/acme-pos/processing/sales.csv && "
           "mv /home/acme-pos/processing/sales.csv /home/acme-pos/processed/sales.csv && "
           "cp /tmp/s.csv /home/acme-pos/upload/sales.csv && ls -la /home/acme-pos/processed")

        # The worker's pickup, on the file exactly as it arrived.
        sh("docker", "cp", f"{NAME}:/home/acme-pos/upload/sales.csv",
           str(drop / "acme-pos" / "upload" / "sales.csv"))
        res = filedrop.pickup()
        assert [(x["outcome"], x["accepted"]) for x in res] == [("processed", 2)], res
        assert _rice(sid, pid) == 97_000
    finally:
        sh("docker", "rm", "-f", NAME, check=False)
        sh("docker", "volume", "rm", VOL, check=False)
