import hashlib
import json
import os
import subprocess
import struct
import tomllib
from pathlib import Path

from codex_mail_workbench import __version__
from codex_mail_workbench.workspace import initialize_workspace


ROOT = Path(__file__).parents[1]
PLUGIN_ROOT = ROOT / "plugins" / "opl-relay"
PACKAGE_PATH = PLUGIN_ROOT / "opl-package.json"
LEGACY_PACKAGE_PATH = ROOT / "packages" / "opl-relay" / "package.json"
PLUGIN_PATH = PLUGIN_ROOT / ".codex-plugin" / "plugin.json"
ICON_PATH = PLUGIN_ROOT / "assets" / "icon.png"
SKILL_PATH = PLUGIN_ROOT / "skills" / "opl-relay" / "SKILL.md"

CAPABILITY_IDS = {
    "communications.mail.v1",
    "personal.context.v1",
    "personal.memory.v1",
    "knowledge.obsidian.v1",
}
VIEW_TYPES = {"list_detail", "timeline", "approval_diff"}
ACTION_REFS = {
    "communications.mail.v1#sync.incremental",
    "communications.mail.v1#draft.create",
    "communications.mail.v1#draft.create_from_persona",
    "communications.mail.v1#draft.inspect",
    "communications.mail.v1#draft.open",
    "communications.mail.v1#draft.send",
    "personal.context.v1#build",
    "personal.memory.v1#inspect",
}

DATA_REFS = {
    "communications.mail.v1#recent",
    "communications.mail.v1#triage.evidence",
    "communications.mail.v1#draft.inspect",
    "personal.memory.v1#search",
}


def load_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def test_package_identity_capabilities_and_plugin_version_are_aligned() -> None:
    package = load_json(PACKAGE_PATH)
    plugin = load_json(PLUGIN_PATH)
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))

    assert package["surface_kind"] == "opl_capability_package_manifest.v2"
    assert package["package_id"] == "opl-relay"
    assert package["package_role"] == "capability_package"
    assert (
        package["version"]
        == plugin["version"]
        == project["project"]["version"]
        == __version__
    )
    assert set(project["project"]["scripts"]) == {"opl-relay"}
    assert set(package["exports"]["core_module_ids"]) == CAPABILITY_IDS
    assert package["exports"]["core_skill_ids"] == ["opl-relay"]
    assert package["codex_surface"]["plugin_id"] == plugin["name"] == "opl-relay"
    assert package["codex_surface"]["configured_codex_plugin_carrier"] == {
        "kind": "codex_plugin_manager",
        "plugin_selector": "opl-relay@opl-relay",
        "executor_route": "codex_cli",
        "marketplace_source": "gaofeng21cn/opl-relay",
        "publication_ref": "ghcr.io/gaofeng21cn/one-person-lab-packages/opl-relay:latest-stable",
    }

    skill = SKILL_PATH.read_text(encoding="utf-8")
    assert all(capability_id in skill for capability_id in CAPABILITY_IDS)
    assert "app_contributions" not in plugin


def test_package_descriptor_has_one_carrier_root_authority() -> None:
    assert PACKAGE_PATH.is_file()
    assert not LEGACY_PACKAGE_PATH.exists()


def test_package_content_lock_matches_plugin_bytes() -> None:
    package = load_json(PACKAGE_PATH)
    content_lock = package["content_lock"]
    digest = hashlib.sha256()

    assert content_lock["algorithm"] == "sha256"
    assert content_lock["canonicalization"] == "ordered_path_length_file_length_bytes"
    assert "opl-package.json" not in content_lock["paths"]
    for relative_path in content_lock["paths"]:
        path_bytes = relative_path.encode("utf-8")
        file_bytes = (PLUGIN_ROOT / relative_path).read_bytes()
        digest.update(len(path_bytes).to_bytes(8, "big"))
        digest.update(path_bytes)
        digest.update(len(file_bytes).to_bytes(8, "big"))
        digest.update(file_bytes)

    assert content_lock["digest"] == f"sha256:{digest.hexdigest()}"


def test_plugin_uses_a_dedicated_small_format_icon() -> None:
    plugin = load_json(PLUGIN_PATH)
    interface = plugin["interface"]
    icon = ICON_PATH.read_bytes()

    assert interface["composerIcon"] == "./assets/icon.png"
    assert interface["logo"] == "./assets/icon.png"
    assert interface["websiteURL"] == "https://github.com/gaofeng21cn/opl-relay"
    assert interface["brandColor"] == "#1260E8"
    assert icon.startswith(b"\x89PNG\r\n\x1a\n")
    assert struct.unpack(">II", icon[16:24]) == (512, 512)
    assert icon[25] in {4, 6} or (icon[25] == 3 and b"tRNS" in icon)
    assert "assets/icon.png" in load_json(PACKAGE_PATH)["content_lock"]["paths"]


def test_app_contributions_are_role_neutral_and_reference_existing_cli_actions() -> None:
    contributions = load_json(PACKAGE_PATH)["app_contributions"]
    abi = load_json(PACKAGE_PATH)["codex_surface"]["app_contribution_abi"]

    assert contributions["schema_version"] == "opl-app-contributions.v1"
    assert set(contributions) <= {
        "schema_version",
        "navigation",
        "views",
        "commands",
        "badges",
    }

    navigation_ids = [item["navigation_id"] for item in contributions["navigation"]]
    view_ids = [item["view_id"] for item in contributions["views"]]
    command_ids = [item["command_id"] for item in contributions["commands"]]
    assert len(navigation_ids) == len(set(navigation_ids))
    assert len(view_ids) == len(set(view_ids))
    assert len(command_ids) == len(set(command_ids))

    assert {item["view_type"] for item in contributions["views"]} == VIEW_TYPES
    assert {item["view_id"] for item in contributions["navigation"]} <= set(view_ids)
    assert {
        command_id
        for view in contributions["views"]
        for command_id in view.get("command_ids", [])
    } <= set(command_ids)
    assert {item["action_ref"] for item in contributions["commands"]} == ACTION_REFS
    assert {item["data_ref"] for item in contributions["views"]} == DATA_REFS
    assert abi == {
        "schema_version": "opl-package-app-contribution-cli.v1",
        "transport": "stdin_json_stdout_json",
        "argv": ["./bin/opl-relay", "--json", "app-contribution"],
        "request_schema": "opl-package-app-contribution-request.v1",
        "response_schema": "opl-package-app-contribution-response.v1",
    }
    assert abi["argv"][0].startswith("./bin/")
    commands_by_id = {
        item["command_id"]: item for item in contributions["commands"]
    }
    assert commands_by_id["relay.draft.send"]["confirmation_required"] is True
    assert commands_by_id["relay.draft.create-from-persona"]["confirmation_required"] is True
    assert all(
        "confirmation_required" not in command
        for command_id, command in commands_by_id.items()
        if command_id not in {"relay.draft.send", "relay.draft.create-from-persona"}
    )

    serialized = json.dumps(contributions)
    assert "standard_agent" not in serialized
    assert not ({"component", "code", "path", "url"} & set(contributions))


def test_app_contribution_abi_executes_from_the_plugin_carrier(tmp_path: Path) -> None:
    abi = load_json(PACKAGE_PATH)["codex_surface"]["app_contribution_abi"]
    profile_workspace = tmp_path / "profile"
    initialize_workspace(profile_workspace)

    env = os.environ.copy()
    env.pop("PYTHONPATH", None)
    env["OPL_PROFILE_WORKSPACE"] = str(profile_workspace)
    result = subprocess.run(
        abi["argv"],
        cwd=PLUGIN_ROOT,
        input=json.dumps(
            {
                "schema_version": "opl-package-app-contribution-request.v1",
                "operation": "read",
                "ref": "communications.mail.v1#recent",
                "input": {},
            }
        ),
        check=False,
        capture_output=True,
        text=True,
        env=env,
    )

    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {
        "schema_version": "opl-package-app-contribution-response.v1",
        "ok": True,
        "ref": "communications.mail.v1#recent",
        "operation": "read",
        "result": {"messages": []},
    }


def test_repo_marketplace_exposes_the_plugin_without_owning_user_data() -> None:
    marketplace = load_json(ROOT / ".agents" / "plugins" / "marketplace.json")
    assert marketplace["name"] == "opl-relay"
    assert marketplace["interface"]["displayName"] == "OPL Relay"
    assert marketplace["plugins"] == [
        {
            "name": "opl-relay",
            "source": {"source": "local", "path": "./plugins/opl-relay"},
            "policy": {
                "installation": "AVAILABLE",
                "authentication": "ON_INSTALL",
            },
            "category": "Productivity",
        }
    ]
