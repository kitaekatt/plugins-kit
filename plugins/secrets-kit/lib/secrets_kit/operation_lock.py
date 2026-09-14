"""Cooperative nonblocking ownership of one physical plugin data directory.

The persistent empty file is never deleted or replaced. No lease or payload
is stored. Native filesystem acceptance is separate from acquiring a lock;
external editors, raw fork transfer and hostile path replacement are outside
this operation boundary.
"""

from contextlib import contextmanager
import errno
import os
from pathlib import Path
import stat
import sys
from typing import Iterator

from . import SecretsError

IS_WINDOWS = sys.platform.startswith("win")


class OperationLockError(SecretsError):
    """Operation ownership could not be acquired or safely released."""


class OperationBusyError(OperationLockError):
    """Another operation owns this physical data identity."""


class OperationLockSetupError(OperationLockError):
    """The local guard's setup, identity or kernel operation is unavailable."""


def _setup_error(path: Path, cause: str) -> OperationLockSetupError:
    return OperationLockSetupError(
        f"operation lock unavailable for {path}: {cause}",
        "Inspect this data directory's ownership, permissions and local filesystem "
        "support, correct the diagnosed setup and retry. Do not delete the guard file.",
    )


def _release_error(path: Path, cause: str) -> OperationLockSetupError:
    return OperationLockSetupError(
        f"operation lock release failed for {path}: {cause}",
        "Protected work may already have completed. Inspect the operation results "
        "before retrying and diagnose the release failure. Do not delete the guard file.",
    )


def _windows_private(path: Path, *, directory: bool) -> None:
    """Accept a current-token-owned, ordinary owner-only DACL on fixed NTFS.

    Unknown ACE forms, deny rules and foreign grants refuse rather than infer
    effective private access. Actual open/create supplies the required access
    check. This inspection never changes existing ACLs or initializes bytes.
    Native acceptance must exercise these calls and empty-file range locking.
    """
    import ctypes
    from ctypes import wintypes

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    advapi = ctypes.WinDLL("advapi32", use_last_error=True)
    pointer = ctypes.c_void_p
    ppointer = ctypes.POINTER(pointer)
    dword = wintypes.DWORD
    kernel.GetCurrentProcess.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.CloseHandle.restype = wintypes.BOOL
    kernel.LocalFree.argtypes = [pointer]
    kernel.LocalFree.restype = pointer
    kernel.GetVolumePathNameW.argtypes = [wintypes.LPCWSTR, wintypes.LPWSTR, dword]
    kernel.GetVolumePathNameW.restype = wintypes.BOOL
    kernel.GetDriveTypeW.argtypes = [wintypes.LPCWSTR]
    kernel.GetDriveTypeW.restype = wintypes.UINT
    kernel.GetVolumeInformationW.argtypes = [wintypes.LPCWSTR, wintypes.LPWSTR, dword, ctypes.POINTER(dword), ctypes.POINTER(dword), ctypes.POINTER(dword), wintypes.LPWSTR, dword]
    kernel.GetVolumeInformationW.restype = wintypes.BOOL
    advapi.OpenProcessToken.argtypes = [wintypes.HANDLE, dword, ctypes.POINTER(wintypes.HANDLE)]
    advapi.OpenProcessToken.restype = wintypes.BOOL
    advapi.GetTokenInformation.argtypes = [wintypes.HANDLE, ctypes.c_int, pointer, dword, ctypes.POINTER(dword)]
    advapi.GetTokenInformation.restype = wintypes.BOOL
    advapi.GetNamedSecurityInfoW.argtypes = [wintypes.LPWSTR, ctypes.c_int, dword, ppointer, ppointer, ppointer, ppointer, ppointer]
    advapi.GetNamedSecurityInfoW.restype = dword
    for name in ["IsValidSid", "IsValidAcl"]:
        function = getattr(advapi, name)
        function.argtypes = [pointer];function.restype = wintypes.BOOL
    advapi.EqualSid.argtypes = [pointer, pointer];advapi.EqualSid.restype = wintypes.BOOL
    advapi.GetAce.argtypes = [pointer, dword, ppointer];advapi.GetAce.restype = wintypes.BOOL

    volume = ctypes.create_unicode_buffer(32768)
    filesystem = ctypes.create_unicode_buffer(64)
    if not kernel.GetVolumePathNameW(str(path), volume, len(volume)) or kernel.GetDriveTypeW(volume.value) != 3:
        raise _setup_error(path, "fixed local volume identity cannot be established")
    if not kernel.GetVolumeInformationW(volume.value, None, 0, None, None, None, filesystem, len(filesystem)) or filesystem.value.upper() != "NTFS":
        raise _setup_error(path, "only locally identified NTFS is supported by this Windows guard")

    token = wintypes.HANDLE()
    descriptor = pointer()
    try:
        if not advapi.OpenProcessToken(kernel.GetCurrentProcess(), 8, ctypes.byref(token)):
            raise _setup_error(path, "current token identity cannot be inspected")
        needed = dword()
        advapi.GetTokenInformation(token, 1, None, 0, ctypes.byref(needed))
        if needed.value < ctypes.sizeof(pointer):
            raise _setup_error(path, "current token user identity is unavailable")
        user = ctypes.create_string_buffer(needed.value)
        if not advapi.GetTokenInformation(token, 1, user, needed, ctypes.byref(needed)):
            raise _setup_error(path, "current token user query failed")
        user_sid = ctypes.cast(user, ppointer).contents.value
        owner, dacl = pointer(), pointer()
        code = advapi.GetNamedSecurityInfoW(str(path), 1, 5, ctypes.byref(owner), None, ctypes.byref(dacl), None, ctypes.byref(descriptor))
        if code or not owner or not dacl or not advapi.IsValidSid(user_sid) or not advapi.IsValidSid(owner) or not advapi.EqualSid(owner, user_sid) or not advapi.IsValidAcl(dacl):
            raise _setup_error(path, "current-user ownership and restrictive DACL cannot be established")

        class ACL(ctypes.Structure):
            _fields_ = [("revision", ctypes.c_ubyte), ("reserved", ctypes.c_ubyte), ("size", ctypes.c_ushort), ("count", ctypes.c_ushort), ("reserved2", ctypes.c_ushort)]

        acl = ctypes.cast(dacl, ctypes.POINTER(ACL)).contents
        granted = 0
        for index in range(acl.count):
            ace = pointer()
            if not advapi.GetAce(dacl, index, ctypes.byref(ace)) or not ace:
                raise _setup_error(path, "DACL entry cannot be inspected")
            header = ctypes.string_at(ace, 4)
            size = int.from_bytes(header[2:4], "little")
            if header[0] != 0 or size < 12:
                raise _setup_error(path, "DACL has an unsupported or denying entry")
            mask = ctypes.c_uint32.from_address(ace.value + 4).value
            sid = ace.value + 8
            if not advapi.IsValidSid(sid):
                raise _setup_error(path, "DACL entry identity is invalid")
            if header[1] & 8:
                continue
            if mask and not advapi.EqualSid(sid, user_sid):
                raise _setup_error(path, "DACL grants access to a different principal")
            if mask & 0x10000000:mask |= 0x1F01FF
            if mask & 0x80000000:mask |= 0x120089
            if mask & 0x40000000:mask |= 0x120116
            if mask & 0x20000000:mask |= 0x1200A0
            granted |= mask
        required = 0x27 if directory else 3
        if granted & required != required:
            raise _setup_error(path, "DACL does not establish required owner access")
    finally:
        if descriptor:kernel.LocalFree(descriptor)
        if token:kernel.CloseHandle(token)


def _directory_trust(path: Path) -> os.stat_result:
    observed = path.stat()
    if not stat.S_ISDIR(observed.st_mode) or observed.st_ino <= 0:
        raise _setup_error(path, "physical directory identity is unavailable")
    if IS_WINDOWS:
        _windows_private(path, directory=True)
    elif observed.st_uid != os.geteuid() or observed.st_mode & 0o022:
        raise _setup_error(path, "data directory has unsuitable ownership or write permissions")
    return observed


def _resolve(path: Path) -> Path:
    try:
        return path.resolve(strict=True)
    except RuntimeError as error:
        raise _setup_error(path, "physical path resolution loop") from error


def _canonical_directory(data_dir: Path) -> Path:
    try:
        path = data_dir.expanduser()
    except RuntimeError as error:
        raise _setup_error(data_dir, "configured home identity cannot be resolved") from error
    try:
        canonical = _resolve(path)
    except FileNotFoundError:
        missing = []
        ancestor = path
        while True:
            try:
                ancestor.lstat()
                break
            except FileNotFoundError:
                missing.append(ancestor.name)
                parent = ancestor.parent
                if parent == ancestor:raise _setup_error(path, "no existing directory ancestor")
                ancestor = parent
        canonical = _resolve(ancestor)
        if not canonical.is_dir():raise _setup_error(path, "ancestor is not a directory")
        for component in reversed(missing):
            if IS_WINDOWS:_directory_trust(canonical)
            candidate = canonical / component
            try:candidate.mkdir(mode=0o700)
            except FileExistsError:pass
            canonical = _resolve(candidate)
            _directory_trust(canonical)
    _directory_trust(canonical)
    return canonical


def _guard_identity(path: Path, fd: int) -> os.stat_result:
    leaf, opened = path.lstat(), os.fstat(fd)
    if not stat.S_ISREG(leaf.st_mode) or not stat.S_ISREG(opened.st_mode) or leaf.st_nlink != 1 or opened.st_nlink != 1 or opened.st_size or opened.st_ino <= 0 or (leaf.st_dev, leaf.st_ino) != (opened.st_dev, opened.st_ino) or getattr(leaf, "st_file_attributes", 0) & 0x400:
        raise _setup_error(path.parent, "guard is not one stable regular file")
    if IS_WINDOWS:
        _windows_private(path, directory=False)
    elif opened.st_uid != os.geteuid() or stat.S_IMODE(opened.st_mode) != 0o600:
        raise _setup_error(path.parent, "guard has unsuitable ownership or permissions")
    return opened


def _acquire(fd: int, path: Path) -> None:
    if IS_WINDOWS:
        import msvcrt
        os.lseek(fd, 0, os.SEEK_SET)
    else:
        import fcntl
    try:
        if IS_WINDOWS:
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
        else:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as error:
        conflicts = (errno.EACCES, errno.EAGAIN, errno.EWOULDBLOCK)
        native_code = getattr(error, "winerror", None)
        busy = native_code == 33 if IS_WINDOWS and native_code is not None else error.errno in conflicts or (IS_WINDOWS and error.errno == errno.EDEADLK)
        if busy:
            raise OperationBusyError(
                f"another secrets operation owns the operation lock for {path}",
                "Retry after the active secrets operation finishes. Do not delete the guard file.",
            ) from error
        raise _setup_error(path, f"kernel acquisition failed (errno {error.errno})") from error


def _unlock(fd: int) -> None:
    if IS_WINDOWS:
        import msvcrt
        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
    else:
        import fcntl
        fcntl.flock(fd, fcntl.LOCK_UN)


@contextmanager
def operation_lock(data_dir: Path) -> Iterator[Path]:
    """Yield canonical paths under one non-reentrant operation ownership unit."""
    fd = None
    acquired = False
    try:
        try:
            canonical = _canonical_directory(data_dir)
            directory = canonical.stat()
            path = canonical / "operation.lock"
            flags = os.O_RDWR | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0)
            expected_leaf = None
            try:fd = os.open(path, flags | os.O_CREAT | os.O_EXCL, 0o600)
            except FileExistsError:
                leaf = path.lstat()
                if not stat.S_ISREG(leaf.st_mode) or leaf.st_nlink != 1 or getattr(leaf, "st_file_attributes", 0) & 0x400:
                    raise _setup_error(canonical, "existing guard leaf is not a private regular file")
                expected_leaf = (leaf.st_dev, leaf.st_ino)
                fd = os.open(path, flags)
            opened = _guard_identity(path, fd)
            if expected_leaf is not None and expected_leaf != (opened.st_dev, opened.st_ino):
                raise _setup_error(canonical, "existing guard identity changed before open")
            os.set_inheritable(fd, False)
            if os.get_inheritable(fd):raise _setup_error(canonical, "guard descriptor remains inheritable")
            _acquire(fd, canonical)
            acquired = True
            _guard_identity(path, fd)
            after = canonical.stat()
            if (directory.st_dev, directory.st_ino) != (after.st_dev, after.st_ino):
                raise _setup_error(canonical, "data directory identity changed during acquisition")
        except OperationLockError:
            raise
        except (OSError, ImportError) as error:
            raise _setup_error(data_dir, f"guard setup failed ({type(error).__name__}, errno {getattr(error, 'errno', None)})") from error
        yield canonical
    finally:
        primary = sys.exc_info()[1]
        release_error = None
        try:
            if acquired:
                try:_unlock(fd)
                except OSError as error:release_error = _release_error(data_dir, f"kernel unlock failed (errno {error.errno})")
        finally:
            if fd is not None:
                try:os.close(fd)
                except OSError as error:
                    if release_error is None:release_error = _release_error(data_dir, f"guard close failed (errno {error.errno})")
        if release_error is not None:
            if primary is not None:
                primary.operation_lock_release_error = release_error
                add_note = getattr(primary, "add_note", None)
                if callable(add_note):add_note(str(release_error))
            else:raise release_error
