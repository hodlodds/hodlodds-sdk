"""Offline release metadata checks; no registry or PyPI requests."""
import fnmatch
import json
from pathlib import Path
import tomllib

import hodlodds
from hodlodds_mcp import __version__


PACKAGE = Path(__file__).resolve().parents[1]
REPO = PACKAGE.parent


def test_package_dependency_bounds_and_entry_point():
    project = tomllib.loads((PACKAGE / "pyproject.toml").read_text())["project"]
    assert project["name"] == "hodlodds-mcp"
    assert project["version"] == __version__ == "0.2.4"
    assert project["requires-python"] == ">=3.11"
    assert project["dependencies"] == ["hodlodds>=0.3.2,<0.4", "mcp>=2.3,<3"]
    assert project["scripts"] == {"hodlodds-mcp": "hodlodds_mcp.server:main"}
    assert project["authors"] == [{"name": "HodlOdds"}]
    assert project["urls"] == {
        "Homepage": "https://hodlodds.com",
        "Source": "https://github.com/hodlodds/hodlodds-sdk/tree/main/mcp",
        "Documentation": "https://api.hodlodds.com/docs",
    }
    sdk = tomllib.loads((REPO / "pyproject.toml").read_text())["project"]
    assert project["classifiers"] == sdk["classifiers"]
    assert sdk["version"] == hodlodds.__version__ == "0.3.4"
    assert "certifi>=2026.10.19" in sdk["dependencies"]
    assert sdk["authors"] == [{"name": "HodlOdds"}]
    assert sdk["requires-python"] == ">=3.10"
    assert Path(hodlodds.__file__).resolve() == REPO / "hodlodds/__init__.py"


def test_registry_metadata_matches_distribution():
    metadata = json.loads((PACKAGE / "server.json").read_text())
    assert metadata["name"] == "com.hodlodds/hodlodds-mcp"
    assert metadata["version"] == __version__
    assert "draft" not in json.dumps(metadata).lower()
    assert "post-launch" not in json.dumps(metadata).lower()
    assert set(metadata) == {"$schema", "name", "description", "version", "websiteUrl", "repository", "packages"}
    assert metadata["$schema"] == "https://static.modelcontextprotocol.io/schemas/2025-12-11/server.schema.json"
    assert len(metadata["description"]) <= 100
    assert metadata["repository"] == {"url": "https://github.com/hodlodds/hodlodds-sdk", "source": "github"}
    package, = metadata["packages"]
    assert package["registryType"] == "pypi"
    assert package["identifier"] == "hodlodds-mcp"
    assert package["version"] == __version__
    assert package["transport"] == {"type": "stdio"}
    assert {item["name"] for item in package["environmentVariables"]} == {
        "HODLODDS_BASE_URL", "HODLODDS_NETWORK", "HODLODDS_PAY_TO",
        "HODLODDS_KEYPAIR", "HODLODDS_MAX_PRICE_USDC", "HODLODDS_SESSION_BUDGET_USDC",
    }


def test_release_workflows_keep_sdk_and_mcp_artifacts_separate():
    workflow = (REPO / ".github/workflows/publish-mcp.yml").read_text()
    sdk_workflow = (REPO / ".github/workflows/publish.yml").read_text()
    assert 'tags: ["mcp-v*"]' in workflow
    assert 'tags: ["v*"]' in sdk_workflow
    assert fnmatch.fnmatchcase("mcp-v0.2.4", "mcp-v*")
    assert not fnmatch.fnmatchcase("mcp-v0.2.4", "v*")
    assert "environment: pypi-mcp" in workflow
    assert "id-token: write" in workflow
    assert "python -m build mcp" in workflow
    assert "pypa/gh-action-pypi-publish@release/v1" in workflow
    assert "packages-dir: mcp/dist/" in workflow


def test_public_instructions_describe_last_tool_defaults_and_payment_safety():
    readme = (PACKAGE / "README.md").read_text()
    # MCP Registry PyPI ownership marker: on its own line, matching server.json's name.
    assert "\n<!-- mcp-name: com.hodlodds/hodlodds-mcp -->\n" in readme
    assert "pip install hodlodds-mcp" in readme
    assert "uvx hodlodds-mcp" in readme
    assert "| `last_known(mint)` | 0.002" in readme
    assert "https://github.com/hodlodds/hodlodds-sdk/tree/main/mcp" in readme
    for field in ("dd_60", "already_crashed", "note", "confidence_detail",
                  "defaults_applied", "total_matches", "server_code", "offer_changed",
                  "include_crashed", "safest", "crashed_within_24h", "last_crash_as_of",
                  "excluded_by_defaults", "min_liquidity_sol=5", "client_refused"):
        assert field in readme
    assert "Watchlists get **none of these defaults**" in readme
    assert "Before 0.2.1 these refusals were reported as `paid_request_failed`." in " ".join(readme.split())
    assert " ".join("""This board costs 0.05 USDC but HODLODDS_MAX_PRICE_USDC is
        0.01; raise it to at least 0.05.""".split()) in " ".join(readme.split())
    assert "**before signing only**" in readme
    assert "A signed request is never retried automatically" in readme
    assert "`confidence` remains the level string" in readme
    assert "`high`, `medium`, `low`, or `missing`" in readme
    assert "Odds, not guarantees. Not financial advice." in readme
    assert "R80" not in readme and "sdk/python" not in readme


def test_sdk_public_instructions_describe_current_board_and_last_behaviour():
    readme = (REPO / "README.md").read_text()
    for field in ("dd_60", "already_crashed", "note", "defaults_applied",
                  "include_crashed", "--include-crashed", "offer_changed",
                  "client.last", "safest", "crashed_within_24h", "last_crash_as_of",
                  "excluded_by_defaults", "min_liquidity_sol=5", "PriceLimitError"):
        assert field in readme
    assert "none of these defaults" in readme
    assert "**once only if the" in readme and "409 occurs before signing**" in readme
    assert "**never retried automatically**" in readme
    assert "after the first mcp-v0.1.0 release" not in readme


def test_both_readmes_explain_signed_offer_changed_safety():
    for path in (REPO / "README.md", PACKAGE / "README.md"):
        text = " ".join(path.read_text().split())
        assert 'code: "offer_changed"' in text
        assert "HodlOdds-Charged: no" in text
        assert "nothing was charged, but the client must call again" in text
        assert "the SDK never re-signs on its own" in text
        assert "A second automatic authorization could be settled twice if a server misbehaved." in text
