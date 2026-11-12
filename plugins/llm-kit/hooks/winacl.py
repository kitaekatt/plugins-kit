"""Windows access control for the relay state directory (stdlib ctypes over advapi32).

POSIX keeps the state directory ``0700`` and every credential ``0600``. On
Windows ``os.chmod`` only toggles the read-only flag, so the same promise is
kept with an owner and a PROTECTED discretionary ACL that grants full control
to the current user and SYSTEM only:

* :func:`provision` sets the owner to the current user and that DACL. A
  directory's ACEs are inheritable, so a file created inside it starts private.
* :func:`validate` reads the security descriptor as binary (owner SID, the
  DACL's ACEs one by one) and refuses what :func:`provision` would never have
  written (see :func:`check_descriptor`). It fails closed: an ACE type it does
  not explicitly understand (conditional, object, ...) is refused. It never
  repairs: a broad file is refused, as on POSIX.
* :func:`create_private_dir` and :func:`create_private_file` create with that
  descriptor in the same call (``SECURITY_ATTRIBUTES``), so nothing is ever
  visible with an inherited ACL.
* :func:`refuse_writable_ancestors` refuses a state root whose parent chain
  lets another trustee replace or re-ACL it.

Importing this module is safe on every platform; the Win32 calls load on first
use and raise :class:`RelayConfigError` anywhere but Windows.
"""
from __future__ import annotations

import ctypes
import os
from pathlib import Path
from typing import Any, NamedTuple, Optional, Sequence

from llm_kit.relay.config import RelayConfigError

_SYSTEM = "S-1-5-18"
_ADMINISTRATORS = "S-1-5-32-544"
_OWNER_RIGHTS = "S-1-3-4"
_TRUSTED_INSTALLER = "S-1-5-80-956008885-3418522649-1831038044-1853292631-2271478464"
#: The only ACE types a DACL may hold for validation: ACCESS_ALLOWED and ACCESS_DENIED.
_ACE_ALLOWED, _ACE_DENIED = 0, 1
_INHERIT_ONLY_ACE = 0x08
_SE_DACL_PROTECTED = 0x1000
#: Rights that let a trustee change, replace or re-ACL a directory entry.
_WRITE_RIGHTS = (
    0x2 | 0x4 | 0x40  # add file, add subdirectory, delete child
    | 0x10000 | 0x40000 | 0x80000  # DELETE, WRITE_DAC, WRITE_OWNER
    | 0x40000000 | 0x10000000  # GENERIC_WRITE, GENERIC_ALL
)
#: Rights that let a trustee rename, replace or re-ACL a directory entry that already exists
#: (DELETE_CHILD on the parent, DELETE/WRITE_DAC/WRITE_OWNER on the entry, or the generic forms).
_REPLACE_RIGHTS = 0x40 | 0x10000 | 0x40000 | 0x80000 | 0x40000000 | 0x10000000
#: Access requested for a created file: read/write data plus READ_CONTROL, WRITE_DAC and WRITE_OWNER, so the
#: creating handle can be provisioned and validated without reopening the path.
_GENERIC_READ_WRITE = 0xC0000000 | 0x20000 | 0x40000 | 0x80000
_FILE_SHARE_READ_WRITE = 0x3
_CREATE_NEW = 1
_FILE_ATTRIBUTE_NORMAL = 0x80
_ERROR_FILE_EXISTS, _ERROR_ALREADY_EXISTS = 80, 183

_SE_FILE_OBJECT = 1
_OWNER_SECURITY_INFORMATION = 0x1
_DACL_SECURITY_INFORMATION = 0x4
_PROTECTED_DACL_SECURITY_INFORMATION = 0x80000000
_SDDL_REVISION_1 = 1
_TOKEN_QUERY = 0x8
_TOKEN_USER = 1

_api_cache: Optional[Any] = None
_sid_cache: Optional[str] = None


class _Api:
    """The Win32 entry points with explicit prototypes (so 64-bit pointers survive)."""

    def __init__(self) -> None:
        from ctypes import wintypes

        advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)  # type: ignore[attr-defined]
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)  # type: ignore[attr-defined]
        self.wintypes = wintypes
        vp = ctypes.c_void_p
        pvp = ctypes.POINTER(ctypes.c_void_p)
        pw = ctypes.POINTER(ctypes.c_wchar_p)
        pdw = ctypes.POINTER(wintypes.DWORD)
        pbool = ctypes.POINTER(wintypes.BOOL)

        class SecurityAttributes(ctypes.Structure):
            _fields_ = [("nLength", wintypes.DWORD), ("lpSecurityDescriptor", vp), ("bInheritHandle", wintypes.BOOL)]

        class AclSizeInformation(ctypes.Structure):
            _fields_ = [("AceCount", wintypes.DWORD), ("AclBytesInUse", wintypes.DWORD), ("AclBytesFree", wintypes.DWORD)]

        self.SecurityAttributes = SecurityAttributes
        self.AclSizeInformation = AclSizeInformation

        self.GetCurrentProcess = kernel32.GetCurrentProcess
        self.GetCurrentProcess.restype = wintypes.HANDLE
        self.CloseHandle = kernel32.CloseHandle
        self.CloseHandle.argtypes = [wintypes.HANDLE]
        self.LocalFree = kernel32.LocalFree
        self.LocalFree.argtypes = [vp]
        self.LocalFree.restype = vp
        self.GetLongPathNameW = kernel32.GetLongPathNameW
        self.GetLongPathNameW.argtypes = [wintypes.LPCWSTR, wintypes.LPWSTR, wintypes.DWORD]
        self.GetLongPathNameW.restype = wintypes.DWORD
        self.CreateDirectoryW = kernel32.CreateDirectoryW
        self.CreateDirectoryW.argtypes = [wintypes.LPCWSTR, ctypes.POINTER(SecurityAttributes)]
        self.CreateDirectoryW.restype = wintypes.BOOL
        self.CreateFileW = kernel32.CreateFileW
        self.CreateFileW.argtypes = [
            wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.POINTER(SecurityAttributes),
            wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE,
        ]
        self.CreateFileW.restype = wintypes.HANDLE

        self.OpenProcessToken = advapi32.OpenProcessToken
        self.OpenProcessToken.argtypes = [wintypes.HANDLE, wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE)]
        self.OpenProcessToken.restype = wintypes.BOOL
        self.GetTokenInformation = advapi32.GetTokenInformation
        self.GetTokenInformation.argtypes = [wintypes.HANDLE, ctypes.c_int, vp, wintypes.DWORD, pdw]
        self.GetTokenInformation.restype = wintypes.BOOL
        self.ConvertSidToStringSidW = advapi32.ConvertSidToStringSidW
        self.ConvertSidToStringSidW.argtypes = [vp, pw]
        self.ConvertSidToStringSidW.restype = wintypes.BOOL
        self.ConvertStringSecurityDescriptorToSecurityDescriptorW = (
            advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW
        )
        self.ConvertStringSecurityDescriptorToSecurityDescriptorW.argtypes = [
            wintypes.LPCWSTR, wintypes.DWORD, pvp, pdw,
        ]
        self.ConvertStringSecurityDescriptorToSecurityDescriptorW.restype = wintypes.BOOL
        self.ConvertSecurityDescriptorToStringSecurityDescriptorW = (
            advapi32.ConvertSecurityDescriptorToStringSecurityDescriptorW
        )
        self.ConvertSecurityDescriptorToStringSecurityDescriptorW.argtypes = [
            vp, wintypes.DWORD, wintypes.DWORD, pw, pdw,
        ]
        self.ConvertSecurityDescriptorToStringSecurityDescriptorW.restype = wintypes.BOOL
        self.GetSecurityDescriptorOwner = advapi32.GetSecurityDescriptorOwner
        self.GetSecurityDescriptorOwner.argtypes = [vp, pvp, pbool]
        self.GetSecurityDescriptorOwner.restype = wintypes.BOOL
        self.GetSecurityDescriptorDacl = advapi32.GetSecurityDescriptorDacl
        self.GetSecurityDescriptorDacl.argtypes = [vp, pbool, pvp, pbool]
        self.GetSecurityDescriptorDacl.restype = wintypes.BOOL
        self.GetSecurityDescriptorControl = advapi32.GetSecurityDescriptorControl
        self.GetSecurityDescriptorControl.argtypes = [vp, ctypes.POINTER(wintypes.WORD), pdw]
        self.GetSecurityDescriptorControl.restype = wintypes.BOOL
        self.GetAclInformation = advapi32.GetAclInformation
        self.GetAclInformation.argtypes = [vp, vp, wintypes.DWORD, ctypes.c_int]
        self.GetAclInformation.restype = wintypes.BOOL
        self.GetAce = advapi32.GetAce
        self.GetAce.argtypes = [vp, wintypes.DWORD, pvp]
        self.GetAce.restype = wintypes.BOOL
        self.SetNamedSecurityInfoW = advapi32.SetNamedSecurityInfoW
        self.SetNamedSecurityInfoW.argtypes = [
            wintypes.LPWSTR, ctypes.c_int, wintypes.DWORD, vp, vp, vp, vp,
        ]
        self.SetNamedSecurityInfoW.restype = wintypes.DWORD
        self.GetNamedSecurityInfoW = advapi32.GetNamedSecurityInfoW
        self.GetNamedSecurityInfoW.argtypes = [
            wintypes.LPCWSTR, ctypes.c_int, wintypes.DWORD, vp, vp, vp, vp, pvp,
        ]
        self.GetNamedSecurityInfoW.restype = wintypes.DWORD
        self.SetSecurityInfo = advapi32.SetSecurityInfo
        self.SetSecurityInfo.argtypes = [wintypes.HANDLE, ctypes.c_int, wintypes.DWORD, vp, vp, vp, vp]
        self.SetSecurityInfo.restype = wintypes.DWORD
        self.GetSecurityInfo = advapi32.GetSecurityInfo
        self.GetSecurityInfo.argtypes = [
            wintypes.HANDLE, ctypes.c_int, wintypes.DWORD, vp, vp, vp, vp, pvp,
        ]
        self.GetSecurityInfo.restype = wintypes.DWORD


def _api() -> _Api:
    global _api_cache
    if os.name != "nt":
        raise RelayConfigError("Windows access control (winacl) is only available on Windows")
    if _api_cache is None:
        _api_cache = _Api()
    return _api_cache


def _winerror(what: str, code: int) -> RelayConfigError:
    return RelayConfigError(f"{what} failed: {ctypes.FormatError(code).strip()} (Windows error {code})")  # type: ignore[attr-defined]


def _sid_string(api: _Api, sid_ptr: Any) -> str:
    text = ctypes.c_wchar_p()
    if not api.ConvertSidToStringSidW(sid_ptr, ctypes.byref(text)):
        raise _winerror("ConvertSidToStringSidW", ctypes.get_last_error())  # type: ignore[attr-defined]
    try:
        return str(text.value)
    finally:
        api.LocalFree(ctypes.cast(text, ctypes.c_void_p))


def current_user_sid() -> str:
    """The current process user's SID as a string (``S-1-5-21-...``)."""
    global _sid_cache
    if _sid_cache is not None:
        return _sid_cache
    api = _api()
    wt = api.wintypes
    token = wt.HANDLE()
    if not api.OpenProcessToken(api.GetCurrentProcess(), _TOKEN_QUERY, ctypes.byref(token)):
        raise _winerror("OpenProcessToken", ctypes.get_last_error())  # type: ignore[attr-defined]
    try:
        needed = wt.DWORD(0)
        api.GetTokenInformation(token, _TOKEN_USER, None, 0, ctypes.byref(needed))
        buf = ctypes.create_string_buffer(needed.value)
        if not api.GetTokenInformation(token, _TOKEN_USER, buf, needed.value, ctypes.byref(needed)):
            raise _winerror("GetTokenInformation", ctypes.get_last_error())  # type: ignore[attr-defined]
        sid_ptr = ctypes.cast(buf, ctypes.POINTER(ctypes.c_void_p))[0]
        _sid_cache = _sid_string(api, sid_ptr)
    finally:
        api.CloseHandle(token)
    return _sid_cache


def _sddl_for(sid: str, *, directory: bool, owner: bool = True) -> str:
    """The SDDL of a private object: owner ``sid`` (optional), protected DACL for ``sid`` and SYSTEM."""
    flags = "OICI" if directory else ""
    return f"{f'O:{sid}' if owner else ''}D:P(A;{flags};FA;;;{sid})(A;{flags};FA;;;SY)"


class _Sd:
    """A security descriptor converted from SDDL; freed on exit."""

    def __init__(self, api: _Api, sddl: str) -> None:
        self.api = api
        self.ptr = ctypes.c_void_p()
        if not api.ConvertStringSecurityDescriptorToSecurityDescriptorW(
            sddl, _SDDL_REVISION_1, ctypes.byref(self.ptr), None
        ):
            raise _winerror("ConvertStringSecurityDescriptorToSecurityDescriptorW", ctypes.get_last_error())  # type: ignore[attr-defined]

    def __enter__(self) -> "_Sd":
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.api.LocalFree(self.ptr)

    def attributes(self) -> Any:
        sa = self.api.SecurityAttributes()
        sa.nLength = ctypes.sizeof(sa)
        sa.lpSecurityDescriptor = self.ptr
        sa.bInheritHandle = False
        return sa


def _apply_sddl(path: "os.PathLike[str] | str", sddl: str, *, fd: Optional[int] = None) -> None:
    """Set owner and DACL from ``sddl`` and mark the DACL protected: on the open file ``fd`` when given, else ``path``."""
    import msvcrt

    api = _api()
    wt = api.wintypes
    with _Sd(api, sddl) as sd:
        owner = ctypes.c_void_p()
        dacl = ctypes.c_void_p()
        flag = wt.BOOL()
        present = wt.BOOL()
        if not api.GetSecurityDescriptorOwner(sd.ptr, ctypes.byref(owner), ctypes.byref(flag)):
            raise _winerror("GetSecurityDescriptorOwner", ctypes.get_last_error())  # type: ignore[attr-defined]
        if not api.GetSecurityDescriptorDacl(sd.ptr, ctypes.byref(present), ctypes.byref(dacl), ctypes.byref(flag)):
            raise _winerror("GetSecurityDescriptorDacl", ctypes.get_last_error())  # type: ignore[attr-defined]
        info = _OWNER_SECURITY_INFORMATION | _DACL_SECURITY_INFORMATION | _PROTECTED_DACL_SECURITY_INFORMATION
        if fd is not None:
            code = api.SetSecurityInfo(msvcrt.get_osfhandle(fd), _SE_FILE_OBJECT, info, owner, None, dacl, None)  # type: ignore[attr-defined]
        else:
            code = api.SetNamedSecurityInfoW(str(path), _SE_FILE_OBJECT, info, owner, None, dacl, None)
        if code != 0:
            raise _winerror(f"setting the owner and ACL of {path}", code)


def provision(path: "os.PathLike[str] | str", directory: bool = False) -> None:
    """Make ``path`` private: owner = current user, protected DACL = that user + SYSTEM only.

    Idempotent. ``directory`` makes the ACEs inheritable, so files created in
    it start private. A failure (for example an owner this process may not
    assign) raises :class:`RelayConfigError`.
    """
    _apply_sddl(path, _sddl_for(current_user_sid(), directory=directory))


def provision_fd(fd: int, path: "os.PathLike[str] | str") -> None:
    """:func:`provision` for a file open as ``fd`` (it must have been opened with WRITE_DAC and WRITE_OWNER,
    as :func:`create_private_file` does): the handle that made the file is the object changed, not a pathname."""
    _apply_sddl(path, _sddl_for(current_user_sid(), directory=False), fd=fd)


def create_private_dir(path: "os.PathLike[str] | str") -> None:
    """Create directory ``path`` with the private descriptor in the same call.

    Raises :class:`FileExistsError` when it exists. Nothing ever observes the
    directory with an ACL inherited from its parent.
    """
    api = _api()
    with _Sd(api, _sddl_for(current_user_sid(), directory=True, owner=False)) as sd:
        sa = sd.attributes()
        if not api.CreateDirectoryW(str(path), ctypes.byref(sa)):
            code = ctypes.get_last_error()  # type: ignore[attr-defined]
            if code in (_ERROR_ALREADY_EXISTS, _ERROR_FILE_EXISTS):
                raise FileExistsError(str(path))
            raise _winerror(f"creating directory {path}", code)


def create_private_file(path: "os.PathLike[str] | str") -> int:
    """Create file ``path`` (``CREATE_NEW``) with the private descriptor in the same call; a read-write binary fd.

    Raises :class:`FileExistsError` when it exists. The returned descriptor is
    the handle that made the file, so a caller keeps it through the first
    write.
    """
    import msvcrt

    api = _api()
    with _Sd(api, _sddl_for(current_user_sid(), directory=False, owner=False)) as sd:
        sa = sd.attributes()
        handle = api.CreateFileW(
            str(path), _GENERIC_READ_WRITE, _FILE_SHARE_READ_WRITE, ctypes.byref(sa), _CREATE_NEW,
            _FILE_ATTRIBUTE_NORMAL, None,
        )
    if handle in (None, 0, -1, 0xFFFFFFFF, 0xFFFFFFFFFFFFFFFF):
        code = ctypes.get_last_error()  # type: ignore[attr-defined]
        if code in (_ERROR_ALREADY_EXISTS, _ERROR_FILE_EXISTS):
            raise FileExistsError(str(path))
        raise _winerror(f"creating file {path}", code)
    try:
        return msvcrt.open_osfhandle(handle, os.O_RDWR | os.O_BINARY)  # type: ignore[attr-defined]
    except BaseException:
        api.CloseHandle(handle)
        raise


def read_sddl(path: "os.PathLike[str] | str") -> str:
    """The owner and DACL of ``path`` as SDDL text (``O:...D:...``); for diagnostics, never for validation."""
    api = _api()
    psd = ctypes.c_void_p()
    code = api.GetNamedSecurityInfoW(
        str(path), _SE_FILE_OBJECT, _OWNER_SECURITY_INFORMATION | _DACL_SECURITY_INFORMATION,
        None, None, None, None, ctypes.byref(psd),
    )
    if code != 0:
        raise _winerror(f"reading the security descriptor of {path}", code)
    try:
        text = ctypes.c_wchar_p()
        if not api.ConvertSecurityDescriptorToStringSecurityDescriptorW(
            psd, _SDDL_REVISION_1, _OWNER_SECURITY_INFORMATION | _DACL_SECURITY_INFORMATION,
            ctypes.byref(text), None,
        ):
            raise _winerror("ConvertSecurityDescriptorToStringSecurityDescriptorW", ctypes.get_last_error())  # type: ignore[attr-defined]
        try:
            return str(text.value)
        finally:
            api.LocalFree(ctypes.cast(text, ctypes.c_void_p))
    finally:
        api.LocalFree(psd)


# --------------------------------------------------------------------------
# reading and judging a descriptor
# --------------------------------------------------------------------------

class Ace(NamedTuple):
    """One ACE of a DACL: type byte, flags, access mask, and trustee SID string (``None`` unless allow/deny)."""

    ace_type: int
    flags: int
    mask: int
    sid: Optional[str]


class Descriptor(NamedTuple):
    owner: Optional[str]
    protected: bool
    dacl_present: bool  # False = a null DACL, which grants everyone full access
    aces: Sequence[Ace]


def _read_acl(api: _Api, dacl: Any) -> list[Ace]:
    info = api.AclSizeInformation()
    if not api.GetAclInformation(dacl, ctypes.byref(info), ctypes.sizeof(info), 2):  # AclSizeInformation
        raise _winerror("GetAclInformation", ctypes.get_last_error())  # type: ignore[attr-defined]
    aces: list[Ace] = []
    for index in range(info.AceCount):
        pace = ctypes.c_void_p()
        if not api.GetAce(dacl, index, ctypes.byref(pace)):
            raise _winerror(f"GetAce({index})", ctypes.get_last_error())  # type: ignore[attr-defined]
        header = ctypes.string_at(pace.value, 4)
        ace_type, flags, size = header[0], header[1], header[2] | (header[3] << 8)
        if ace_type in (_ACE_ALLOWED, _ACE_DENIED) and size >= 16:
            mask = int.from_bytes(ctypes.string_at(pace.value + 4, 4), "little")
            aces.append(Ace(ace_type, flags, mask, _sid_string(api, pace.value + 8)))
        else:
            aces.append(Ace(ace_type, flags, 0, None))  # not understood: the policy refuses it
    return aces


def _descriptor_from(api: _Api, code: int, what: str, owner: Any, dacl: Any, psd: Any) -> Descriptor:
    if code != 0:
        raise _winerror(f"reading the security descriptor of {what}", code)
    try:
        wt = api.wintypes
        control = wt.WORD()
        revision = wt.DWORD()
        if not api.GetSecurityDescriptorControl(psd, ctypes.byref(control), ctypes.byref(revision)):
            raise _winerror("GetSecurityDescriptorControl", ctypes.get_last_error())  # type: ignore[attr-defined]
        return Descriptor(
            owner=_sid_string(api, owner) if owner.value else None,
            protected=bool(control.value & _SE_DACL_PROTECTED),
            dacl_present=bool(dacl.value),
            aces=_read_acl(api, dacl) if dacl.value else [],
        )
    finally:
        api.LocalFree(psd)


def read_descriptor(path: "os.PathLike[str] | str") -> Descriptor:
    """The owner and the binary DACL of ``path``."""
    api = _api()
    owner, dacl, psd = ctypes.c_void_p(), ctypes.c_void_p(), ctypes.c_void_p()
    code = api.GetNamedSecurityInfoW(
        str(path), _SE_FILE_OBJECT, _OWNER_SECURITY_INFORMATION | _DACL_SECURITY_INFORMATION,
        ctypes.byref(owner), None, ctypes.byref(dacl), None, ctypes.byref(psd),
    )
    return _descriptor_from(api, code, str(path), owner, dacl, psd)


def read_descriptor_fd(fd: int) -> Descriptor:
    """The owner and the binary DACL of the file open as ``fd`` (the object actually read, not a path)."""
    import msvcrt

    api = _api()
    owner, dacl, psd = ctypes.c_void_p(), ctypes.c_void_p(), ctypes.c_void_p()
    code = api.GetSecurityInfo(
        msvcrt.get_osfhandle(fd), _SE_FILE_OBJECT, _OWNER_SECURITY_INFORMATION | _DACL_SECURITY_INFORMATION,  # type: ignore[attr-defined]
        ctypes.byref(owner), None, ctypes.byref(dacl), None, ctypes.byref(psd),
    )
    return _descriptor_from(api, code, f"descriptor {fd}", owner, dacl, psd)


def check_descriptor(desc: Descriptor, user_sid: str, path: object) -> None:
    """Refuse ``desc`` unless it is what :func:`provision` writes. Pure and portable; fails closed.

    Refused: an owner other than ``user_sid``; a null DACL; a DACL that is not
    protected (it would follow its parent); any ACE that is not a plain
    ACCESS_ALLOWED or ACCESS_DENIED ACE (conditional, object, callback, anything
    unknown); an allow ACE for a trustee other than ``user_sid`` or SYSTEM; and
    a DACL with no allow ACE for ``user_sid``. A deny ACE is not a grant.
    """
    if desc.owner != user_sid:
        raise RelayConfigError(f"{path} is not owned by the current user (owner {desc.owner}, expected {user_sid})")
    if not desc.dacl_present:
        raise RelayConfigError(f"{path} has a null or missing DACL, which grants everyone access")
    if not desc.protected:
        raise RelayConfigError(f"{path} has an unprotected DACL; it inherits access from its parent")
    allowed = {user_sid, _SYSTEM}
    user_granted = False
    for ace in desc.aces:
        if ace.ace_type not in (_ACE_ALLOWED, _ACE_DENIED) or ace.sid is None:
            raise RelayConfigError(
                f"{path} has an ACE of type {ace.ace_type} this relay does not understand (refused, not skipped)"
            )
        if ace.ace_type == _ACE_DENIED:
            continue
        if ace.sid not in allowed:
            raise RelayConfigError(f"{path} grants access to {ace.sid}; only the current user and SYSTEM may have it")
        if ace.sid == user_sid:
            user_granted = True
    if not user_granted:
        raise RelayConfigError(f"{path} grants the current user no access")


def _long_abs(path: str, api: _Api) -> str:
    """The absolute path with 8.3 short names expanded, so it is comparable to a real path."""
    absolute = os.path.abspath(path)
    buf = ctypes.create_unicode_buffer(32768)
    n = api.GetLongPathNameW(absolute, buf, len(buf))
    return buf.value if 0 < n < len(buf) else absolute


def _strip_prefix(path: str) -> str:
    return path[4:] if path.startswith("\\\\?\\") and not path.startswith("\\\\?\\UNC\\") else path


def refuse_redirected(path: "os.PathLike[str] | str") -> None:
    """Refuse ``path`` when its real path differs from its absolute path (a junction or symlink)."""
    api = _api()
    absolute = _strip_prefix(_long_abs(str(path), api))
    real = _strip_prefix(os.path.realpath(str(path)))
    if os.path.normcase(os.path.normpath(real)) != os.path.normcase(os.path.normpath(absolute)):
        raise RelayConfigError(
            f"{path} resolves to {real}, not {absolute}; a junction or symlink may not redirect relay state"
        )


def validate(path: "os.PathLike[str] | str") -> None:
    """Refuse ``path`` unless it is private to the current user (see :func:`check_descriptor`).

    Also refuses a path whose real path differs from its absolute path.
    Raises :class:`RelayConfigError`; never repairs.
    """
    refuse_redirected(path)
    check_descriptor(read_descriptor(path), current_user_sid(), Path(path))


def validate_fd(fd: int, path: "os.PathLike[str] | str") -> None:
    """:func:`validate` for a file already open as ``fd``: the descriptor judged is the open object's."""
    refuse_redirected(path)
    check_descriptor(read_descriptor_fd(fd), current_user_sid(), Path(path))


# --------------------------------------------------------------------------
# ancestors
# --------------------------------------------------------------------------

def check_ancestor(desc: Descriptor, user_sid: str, path: object, *, strict: bool = True) -> None:
    """Refuse an ancestor directory that another trustee can use to replace what lies below. Pure; fails closed.

    The owner must be the user, SYSTEM, Administrators or TrustedInstaller: an owner always holds WRITE_DAC
    implicitly, so a foreign owner can regain any right. Among the DACL's allow ACEs, a trustee outside that
    set must not hold a right in the refused mask:

    * ``strict`` (the state root's own container chain): add-file, add-subdirectory, delete-child, DELETE,
      WRITE_DAC, WRITE_OWNER and the generic write/all rights. Someone who can add entries beside the state
      root can pre-create or race what the relay creates there.
    * not strict (the path above it): only the rights that rename or replace an entry that already exists,
      DELETE_CHILD (on this directory as the parent of the next component), DELETE, WRITE_DAC and WRITE_OWNER
      (on it as that component), and the generic write/all rights. Add-file or add-subdirectory alone, as
      Authenticated Users hold on ``C:\\``, cannot move an existing directory and is not refused.

    Inherit-only ACEs do not apply to the directory itself and are skipped. OWNER RIGHTS (S-1-3-4) counts as
    trusted when the owner is. A null DACL, or an ACE type other than allow or deny, is refused.
    """
    trusted = {user_sid, _SYSTEM, _ADMINISTRATORS, _TRUSTED_INSTALLER}
    if desc.owner not in trusted:
        raise RelayConfigError(
            f"ancestor {path} is owned by {desc.owner}, who can always change its ACL; "
            "keep relay state under directories owned by you, SYSTEM or Administrators"
        )
    if not desc.dacl_present:
        raise RelayConfigError(f"ancestor {path} has a null DACL, which lets everyone replace its children")
    trusted.add(_OWNER_RIGHTS)
    refused = _WRITE_RIGHTS if strict else _REPLACE_RIGHTS
    for ace in desc.aces:
        if ace.ace_type not in (_ACE_ALLOWED, _ACE_DENIED) or ace.sid is None:
            raise RelayConfigError(f"ancestor {path} has an ACE of type {ace.ace_type} this relay does not understand")
        if ace.ace_type == _ACE_DENIED or ace.flags & _INHERIT_ONLY_ACE:
            continue
        if ace.mask & refused and ace.sid not in trusted:
            raise RelayConfigError(
                f"ancestor {path} lets {ace.sid} write to it (access mask {ace.mask:#x}); "
                "keep relay state under a directory only you can change"
            )


def ancestor_chain(
    path: "os.PathLike[str] | str", home: "os.PathLike[str] | str"
) -> tuple[list[Path], list[Path]]:
    """``(strict, lenient)``: ``path``'s parents, nearest first, split by how closely they are checked.

    Every parent up to the volume or share root is returned. ``strict`` is the state root's own container
    chain: its parents up to and including ``home`` when it lies under the profile, else only its immediate
    parent. ``lenient`` is everything above that, up to the root.
    """
    stop = os.path.normcase(os.path.abspath(str(home)))
    full: list[Path] = []
    current = Path(os.path.abspath(str(path))).parent
    while True:
        full.append(current)
        if current.parent == current:
            break
        current = current.parent
    for index, ancestor in enumerate(full):
        if os.path.normcase(str(ancestor)) == stop:
            return full[: index + 1], full[index + 1 :]
    return full[:1], full[1:]


def refuse_writable_ancestors(path: "os.PathLike[str] | str") -> None:
    """Refuse ``path`` when an ancestor lets another trustee replace or re-ACL it (see :func:`check_ancestor`)."""
    user = current_user_sid()
    strict, lenient = ancestor_chain(path, Path.home())
    for ancestor in strict:
        check_ancestor(read_descriptor(ancestor), user, ancestor, strict=True)
    for ancestor in lenient:
        check_ancestor(read_descriptor(ancestor), user, ancestor, strict=False)


__all__ = [
    "Ace", "Descriptor", "ancestor_chain", "check_ancestor", "check_descriptor", "create_private_dir",
    "create_private_file", "current_user_sid", "provision", "provision_fd", "read_descriptor", "read_descriptor_fd", "read_sddl",
    "refuse_redirected", "refuse_writable_ancestors", "validate", "validate_fd",
]
