"""Keep public documentation aligned with the packaged Hermes provider."""

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
ROOT_README = ROOT / "README.md"
PLUGIN_README = ROOT / "src" / "hermes_xmemo" / "xmemo" / "README.md"


def test_readme_brand_assets_exist_and_are_referenced() -> None:
    readme = ROOT_README.read_text(encoding="utf-8")

    for asset in (
        "assets/icon.png",
        "assets/hermes-architecture.svg",
        "assets/hermes-setup-flow.svg",
    ):
        assert (ROOT / asset).is_file()
        assert asset in readme


def test_release_badge_tracks_the_workflow_push_event() -> None:
    readme = ROOT_README.read_text(encoding="utf-8")

    assert "pypi-publish.yml?event=push" in readme
    assert "https://pypi.org/project/hermes-xmemo/" in readme


def test_root_and_packaged_readmes_share_install_and_discovery_contract() -> None:
    root = ROOT_README.read_text(encoding="utf-8")
    packaged = PLUGIN_README.read_text(encoding="utf-8")

    required_markers = (
        "xmemo setup hermes",
        "hermes-xmemo install",
        "hermes memory setup xmemo",
        "https://xmemo.dev/.well-known/agent-discovery.json",
        "https://xmemo.dev/v1/mcp/config/hermes",
        "https://xmemo.dev/mcp",
        "$HERMES_HOME/xmemo.json",
        "$HERMES_HOME/xmemo_cache.db",
    )

    for marker in required_markers:
        assert marker in root
        assert marker in packaged


def test_documented_default_tools_match_provider_surface() -> None:
    root = ROOT_README.read_text(encoding="utf-8")
    packaged = PLUGIN_README.read_text(encoding="utf-8")

    for tool in (
        "xmemo_recall_context",
        "xmemo_search",
        "xmemo_remember",
        "xmemo_update_state",
    ):
        assert tool in root
        assert tool in packaged
