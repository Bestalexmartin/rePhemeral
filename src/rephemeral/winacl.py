"""Windows file ACLs, for the one file that needs them: the SSH key.

On Windows, the mode argument to os.open is ignored and Path.chmod only
toggles the read-only attribute, so a key written "mode 600" keeps
whatever ACL it inherits from its folder. Under the user profile that
happens to exclude other standard accounts. Under a folder such as
C:\\rephemeral it grants BUILTIN\\Users read and Authenticated Users
modify. This module sets an explicit, protected DACL instead, and reads
one back so the inspector can report what is actually true.

It calls advapi32 through ctypes rather than running icacls or depending
on pywin32, so the tool still starts no subprocesses and needs no
platform-only package.
"""
from __future__ import annotations

import ctypes
from ctypes import wintypes
from pathlib import Path

SYSTEM_SID = "S-1-5-18"
#: Stands for whoever owns the file, so it is the owner's own access.
OWNER_RIGHTS_SID = "S-1-3-4"

_SE_FILE_OBJECT = 1
_OWNER_SECURITY_INFORMATION = 0x00000001
_DACL_SECURITY_INFORMATION = 0x00000004
_PROTECTED_DACL_SECURITY_INFORMATION = 0x80000000
_SDDL_REVISION_1 = 1
_TOKEN_QUERY = 0x0008
_TOKEN_USER = 1
_ACCESS_ALLOWED_ACE_TYPE = 0
_INHERIT_ONLY_ACE = 0x08
# An allow entry carrying any of these lets its principal read the file:
# FILE_READ_DATA, GENERIC_READ, GENERIC_ALL.
_READ_MASK = 0x00000001 | 0x80000000 | 0x10000000

_advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
_kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

_PVOID = ctypes.c_void_p
_PPVOID = ctypes.POINTER(ctypes.c_void_p)

_advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW.argtypes = [
    wintypes.LPCWSTR, wintypes.DWORD, _PPVOID, ctypes.POINTER(wintypes.ULONG)]
_advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW.restype = wintypes.BOOL
_advapi32.GetSecurityDescriptorDacl.argtypes = [
    _PVOID, ctypes.POINTER(wintypes.BOOL), _PPVOID, ctypes.POINTER(wintypes.BOOL)]
_advapi32.GetSecurityDescriptorDacl.restype = wintypes.BOOL
_advapi32.SetNamedSecurityInfoW.argtypes = [
    wintypes.LPWSTR, ctypes.c_int, wintypes.DWORD, _PVOID, _PVOID, _PVOID, _PVOID]
_advapi32.SetNamedSecurityInfoW.restype = wintypes.DWORD
_advapi32.GetNamedSecurityInfoW.argtypes = [
    wintypes.LPCWSTR, ctypes.c_int, wintypes.DWORD,
    _PPVOID, _PPVOID, _PPVOID, _PPVOID, _PPVOID]
_advapi32.GetNamedSecurityInfoW.restype = wintypes.DWORD
_advapi32.GetAce.argtypes = [_PVOID, wintypes.DWORD, _PPVOID]
_advapi32.GetAce.restype = wintypes.BOOL
_advapi32.ConvertSidToStringSidW.argtypes = [_PVOID, _PPVOID]
_advapi32.ConvertSidToStringSidW.restype = wintypes.BOOL
_advapi32.ConvertStringSidToSidW.argtypes = [wintypes.LPCWSTR, _PPVOID]
_advapi32.ConvertStringSidToSidW.restype = wintypes.BOOL
_advapi32.LookupAccountSidW.argtypes = [
    wintypes.LPCWSTR, _PVOID, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD),
    wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD), ctypes.POINTER(wintypes.DWORD)]
_advapi32.LookupAccountSidW.restype = wintypes.BOOL
_advapi32.OpenProcessToken.argtypes = [
    wintypes.HANDLE, wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE)]
_advapi32.OpenProcessToken.restype = wintypes.BOOL
_advapi32.GetTokenInformation.argtypes = [
    wintypes.HANDLE, ctypes.c_int, _PVOID, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD)]
_advapi32.GetTokenInformation.restype = wintypes.BOOL
_kernel32.GetCurrentProcess.argtypes = []
_kernel32.GetCurrentProcess.restype = wintypes.HANDLE
_kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
_kernel32.CloseHandle.restype = wintypes.BOOL
_kernel32.LocalFree.argtypes = [_PVOID]
_kernel32.LocalFree.restype = _PVOID


class _ACL(ctypes.Structure):
    _fields_ = [
        ("AclRevision", ctypes.c_ubyte),
        ("Sbz1", ctypes.c_ubyte),
        ("AclSize", wintypes.WORD),
        ("AceCount", wintypes.WORD),
        ("Sbz2", wintypes.WORD),
    ]


class _ACE(ctypes.Structure):
    # The layout shared by ACCESS_ALLOWED_ACE and ACCESS_DENIED_ACE. The SID
    # begins at SidStart and runs on past the end of the structure.
    _fields_ = [
        ("AceType", ctypes.c_ubyte),
        ("AceFlags", ctypes.c_ubyte),
        ("AceSize", wintypes.WORD),
        ("Mask", wintypes.DWORD),
        ("SidStart", wintypes.DWORD),
    ]


def _last_error(path: Path | None = None) -> OSError:
    error = ctypes.WinError(ctypes.get_last_error())
    if path is not None:
        error.filename = str(path)
    return error


def _sid_string(sid: int) -> str:
    out = ctypes.c_void_p()
    if not _advapi32.ConvertSidToStringSidW(sid, ctypes.byref(out)):
        raise _last_error()
    try:
        return ctypes.wstring_at(out.value)
    finally:
        _kernel32.LocalFree(out)


def _account_name(sid: int) -> str | None:
    name, domain = ctypes.create_unicode_buffer(256), ctypes.create_unicode_buffer(256)
    name_len, domain_len, use = wintypes.DWORD(256), wintypes.DWORD(256), wintypes.DWORD()
    if not _advapi32.LookupAccountSidW(None, sid, name, ctypes.byref(name_len),
                                       domain, ctypes.byref(domain_len), ctypes.byref(use)):
        return None
    return f"{domain.value}\\{name.value}" if domain.value else name.value


def current_user_sid() -> str:
    """The SID of the account this process runs as."""
    token = wintypes.HANDLE()
    if not _advapi32.OpenProcessToken(_kernel32.GetCurrentProcess(), _TOKEN_QUERY,
                                      ctypes.byref(token)):
        raise _last_error()
    try:
        needed = wintypes.DWORD()
        _advapi32.GetTokenInformation(token, _TOKEN_USER, None, 0, ctypes.byref(needed))
        buffer = ctypes.create_string_buffer(needed.value)
        if not _advapi32.GetTokenInformation(token, _TOKEN_USER, buffer, needed,
                                             ctypes.byref(needed)):
            raise _last_error()
        # TOKEN_USER opens with a pointer to the user's SID.
        return _sid_string(ctypes.c_void_p.from_buffer(buffer).value)
    finally:
        _kernel32.CloseHandle(token)


def account_name(sid: str) -> str:
    """DOMAIN\\name for a SID string, or the SID itself if it has no name."""
    binary = ctypes.c_void_p()
    if not _advapi32.ConvertStringSidToSidW(sid, ctypes.byref(binary)):
        raise _last_error()
    try:
        return _account_name(binary.value) or sid
    finally:
        _kernel32.LocalFree(binary)


def set_dacl(path: Path, sddl: str) -> None:
    """Replace path's DACL with the one described by an SDDL string.

    Protected, so nothing is inherited from the folder above.
    """
    descriptor = ctypes.c_void_p()
    if not _advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW(
            sddl, _SDDL_REVISION_1, ctypes.byref(descriptor), None):
        raise _last_error(path)
    try:
        present, defaulted, dacl = wintypes.BOOL(), wintypes.BOOL(), ctypes.c_void_p()
        if not _advapi32.GetSecurityDescriptorDacl(descriptor, ctypes.byref(present),
                                                   ctypes.byref(dacl), ctypes.byref(defaulted)):
            raise _last_error(path)
        code = _advapi32.SetNamedSecurityInfoW(
            str(path), _SE_FILE_OBJECT,
            _DACL_SECURITY_INFORMATION | _PROTECTED_DACL_SECURITY_INFORMATION,
            None, None, dacl, None,
        )
        if code:
            error = ctypes.WinError(code)
            error.filename = str(path)
            raise error
    finally:
        _kernel32.LocalFree(descriptor)


def restrict(path: Path) -> None:
    """Give the current user and SYSTEM full control, and nobody else anything.

    SYSTEM stays so that backup and indexing services keep working.
    Administrators are left out: an administrator who wants the key has to
    take ownership of it first, which is an audited act, not a read.
    A directory's entries are inheritable, so files created inside it
    later start out restricted too.
    """
    inherit = "OICI" if path.is_dir() else ""
    user = current_user_sid()
    set_dacl(path, f"D:P(A;{inherit};FA;;;{user})(A;{inherit};FA;;;{SYSTEM_SID})")


def other_readers(path: Path) -> list[tuple[str, str]]:
    """Principals other than the current user and SYSTEM that can read path.

    Returns (SID, account name) pairs. Deny entries are not subtracted, so
    this can over-report but never under-report, which is the right way
    round for a warning about a private key.
    """
    owner, dacl, descriptor = ctypes.c_void_p(), ctypes.c_void_p(), ctypes.c_void_p()
    code = _advapi32.GetNamedSecurityInfoW(
        str(path), _SE_FILE_OBJECT,
        _OWNER_SECURITY_INFORMATION | _DACL_SECURITY_INFORMATION,
        ctypes.byref(owner), None, ctypes.byref(dacl), None, ctypes.byref(descriptor),
    )
    if code:
        error = ctypes.WinError(code)
        error.filename = str(path)
        raise error
    try:
        if not dacl.value:
            # A null DACL grants everyone full access.
            return [("S-1-1-0", "Everyone (the file has no access control list)")]
        user = current_user_sid()
        allowed = {user, SYSTEM_SID}
        if owner.value and _sid_string(owner.value) == user:
            allowed.add(OWNER_RIGHTS_SID)
        found: dict[str, str] = {}
        for index in range(_ACL.from_address(dacl.value).AceCount):
            ace = ctypes.c_void_p()
            if not _advapi32.GetAce(dacl, index, ctypes.byref(ace)):
                raise _last_error(path)
            entry = _ACE.from_address(ace.value)
            if (entry.AceType != _ACCESS_ALLOWED_ACE_TYPE
                    or entry.AceFlags & _INHERIT_ONLY_ACE
                    or not entry.Mask & _READ_MASK):
                continue
            sid_address = ace.value + _ACE.SidStart.offset
            sid = _sid_string(sid_address)
            if sid not in allowed:
                found.setdefault(sid, _account_name(sid_address) or sid)
        return list(found.items())
    finally:
        _kernel32.LocalFree(descriptor)
