import ctypes

import pytest

from codex_mail_workbench import keychain


def test_oauth_rotation_preserves_existing_acl_and_avoids_access_prompt(monkeypatch):
    calls = []
    class Function:
        def __init__(self, name): self.name = name
        def __call__(self, *args):
            calls.append(self.name)
            if self.name == "SecKeychainFindGenericPassword":
                ctypes.cast(args[-1], ctypes.POINTER(ctypes.c_void_p))[0] = ctypes.c_void_p(1)
            return 0
    class Library:
        def __init__(self): self.functions = {}
        def __getattr__(self, name):
            return self.functions.setdefault(name, Function(name))
    monkeypatch.setattr(keychain.sys, "platform", "darwin")
    monkeypatch.setattr(keychain.C, "CDLL", lambda path: Library())
    keychain.set_oauth_secret("test-ref", "renewed-test-credential")
    assert calls.count("SecKeychainItemModifyAttributesAndData") == 1
    assert "SecKeychainAddGenericPassword" not in calls
    assert "SecAccessCreate" not in calls and "SecKeychainItemSetAccess" not in calls


def test_oauth_read_preserves_long_credentials_and_releases_native_content(monkeypatch):
    value = ('{"access_token":"' + "x" * 16000 + '"}').encode()
    buffer = ctypes.create_string_buffer(value)
    released = []

    class Security:
        def SecKeychainOpen(self, path, target):
            ctypes.cast(target, ctypes.POINTER(ctypes.c_void_p))[0] = 1
            return 0

        def SecKeychainFindGenericPassword(self, *args):
            ctypes.cast(args[5], ctypes.POINTER(ctypes.c_uint32))[0] = len(value)
            ctypes.cast(args[6], ctypes.POINTER(ctypes.c_void_p))[0] = ctypes.addressof(buffer)
            ctypes.cast(args[7], ctypes.POINTER(ctypes.c_void_p))[0] = 2
            return 0

        def SecKeychainItemFreeContent(self, attributes, data):
            released.append("content")

    class Core:
        def CFRelease(self, obj):
            released.append(obj.value)

    monkeypatch.setattr(keychain, "_frameworks", lambda: (Security(), Core()))
    assert keychain.get_oauth_secret("test-ref") == value.decode()
    assert released == ["content", 2, 1]


def test_oauth_read_reports_authorization_failure_without_treating_it_as_missing(monkeypatch):
    class Security:
        def SecKeychainOpen(self, *args):
            return -25308  # errSecInteractionNotAllowed

    monkeypatch.setattr(keychain, "_frameworks", lambda: (Security(), object()))
    with pytest.raises(RuntimeError, match="OSStatus -25308"):
        keychain.get_oauth_secret("test-ref")
