from pathlib import Path

import pytest
from test_host import running_host

from pico.desktop_bootstrap import bootstrap
from pico.state_store import ApplicationError


def test_bootstrap_resolves_alias_and_reuses_existing_host(tmp_path):
    root = tmp_path / "workspace"
    root.mkdir()
    alias = tmp_path / "alias"
    alias.symlink_to(root, target_is_directory=True)
    with running_host(root, []) as (client, _, _):
        expected = client.call("host.info")
        result = bootstrap(alias)
        assert result["workspace"] == str(root.resolve())
        assert result["info"]["instance_id"] == expected["instance_id"]
        assert Path(result["socket"]) == client.path
        assert result["info"]["capabilities"]


def test_bootstrap_rejects_files_and_missing_directories(tmp_path):
    file = tmp_path / "file"
    file.write_text("not a workspace")
    with pytest.raises(ApplicationError, match="directory"):
        bootstrap(file)
    with pytest.raises(FileNotFoundError):
        bootstrap(tmp_path / "absent")
