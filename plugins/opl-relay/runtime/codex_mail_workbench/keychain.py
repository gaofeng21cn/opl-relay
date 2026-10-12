"""Store long OAuth credentials with macOS Security, without argv or TTY limits."""
from __future__ import annotations

import ctypes as C
import sys

from .config import KEYCHAIN_SERVICE, LOGIN_KEYCHAIN


def set_oauth_secret(reference: str, secret: str) -> None:
    if sys.platform != "darwin":
        raise RuntimeError("OAuth credential storage requires macOS Keychain")
    security = C.CDLL("/System/Library/Frameworks/Security.framework/Security")
    core = C.CDLL("/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation")
    pointer = C.c_void_p
    signatures = {
        "SecKeychainOpen": ([C.c_char_p, C.POINTER(pointer)], C.c_int32),
        "SecKeychainFindGenericPassword": ([pointer, C.c_uint32, C.c_char_p, C.c_uint32, C.c_char_p,
                                            C.POINTER(C.c_uint32), C.POINTER(pointer), C.POINTER(pointer)], C.c_int32),
        "SecKeychainAddGenericPassword": ([pointer, C.c_uint32, C.c_char_p, C.c_uint32, C.c_char_p,
                                           C.c_uint32, C.c_void_p, C.POINTER(pointer)], C.c_int32),
        "SecKeychainItemModifyAttributesAndData": ([pointer, pointer, C.c_uint32, C.c_void_p], C.c_int32),
        "SecTrustedApplicationCreateFromPath": ([C.c_char_p, C.POINTER(pointer)], C.c_int32),
        "SecAccessCreate": ([pointer, pointer, C.POINTER(pointer)], C.c_int32),
        "SecKeychainItemSetAccess": ([pointer, pointer], C.c_int32),
    }
    for name, (args, restype) in signatures.items():
        fn = getattr(security, name)
        fn.argtypes, fn.restype = args, restype
    core.CFRelease.argtypes = [pointer]
    core.CFStringCreateWithCString.argtypes = [pointer, C.c_char_p, C.c_uint32]
    core.CFStringCreateWithCString.restype = pointer
    core.CFArrayCreate.argtypes = [pointer, C.POINTER(pointer), C.c_long, pointer]
    core.CFArrayCreate.restype = pointer

    def check(status):
        if status:
            raise RuntimeError(f"macOS Keychain OAuth write failed (OSStatus {status}); unlock/authorize the login Keychain")

    keychain, item, trusted, writer, access = pointer(), pointer(), pointer(), pointer(), pointer()
    label = array = None
    service, account, value = KEYCHAIN_SERVICE.encode(), reference.encode(), secret.encode()
    try:
        check(security.SecKeychainOpen(LOGIN_KEYCHAIN.encode(), C.byref(keychain)))
        status = security.SecKeychainFindGenericPassword(keychain, len(service), service, len(account), account,
                                                       None, None, C.byref(item))
        if status == -25300:  # errSecItemNotFound
            check(security.SecKeychainAddGenericPassword(keychain, len(service), service, len(account), account,
                                                        len(value), value, C.byref(item)))
        else:
            check(status)
            check(security.SecKeychainItemModifyAttributesAndData(item, None, len(value), value))
        # Same trusted reader as Relay's existing password contract. This grants
        # no third-party access and does not bypass a locked Keychain or prompt.
        check(security.SecTrustedApplicationCreateFromPath(b"/usr/bin/security", C.byref(trusted)))
        check(security.SecTrustedApplicationCreateFromPath(sys.executable.encode(), C.byref(writer)))
        label = core.CFStringCreateWithCString(None, b"OPL Relay OAuth", 0x08000100)
        callbacks = C.addressof((C.c_byte * 1).in_dll(core, "kCFTypeArrayCallBacks"))
        array = core.CFArrayCreate(None, (pointer * 2)(trusted, writer), 2, callbacks)
        check(security.SecAccessCreate(label, array, C.byref(access)))
        check(security.SecKeychainItemSetAccess(item, access))
    finally:
        for obj in (access, array, label, writer, trusted, item, keychain):
            if obj:
                core.CFRelease(obj)
