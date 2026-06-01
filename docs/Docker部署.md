# 毛竹 Docker 部署说明

应用代码在镜像内；与 `docker-compose.yml` **同级**的 `data/` 目录集中挂载持久化数据。

## 1. 目录结构（compose 同级）

```
edgeops-1.3.6/          # 发行包解压目录示例（版本以 config.VERSION / 包名为准）
├── docker-compose.yml
├── edgeops-v1.3.6.tar   # 或 edgeops-v<版本>.tgz
└── data/
    ├── data/           → 容器 /app/data      （edgeops.db 等）
    ├── fs/             → 容器 /app/web/fs    （用户工作区、聊天附件）
    └── logs/           → 容器 /app/logs      （edgeops.log）
```

发行包构建时会自动创建 `data/data`、`data/fs`、`data/logs` 及 `.gitkeep`。

## 2. 开发环境（Git 仓库）

在项目根先确保目录存在：

```bash
mkdir -p data/data data/fs data/logs
```

```bash
docker compose -f docker/docker-compose.yml up -d --build
```

卷映射（相对 `docker/docker-compose.yml`）：

| 宿主机 | 容器 |
|--------|------|
| `../data/data` | `/app/data` |
| `../data/fs` | `/app/web/fs` |
| `../data/logs` | `/app/logs` |

若此前数据库在 `data/edgeops.db`，请移至 `data/data/edgeops.db` 后再启动。

## 3. 发行包（`edgeops-v<版本>.tgz`）

```bat
build-and-export.bat
```

解压后仅含目录 `edgeops-<版本>/`，其内同一层包括：

```text
edgeops-<版本>/
  docker-compose.yml
  run.sh / run.bat / start-compose.*
  edgeops-v<版本>.tar
  data/data/  data/fs/  data/logs/
```

进入 `edgeops-<版本>/` 后执行 `start-compose.bat` 或 `./start-compose.sh`。

## 4. 常用命令

```bash
docker compose -f docker/docker-compose.yml logs -f edgeops
docker compose -f docker/docker-compose.yml down
docker compose -f docker/docker-compose.yml up -d --build
```

修改代码或 `requirements.txt` 后必须 `--build` 重建镜像。
