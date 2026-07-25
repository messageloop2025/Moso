# three.js（本地 vendor）

版本：**0.160.0**（MIT）

| 文件 | 用途 |
|------|------|
| `three.min.js` | UMD，全局 `window.THREE`（简单 HTML / 聊天 three-scene） |
| `three.module.js` | ESM 主包 |
| `jsm/controls/OrbitControls.js` | 轨道相机控制（`three/addons/controls/OrbitControls.js`） |
| `jsm/renderers/CSS2DRenderer.js` | CSS2D 标签 |
| `jsm/loaders/GLTFLoader.js` | glTF 加载（依赖下方 utils） |
| `jsm/utils/BufferGeometryUtils.js` | GLTFLoader 依赖 |

## HTML 报告推荐写法

```html
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
```

`create_chat_artifact(..., libs=["three"])` 会把上表文件复制到 artifact 的 `libs/` 下。

**禁止**使用 `cdn.jsdelivr.net` / `unpkg.com` 等外网 CDN。
