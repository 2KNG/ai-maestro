"""Remote aliases dispatch without quota collection or unintended setup."""
import sys
from unittest.mock import patch
import pytest
import maestro
import maestro_remote


@pytest.mark.parametrize('command,arguments,expected', [
    ('remote', ['status'], ['status']),
    ('orca-remote', ['up', '--node-id', 'pc2'], ['up', '--node-id', 'pc2']),
    ('fleet', ['install-node', '--dry-run'], ['install-node', '--dry-run']),
    ('fleet-setup', ['--node-id', 'pc3'], ['up', '--node-id', 'pc3']),
    ('remote-setup', [], ['up']),
    ('remote-setup', ['status'], ['status']),
])
def test_remote_alias_dispatch(command, arguments, expected):
    with patch.object(sys, 'argv', ['maestro', command, *arguments]), \
            patch.object(maestro_remote, 'main', return_value=0) as remote, \
            patch.object(maestro.cau, 'collect_usage') as usage, pytest.raises(SystemExit) as stopped:
        maestro.main()
    assert stopped.value.code == 0
    remote.assert_called_once_with(expected)
    usage.assert_not_called()
