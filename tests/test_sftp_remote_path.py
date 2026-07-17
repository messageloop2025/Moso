import stat

from services.sftp_transfer import expand_sftp_tilde, resolve_remote_push_target


class _FakeStat:
    def __init__(self, *, is_dir: bool):
        self.st_mode = stat.S_IFDIR if is_dir else stat.S_IFREG


class FakeSftp:
    home = "/home/deploy"

    def normalize(self, path: str) -> str:
        if path == ".":
            return self.home
        return path

    def __init__(self, dirs=None):
        self.dirs = set(dirs or [])

    def stat(self, path: str):
        if path in self.dirs:
            return _FakeStat(is_dir=True)
        raise FileNotFoundError(path)


def test_expand_sftp_tilde_home_subdir():
    sftp = FakeSftp()
    assert expand_sftp_tilde(sftp, "~/moss/") == "/home/deploy/moss/"
    assert expand_sftp_tilde(sftp, "~") == "/home/deploy"
    assert expand_sftp_tilde(sftp, "/tmp/x") == "/tmp/x"


def test_resolve_remote_push_target_dir_trailing_slash():
    sftp = FakeSftp()
    assert (
        resolve_remote_push_target(sftp, "~/moss/", "moss-v1.7.8.tgz")
        == "/home/deploy/moss/moss-v1.7.8.tgz"
    )


def test_resolve_remote_push_target_existing_dir():
    sftp = FakeSftp(dirs={"/home/deploy/moss"})
    assert (
        resolve_remote_push_target(sftp, "/home/deploy/moss", "pkg.tgz")
        == "/home/deploy/moss/pkg.tgz"
    )


def test_resolve_remote_push_target_full_file_path():
    sftp = FakeSftp()
    assert (
        resolve_remote_push_target(sftp, "/tmp/pkg.tgz", "local.tgz")
        == "/tmp/pkg.tgz"
    )
