# 文件系统（批量操作资源）

本目录为 毛竹 文件系统根目录，用于存放批量操作所需的**脚本、文档与资源**。

- **scripts/**：可被「批量执行脚本」引用的脚本文件（如 `scripts/restart_nginx.sh`）。在创建批量任务时 `script_path` 填相对路径，如 `scripts/restart_nginx.sh`。
- **docs/**：文档或其它资源，可按需放置。

路径均相对本目录，禁止使用 `..` 逃逸。通过「文件系统」页或 API `/api/fs/list`、`/api/fs/read` 等管理；AI 助手可通过 `batch_create(operation_type='run_script', params={script_path: 'scripts/xxx.sh'})` 进行批量执行。
