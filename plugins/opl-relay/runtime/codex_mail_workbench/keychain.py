"""Read and store OAuth credentials through the authorized macOS Security API."""
from __future__ import annotations

import ctypes as C
import sys

from .config import KEYCHAIN_SERVICE, LOGIN_KEYCHAIN


def _frameworks(*, allow_interaction=False):
    if sys.platform != "darwin":
        raise RuntimeError("OAuth credential storage requires macOS Keychain")
    security = C.CDLL("/System/Library/Frameworks/Security.framework/Security")
    core = C.CDLL("/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation")
    pointer = C.c_void_p
    signatures = {
        "SecKeychainSetUserInteractionAllowed": ([C.c_bool], C.c_int32),
        "SecKeychainOpen": ([C.c_char_p, C.POINTER(pointer)], C.c_int32),
        "SecKeychainFindGenericPassword": ([pointer, C.c_uint32, C.c_char_p, C.c_uint32, C.c_char_p,
                                            C.POINTER(C.c_uint32), C.POINTER(pointer), C.POINTER(pointer)], C.c_int32),
        "SecKeychainAddGenericPassword": ([pointer, C.c_uint32, C.c_char_p, C.c_uint32, C.c_char_p,
                                           C.c_uint32, C.c_void_p, C.POINTER(pointer)], C.c_int32),
        "SecKeychainItemModifyAttributesAndData": ([pointer, pointer, C.c_uint32, C.c_void_p], C.c_int32),
        "SecKeychainItemFreeContent": ([pointer, pointer], C.c_int32),
        "SecTrustedApplicationCreateFromPath": ([C.c_char_p, C.POINTER(pointer)], C.c_int32),
        "SecAccessCreate": ([pointer, pointer, C.POINTER(pointer)], C.c_int32),
        "SecKeychainItemSetAccess": ([pointer, pointer], C.c_int32),
    }
    for name, (args, restype) in signatures.items():
        fn = getattr(security, name)
        fn.argtypes, fn.restype = args, restype
    status = security.SecKeychainSetUserInteractionAllowed(allow_interaction)
    if status:
        raise RuntimeError(f"macOS Keychain interaction policy failed (OSStatus {status})")
    core.CFRelease.argtypes = [pointer]
    core.CFStringCreateWithCString.argtypes = [pointer, C.c_char_p, C.c_uint32]
    core.CFStringCreateWithCString.restype = pointer
    core.CFArrayCreate.argtypes = [pointer, C.POINTER(pointer), C.c_long, pointer]
    core.CFArrayCreate.restype = pointer
    return security, core


def get_oauth_secret(reference: str, *, allow_interaction=False) -> str:
    security, core = _frameworks(allow_interaction=allow_interaction)
    pointer = C.c_void_p
    keychain, data, item = pointer(), pointer(), pointer()
    length = C.c_uint32()
    service, account = KEYCHAIN_SERVICE.encode(), reference.encode()
    try:
        status = security.SecKeychainOpen(LOGIN_KEYCHAIN.encode(), C.byref(keychain))
        if status == 0:
            status = security.SecKeychainFindGenericPassword(
                keychain, len(service), service, len(account), account,
                C.byref(length), C.byref(data), C.byref(item))
        if status:
            raise RuntimeError(f"macOS Keychain OAuth read failed (OSStatus {status}); unlock/authorize the login Keychain")
        return C.string_at(data, length.value).decode("utf-8")
    finally:
        if data:
            security.SecKeychainItemFreeContent(None, data)
        for obj in (item, keychain):
            if obj:
                core.CFRelease(obj)


def set_oauth_secret(reference: str, secret: str, *, allow_interaction=False) -> None:
    security, core = _frameworks(allow_interaction=allow_interaction)
    pointer = C.c_void_p

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
            # Rotation changes only the credential value. Replacing an existing
            # ACL requires interactive macOS authorization and can strand the
            # background refresh lock even though the new token was saved.
            return
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
