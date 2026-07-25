"""artifact HTML 子资源 token 改写：path 内嵌 token + importmap 目录前缀。"""

from api.ai_artifacts import (
    _artifact_error_response,
    _artifact_file_url_with_token,
    _is_relative_asset_url,
    _resolve_manifest_lib_fallback,
    _rewrite_artifact_html_refs_for_token,
)


UUID = "3df5c3ac6a3f47289356f5ca88e8fe44"
TOKEN = "test.jwt.token"


def test_is_relative_asset_url():
    assert _is_relative_asset_url("./libs/three.module.js")
    assert _is_relative_asset_url("./libs/jsm/")
    assert _is_relative_asset_url("libs/echarts.min.js")
    assert _is_relative_asset_url("three.min.js")
    assert not _is_relative_asset_url("three")  # bare specifier
    assert not _is_relative_asset_url("/api/ai/artifacts/x/files/a.js")
    assert not _is_relative_asset_url("https://cdn.example/x.js")
    assert not _is_relative_asset_url("../escape.js")


def test_path_token_url_preserves_trailing_slash():
    u = _artifact_file_url_with_token(UUID, "libs/jsm/", TOKEN)
    assert u.endswith("/files/libs/jsm/")
    assert f"/at/{TOKEN}/files/" in u
    assert "?" not in u


def test_rewrite_script_src_uses_path_token():
    html = '<script src="./libs/three.min.js"></script>'
    out = _rewrite_artifact_html_refs_for_token(html, UUID, TOKEN)
    assert f"/api/ai/artifacts/{UUID}/at/{TOKEN}/files/libs/three.min.js" in out
    assert "?token=" not in out


def test_rewrite_importmap_three_addons_prefix():
    html = """
<script type="importmap">
{
  "imports": {
    "three": "./libs/three.module.js",
    "three/addons/": "./libs/jsm/"
  }
}
</script>
<script type="module">
import * as THREE from 'three';
import { OrbitControls } from 'three/addons/controls/OrbitControls.js';
</script>
"""
    out = _rewrite_artifact_html_refs_for_token(html, UUID, TOKEN)
    assert '"three"' in out
    assert '"three/addons/"' in out
    assert f"/at/{TOKEN}/files/libs/three.module.js" in out
    # 目录前缀 address 必须以 / 结尾（Import Maps 规范）
    assert f"/at/{TOKEN}/files/libs/jsm/" in out
    assert '"./libs/jsm/"' not in out
    assert "import { OrbitControls } from 'three/addons/controls/OrbitControls.js'" in out


def test_rewrite_esm_relative_from():
    html = """
<script type="module">
import * as THREE from './libs/three.module.js';
import('./libs/helper.js');
</script>
"""
    out = _rewrite_artifact_html_refs_for_token(html, UUID, TOKEN)
    assert "./libs/three.module.js" not in out
    assert f"/at/{TOKEN}/files/libs/three.module.js" in out
    assert f"/at/{TOKEN}/files/libs/helper.js" in out


def test_manifest_lib_fallback_three_jsm():
    from api.ai_artifacts import load_html_libs_manifest

    m = load_html_libs_manifest(force_reload=True)
    three_files = (m.get("packages") or {}).get("three", {}).get("files") or []
    assert "jsm/controls/OrbitControls.js" in three_files
    assert "three.module.js" in three_files

    p = _resolve_manifest_lib_fallback("libs/jsm/controls/OrbitControls.js")
    assert p is not None
    assert p.is_file()
    assert p.name == "OrbitControls.js"
    assert _resolve_manifest_lib_fallback("libs/not-a-real-vendor.js") is None
    assert _resolve_manifest_lib_fallback("../escape.js") is None


def test_artifact_error_response_has_cors():
    resp = _artifact_error_response(404, "文件不存在")
    assert resp.status_code == 404
    assert resp.headers.get("Access-Control-Allow-Origin") == "*"
