"""Content-addressed blob store. Part of the trust core (v0.2 build order step 1):
caps + blob hashes + skipped metadata shape the snapshot itself, so this is not a
late add-on. Keyed by git blob OID so it dedups against git's own naming."""
import os
import shutil

DEFAULT_MAX_FILE = 5 * 1024 * 1024  # 5 MiB; over cap -> skipped_size, non-verifying


class CAS:
    def __init__(self, root, max_file=DEFAULT_MAX_FILE):
        self.root = root
        self.max_file = max_file
        os.makedirs(root, exist_ok=True)

    def _path(self, oid):
        return os.path.join(self.root, oid[:2], oid[2:])

    def has(self, oid):
        return os.path.exists(self._path(oid))

    def put_file(self, src, oid):
        """Store file bytes under oid. Returns True if stored, False if over the size cap."""
        if os.path.getsize(src) > self.max_file:
            return False
        dst = self._path(oid)
        if os.path.exists(dst):  # dedup: already have this blob
            return True
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        tmp = dst + ".tmp"
        shutil.copyfile(src, tmp)
        os.replace(tmp, dst)
        return True

    def put_bytes(self, data, oid):
        dst = self._path(oid)
        if os.path.exists(dst):
            return True
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        tmp = dst + ".tmp"
        with open(tmp, "wb") as f:
            f.write(data)
        os.replace(tmp, dst)
        return True

    def open(self, oid):
        return open(self._path(oid), "rb")
