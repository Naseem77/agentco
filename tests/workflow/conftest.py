import pytest
from orch_helpers import build_rig


@pytest.fixture
def make_rig(tmp_path, store, loop_cfg, make_repo):
    async def make(script, **kwargs):
        return await build_rig(tmp_path, store, loop_cfg, make_repo, script, **kwargs)

    return make
