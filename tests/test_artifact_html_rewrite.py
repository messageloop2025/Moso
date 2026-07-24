"""artifact HTML 子资源 token 改写：src/href + importmap + ESM relative import。"""

from api.ai_artifacts import (
    _is_relative_asset_url,
    _rewrite_artifact_html_refs_for_token,
)


UUID = "3df5c3ac6a3f47289356f5ca88e8fe44"
TOKEN = "test.jwt.token"


def test_is_relative_asset_url():
    assert _is_relative_asset_url("./libs/three.module.js")
    assert _is_relative_asset_url("libs/echarts.min.js")
    assert _is_relative_asset_url("three.min.js")
    assert not _is_relative_asset_url("three")  # bare specifier
    assert not _is_relative_asset_url("/api/ai/artifacts/x/files/a.js")
    assert not _is_relative_asset_url("https://cdn.example/x.js")
    assert not _is_relative_asset_url("../escape.js")


def test_rewrite_script_src_keeps_token():
    html = '<script src="./libs/three.min.js"></script>'
    out = _rewrite_artifact_html_refs_for_token(html, UUID, TOKEN)
    assert f"/api/ai/artifacts/{UUID}/files/libs/three.min.js?token=" in out
    assert TOKEN in out


def test_rewrite_importmap_three_module():
    html = """
<script type="importmap">
{
  "imports": {
    "three": "./libs/three.module.js"
  }
}
</script>
<script type="module">
import * as THREE from 'three';
</script>
"""
    out = _rewrite_artifact_html_refs_for_token(html, UUID, TOKEN)
    assert '"three"' in out  # bare key 不变
    assert "import * as THREE from 'three'" in out
    assert f"/api/ai/artifacts/{UUID}/files/libs/three.module.js?token=" in out
    assert "./libs/three.module.js" not in out


def test_rewrite_esm_relative_from():
    html = """
<script type="module">
import * as THREE from './libs/three.module.js';
import('./libs/helper.js');
</script>
"""
    out = _rewrite_artifact_html_refs_for_token(html, UUID, TOKEN)
    assert "./libs/three.module.js" not in out
    assert "./libs/helper.js" not in out
    assert f"/api/ai/artifacts/{UUID}/files/libs/three.module.js?token=" in out
    assert f"/api/ai/artifacts/{UUID}/files/libs/helper.js?token=" in out
