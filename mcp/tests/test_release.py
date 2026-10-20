"""Offline release metadata checks; no registry or PyPI requests."""
import fnmatch
import json
from pathlib import Path
import tomllib

from hodlodds_mcp import __version__


PACKAGE = Path(__file__).resolve().parents[1]
REPO = PACKAGE.parent


def test_package_dependency_bounds_and_entry_point():
    project = tomllib.loads((PACKAGE / "pyproject.toml").read_text())["project"]
    assert project["name"] == "hodlodds-mcp"
    assert project["version"] == __version__ == "0.1.0"
    assert project["requires-python"] == ">=3.11"
    assert project["dependencies"] == ["hodlodds>=0.3.0,<0.4", "mcp>=2.3,<3"]
    assert project["scripts"] == {"hodlodds-mcp": "hodlodds_mcp.server:main"}


def test_registry_metadata_matches_distribution():
    metadata = json.loads((PACKAGE / "server.json").read_text())
    assert metadata["name"] == "com.hodlodds/hodlodds-mcp"
    assert metadata["version"] == __version__
    assert "draft" not in json.dumps(metadata).lower()
    assert "post-launch" not in json.dumps(metadata).lower()
    assert set(metadata) == {"name", "description", "version", "packages"}
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
    assert fnmatch.fnmatchcase("mcp-v0.1.0", "mcp-v*")
    assert not fnmatch.fnmatchcase("mcp-v0.1.0", "v*")
    assert "environment: pypi-mcp" in workflow
    assert "id-token: write" in workflow
    assert "python -m build mcp" in workflow
    assert "pypa/gh-action-pypi-publish@release/v1" in workflow
    assert "packages-dir: mcp/dist/" in workflow


def test_public_instructions_do_not_advertise_an_unimplemented_last_tool():
    readme = (PACKAGE / "README.md").read_text()
    assert "pip install hodlodds-mcp" in readme
    assert "uvx hodlodds-mcp" in readme
    assert "/v1/risk/{mint}/last` costs 0.002" in readme
    assert "exposes no last-known MCP tool" in readme
    assert "Odds, not guarantees. Not financial advice." in readme
    assert "R80" not in readme and "sdk/python" not in readme
