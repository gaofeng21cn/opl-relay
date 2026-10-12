import ctypes

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
