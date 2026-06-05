from __future__ import annotations

import argparse
import ast
import os
import runpy
import shutil
import subprocess
import sys
from pathlib import Path

import python_minifier


ROOT_DIR = Path(__file__).resolve().parents[1]
DEFAULT_PLATFORM = "linux.x86_64"
TARGET_PYTHON_FEATURE_VERSION = (3, 11)
RUNTIME_INPUTS = ["app.py", "config.py", "api", "services", "database"]
KEEP_WEB_FS = {".gitkeep", "README.md"}


def load_version() -> str:
    data = runpy.run_path(str(ROOT_DIR / "config.py"))
    version = str(data.get("VERSION") or "").strip()
    if not version:
        raise RuntimeError("无法从 config.py 读取 VERSION")
    return version


def locate_pyarmor() -> str:
    candidates = [
        shutil.which("pyarmor"),
        str(Path(sys.executable).parent / "Scripts" / "pyarmor.exe"),
        str(Path(sys.executable).parent / "pyarmor"),
    ]
    for item in candidates:
        if item and Path(item).exists():
            return item
    raise RuntimeError(
        "未找到 pyarmor，可先执行: python -m pip install pyarmor pyarmor.cli.core.linux"
    )


def locate_docker() -> str | None:
    candidates = [
        shutil.which("docker"),
        shutil.which("docker.exe"),
        str(Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "Docker" / "Docker" / "resources" / "bin" / "docker.exe"),
    ]
    for item in candidates:
        if item and Path(item).exists():
            return item
    return None


def reset_dir(path: Path) -> None:
    if path.exists():
        shutil.rmtree(path)
    path.mkdir(parents=True, exist_ok=True)


def copy_web_tree(src: Path, dst: Path) -> None:
    def ignore(current: str, names: list[str]) -> set[str]:
        ignored: set[str] = set()
        rel = Path(current).resolve().relative_to(src.resolve())
        if "__pycache__" in names:
            ignored.add("__pycache__")
        for name in names:
            if name.endswith((".pyc", ".pyo")):
                ignored.add(name)
        if rel == Path("fs"):
            for name in names:
                if name not in KEEP_WEB_FS:
                    ignored.add(name)
        return ignored

    shutil.copytree(src, dst, ignore=ignore)
    fs_dir = dst / "fs"
    fs_dir.mkdir(parents=True, exist_ok=True)
    for keep_name in KEEP_WEB_FS:
        keep_path = fs_dir / keep_name
        if keep_name == ".gitkeep" and not keep_path.exists():
            keep_path.write_text("", encoding="utf-8")


def ensure_release_persist_dirs(release_dir: Path) -> None:
    """与 docker-compose.yml 同级：data/data（库）、data/fs（用户文件）、data/logs（运行日志）。"""
    base = release_dir / "data"
    for sub in ("data", "fs", "logs"):
        d = base / sub
        d.mkdir(parents=True, exist_ok=True)
        gitkeep = d / ".gitkeep"
        if not gitkeep.exists():
            gitkeep.write_text("", encoding="utf-8")
    readme_src = ROOT_DIR / "web" / "fs" / "README.md"
    readme_dst = base / "fs" / "README.md"
    if readme_src.is_file():
        shutil.copy2(readme_src, readme_dst)


def copy_runtime_assets(image_ctx: Path) -> None:
    """复制进 Docker 构建上下文（仅用于打镜像，不进入发行压缩包）。"""
    shutil.copy2(ROOT_DIR / "requirements.txt", image_ctx / "requirements.txt")
    copy_web_tree(ROOT_DIR / "web", image_ctx / "web")


def generate_dockerfile(release_dir: Path, version: str) -> None:
    """与仓库 docker/Dockerfile 保持一致：代码在镜像内，compose 仅挂载 data 与 web/fs。

    层顺序刻意把 ARG/ENV 版本号放在依赖安装之后，避免仅改 VERSION 或业务代码时
    击穿 apt/pip 层缓存。依赖层仅随 requirements.txt 或系统包列表变化而重建。
    """
    content = f"""# 毛竹 发行镜像（由 scripts/build_release.py 生成，与 docker/Dockerfile 对齐）
FROM python:3.11-slim

WORKDIR /app

RUN apt-get update \\
    && apt-get install -y --no-install-recommends libmagic1 openssh-client sshpass \\
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN --mount=type=cache,target=/root/.cache/pip \\
    pip install -r requirements.txt \\
    && python -c "from markitdown import MarkItDown; print('markitdown OK')"

COPY . /app

ARG EDGEOPS_VERSION={version}
ENV EDGEOPS_VERSION=${{EDGEOPS_VERSION}}

RUN mkdir -p /app/data /app/web/fs /app/logs

ENV EDGEOPS_HOST=0.0.0.0 \\
    EDGEOPS_PORT=8010 \\
    EDGEOPS_DB=/app/data/edgeops.db

EXPOSE 8010

VOLUME ["/app/data", "/app/web/fs", "/app/logs"]

CMD ["sh", "-c", "mkdir -p /app/logs && uvicorn app:app --host ${{EDGEOPS_HOST:-0.0.0.0}} --port ${{EDGEOPS_PORT:-8010}} --forwarded-allow-ips='*' 2>&1 | tee -a /app/logs/edgeops.log"]
"""
    (release_dir / "Dockerfile").write_text(content, encoding="utf-8")


def generate_dockerignore(release_dir: Path, used_mode: str = "") -> None:
    # 关键：pyc 模式下发行目录里只剩 .pyc（.py 已被删除），绝不能把 .pyc 忽略掉，否则镜像里没可运行的代码。
    # 其他模式（pyarmor / minify）里根本不该有 .pyc，排除掉无影响且更干净。
    if used_mode == "pyc":
        py_ignore_line = "# pyc 模式：发行目录已经是字节码，保留 .pyc 入镜像；仅忽略 .pyo/.pyd\n*.py[od]\n"
    else:
        py_ignore_line = "*.py[cod]\n"
    content = (
        "__pycache__\n"
        + py_ignore_line
        + "*.log\n"
          "*.db\n"
          "*.db-journal\n"
          "*.db-wal\n"
          "*.db-shm\n"
          "build/\n"
          "data/data/*\n"
          "!data/data/.gitkeep\n"
          "data/fs/*\n"
          "!data/fs/.gitkeep\n"
          "!data/fs/README.md\n"
          "data/logs/*\n"
          "!data/logs/.gitkeep\n"
          "web/fs/*\n"
          "!web/fs/.gitkeep\n"
          "!web/fs/README.md\n"
    )
    (release_dir / ".dockerignore").write_text(content, encoding="utf-8")


def generate_docker_compose(release_dir: Path, version: str, tag: str) -> None:
    content = f"""services:
  edgeops:
    image: {tag}
    container_name: edgeops-{version}
    ports:
      - "8010:8010"
    environment:
      EDGEOPS_DB: /app/data/edgeops.db
      EDGEOPS_HOST: "0.0.0.0"
      EDGEOPS_PORT: "8010"
      EDGEOPS_VERSION: "{version}"
      # EDGEOPS_SECRET: "your-jwt-secret"
      # AI_API_KEY: ""
      # AI_BASE_URL: "https://dashscope.aliyuncs.com/compatible-mode/v1"
      # AI_MODEL: "qwen3.5-plus"
    volumes:
      - ./data/data:/app/data
      - ./data/fs:/app/web/fs
      - ./data/logs:/app/logs
    restart: unless-stopped
"""
    (release_dir / "docker-compose.yml").write_text(content, encoding="utf-8")


def generate_linux_scripts(app_dir: Path, version: str, tag: str) -> None:
    tar_name = f"edgeops-v{version}.tar"
    run_sh = f"""#!/usr/bin/env sh
set -eu

SCRIPT_DIR="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
VERSION="{version}"
IMAGE="{tag}"
TAR_PATH="$SCRIPT_DIR/{tar_name}"

if ! command -v docker >/dev/null 2>&1; then
  echo "docker 未安装或不在 PATH 中"
  exit 1
fi

if ! docker image inspect "$IMAGE" >/dev/null 2>&1; then
  if [ ! -f "$TAR_PATH" ]; then
    echo "未找到镜像 $IMAGE，且未找到镜像包: $TAR_PATH"
    exit 1
  fi
  echo "导入镜像: $TAR_PATH"
  docker load -i "$TAR_PATH"
fi

mkdir -p "$SCRIPT_DIR/data/data" "$SCRIPT_DIR/data/fs" "$SCRIPT_DIR/data/logs"

if docker compose version >/dev/null 2>&1; then
  docker compose -f "$SCRIPT_DIR/docker-compose.yml" up -d
elif command -v docker-compose >/dev/null 2>&1; then
  docker-compose -f "$SCRIPT_DIR/docker-compose.yml" up -d
else
  echo "未找到 docker compose 或 docker-compose"
  exit 1
fi

echo "毛竹 已启动: $IMAGE"
echo "数据目录: $SCRIPT_DIR/data/"
"""
    start_compose_sh = """#!/usr/bin/env sh
set -eu
SCRIPT_DIR="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
exec sh "$SCRIPT_DIR/run.sh"
"""
    for name, content in {
        "run.sh": run_sh,
        "start-compose.sh": start_compose_sh,
    }.items():
        path = app_dir / name
        path.write_text(content, encoding="utf-8", newline="\n")
        path.chmod(0o755)


def generate_windows_scripts(app_dir: Path, version: str, tag: str) -> None:
    tar_name = f"edgeops-v{version}.tar"
    run_bat = f"""@echo off
setlocal EnableDelayedExpansion
cd /d "%~dp0"

set "VERSION={version}"
set "IMAGE={tag}"
set "TAR_PATH=%~dp0{tar_name}"

docker image inspect "%IMAGE%" >nul 2>nul
if errorlevel 1 (
  if not exist "%TAR_PATH%" (
    echo Image not found: %IMAGE%
    echo Tar not found: %TAR_PATH%
    exit /b 1
  )
  echo Loading image: %TAR_PATH%
  docker load -i "%TAR_PATH%"
  if errorlevel 1 (
    echo Docker load failed.
    exit /b 1
  )
)

if not exist "%~dp0data\\data" mkdir "%~dp0data\\data"
if not exist "%~dp0data\\fs" mkdir "%~dp0data\\fs"
if not exist "%~dp0data\\logs" mkdir "%~dp0data\\logs"

docker compose version >nul 2>nul
if not errorlevel 1 (
  docker compose -f "%~dp0docker-compose.yml" up -d
  if errorlevel 1 exit /b 1
  echo 毛竹 started: %IMAGE%
  endlocal
  exit /b 0
)

docker-compose version >nul 2>nul
if not errorlevel 1 (
  docker-compose -f "%~dp0docker-compose.yml" up -d
  if errorlevel 1 exit /b 1
  echo 毛竹 started: %IMAGE%
  endlocal
  exit /b 0
)

echo docker compose / docker-compose not found.
exit /b 1
"""
    start_compose_bat = """@echo off
setlocal
cd /d "%~dp0"
call "%~dp0run.bat"
endlocal
"""
    for name, content in {
        "run.bat": run_bat,
        "start-compose.bat": start_compose_bat,
    }.items():
        (app_dir / name).write_text(content, encoding="utf-8", newline="\r\n")


def prepare_release_dir(app_dir: Path, version: str, tag: str) -> None:
    """发行目录 edgeops-<version>/：compose、启动脚本、data/、镜像 tar 均在同一层。"""
    reset_dir(app_dir)
    ensure_release_persist_dirs(app_dir)
    generate_docker_compose(app_dir, version, tag)
    generate_linux_scripts(app_dir, version, tag)
    generate_windows_scripts(app_dir, version, tag)


def export_docker_image(tag: str, tar_path: Path) -> None:
    docker = locate_docker()
    if not docker:
        raise RuntimeError("未找到 docker 可执行文件，无法导出镜像")
    tar_path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run([docker, "save", "-o", str(tar_path), tag], check=True)


def run_pyarmor(pyarmor_exe: str, release_dir: Path, platform: str) -> None:
    cmd = [
        pyarmor_exe,
        "gen",
        "--platform",
        platform,
        "-r",
        "-O",
        str(release_dir),
        *RUNTIME_INPUTS,
    ]
    subprocess.run(cmd, cwd=ROOT_DIR, check=True)


def validate_python_code(code: str, path: Path) -> None:
    ast.parse(code, filename=str(path), feature_version=TARGET_PYTHON_FEATURE_VERSION)


def minify_python_file(src: Path, dst: Path) -> None:
    code = src.read_text(encoding="utf-8")
    try:
        minified = python_minifier.minify(
            code,
            rename_locals=True,
            rename_globals=False,
            remove_pass=True,
            remove_literal_statements=True,
            remove_annotations=False,
            combine_imports=True,
            hoist_literals=False,
            preserve_shebang=True,
        )
    except Exception:
        minified = code
    try:
        validate_python_code(minified, dst)
    except SyntaxError:
        print(f"Minify 产物语法无效，回退原文件: {src.relative_to(ROOT_DIR)}")
        minified = code
        validate_python_code(minified, dst)
    dst.parent.mkdir(parents=True, exist_ok=True)
    dst.write_text(minified, encoding="utf-8", newline="\n")


def run_minifier(release_dir: Path) -> None:
    for name in RUNTIME_INPUTS:
        src = ROOT_DIR / name
        if src.is_file():
            minify_python_file(src, release_dir / src.name)
            continue
        for path in src.rglob("*.py"):
            if "__pycache__" in path.parts:
                continue
            rel = path.relative_to(ROOT_DIR)
            minify_python_file(path, release_dir / rel)


def _copy_runtime_sources(release_dir: Path) -> None:
    """把 RUNTIME_INPUTS 里的原始 .py 文件/目录完整复制进 release_dir。
    供 pyc 模式在编译前使用（compileall 需要源码存在于 release_dir 里）。"""
    for name in RUNTIME_INPUTS:
        src = ROOT_DIR / name
        dst = release_dir / name
        if src.is_file():
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)
        elif src.is_dir():
            def _ignore(_current: str, names: list[str]) -> set[str]:
                return {n for n in names if n == "__pycache__"}
            shutil.copytree(src, dst, ignore=_ignore, dirs_exist_ok=True)


def compile_to_pyc(release_dir: Path) -> None:
    """先将 RUNTIME_INPUTS 的源码复制进 release_dir，再在 python:3.11-slim 容器内执行
    `python -OO -m compileall -b` 得到与 .py 同目录、无 `.cpython-XX` 版本标签的 .pyc，最后删除所有 .py。
    - 用 `-OO` 同时去除 docstring 与 assert，让字节码更精简；
    - 用 `-b` 让 .pyc 落在源文件同目录（旧式位置）而非 `__pycache__`，方便删除 .py 后被 Python 直接加载；
    - 必须使用 python:3.11-slim（与目标运行镜像同版本），否则 .pyc 的 magic number 无法被目标 Python 识别。
    """
    print("  复制原始 Python 源码到发行目录…")
    _copy_runtime_sources(release_dir)
    docker = locate_docker()
    if not docker:
        raise RuntimeError(
            "pyc 模式需要 Docker 守护进程可用（要用 python:3.11-slim 容器来保证字节码与目标镜像的 Python 版本一致）。\n"
            "请启动 Docker Desktop 后重试；或改用 --mode minify 仅做源码精简。"
        )
    ok, reason = _docker_daemon_available(docker)
    if not ok:
        raise RuntimeError(
            f"pyc 模式需要 Docker 守护进程可用，但检测不可达：{reason}\n"
            "请启动 Docker Desktop 后重试；或改用 --mode minify 仅做源码精简。"
        )
    script = (
        "set -e; "
        "python -OO -m compileall -b -f -q /src; "
        "find /src -type f -name '*.py' -delete; "
        "find /src -type d -name '__pycache__' -exec rm -rf {} + 2>/dev/null || true"
    )
    print("  在 python:3.11-slim 容器内执行 `python -OO -m compileall -b` 并删除 .py 源码…")
    result = subprocess.run(
        [
            docker, "run", "--rm",
            "-v", f"{release_dir}:/src",
            "python:3.11-slim",
            "sh", "-c", script,
        ],
        text=True,
        capture_output=True,
    )
    if result.returncode != 0:
        err = (result.stderr or result.stdout or "").strip()
        raise RuntimeError(f"pyc 编译失败：{err or '未知错误'}")
    pyc_count = sum(1 for _ in release_dir.rglob("*.pyc"))
    py_remaining = sum(1 for _ in release_dir.rglob("*.py"))
    print(f"  完成：生成 {pyc_count} 个 .pyc；剩余 .py 源文件 {py_remaining} 个（应为 0）")
    if py_remaining:
        raise RuntimeError(f"pyc 模式结束后仍残留 {py_remaining} 个 .py 源文件，请检查编译日志")


def verify_release_python(release_dir: Path) -> None:
    failed: list[Path] = []
    for path in release_dir.rglob("*.py"):
        if "__pycache__" in path.parts:
            continue
        try:
            validate_python_code(path.read_text(encoding="utf-8"), path)
        except SyntaxError:
            failed.append(path.relative_to(release_dir))
    if failed:
        joined = ", ".join(str(item) for item in failed[:10])
        raise RuntimeError(f"发行目录 Python 语法校验失败: {joined}")


def _docker_daemon_available(docker: str) -> tuple[bool, str]:
    """探测 docker 守护进程是否可达，避免真正 run 镜像时才报错。
    可达返回 (True, "")；不可达返回 (False, 简要原因)。"""
    try:
        probe = subprocess.run(
            [docker, "version", "--format", "{{.Server.Version}}"],
            text=True,
            capture_output=True,
            timeout=10,
        )
    except Exception as exc:
        return (False, f"docker version 调用失败: {exc}")
    if probe.returncode == 0 and probe.stdout.strip():
        return (True, "")
    reason = (probe.stderr or probe.stdout or "未知错误").strip().splitlines()[0]
    return (False, reason)


def find_target_python_incompatible_files(release_dir: Path) -> list[Path]:
    docker = locate_docker()
    if not docker:
        print("未找到 docker，跳过 Python 3.11 兼容性校验")
        return []
    ok, reason = _docker_daemon_available(docker)
    if not ok:
        print(
            "⚠️ Docker 守护进程不可达，跳过 Python 3.11 兼容性校验（这只影响本地预检；"
            "真正在目标机器上 docker build / docker compose up 时仍会用 python:3.11-slim 运行）。\n"
            f"   原因：{reason}\n"
            "   如需启用此校验，请先启动 Docker Desktop 再重跑 build-and-export.bat。"
        )
        return []
    check_script = """
import ast
from pathlib import Path
import sys

root = Path('/src')
failed = []
for path in root.rglob('*.py'):
    if '__pycache__' in path.parts:
        continue
    try:
        ast.parse(path.read_text(encoding='utf-8'), filename=str(path))
    except Exception:
        failed.append(str(path.relative_to(root)).replace('\\\\', '/'))

if failed:
    print('\\n'.join(failed))
    sys.exit(1)
"""
    result = subprocess.run(
        [
            docker,
            "run",
            "--rm",
            "-v",
            f"{release_dir}:/src",
            "python:3.11-slim",
            "python",
            "-c",
            check_script,
        ],
        text=True,
        capture_output=True,
    )
    if result.returncode == 0:
        return []
    stdout = result.stdout.strip()
    if not stdout:
        stderr = (result.stderr or "").strip()
        lowered = stderr.lower()
        daemon_markers = (
            "cannot connect to the docker daemon",
            "failed to connect to the docker api",
            "is the docker daemon running",
            "pipe/dockerdesktoplinuxengine",
            "error during connect",
            "docker_host",
        )
        if any(marker in lowered for marker in daemon_markers):
            print(
                "⚠️ docker run 执行时守护进程不可达，跳过 Python 3.11 兼容性校验。\n"
                f"   原因：{stderr.splitlines()[0] if stderr else '未知'}"
            )
            return []
        raise RuntimeError(f"Python 3.11 兼容性校验失败: {stderr or '未知错误'}")
    return [Path(line.strip()) for line in stdout.splitlines() if line.strip()]


def ensure_target_python_compatibility(release_dir: Path) -> None:
    restored: set[Path] = set()
    while True:
        failed = find_target_python_incompatible_files(release_dir)
        if not failed:
            return
        current: list[Path] = []
        for rel in failed:
            src = ROOT_DIR / rel
            dst = release_dir / rel
            if not src.exists():
                continue
            shutil.copy2(src, dst)
            current.append(rel)
        if not current:
            joined = ", ".join(str(item) for item in failed[:10])
            raise RuntimeError(f"以下文件与 Python 3.11 不兼容，且无法自动回退: {joined}")
        unchanged = [rel for rel in current if rel in restored]
        if unchanged:
            joined = ", ".join(str(item) for item in unchanged[:10])
            raise RuntimeError(f"以下文件回退原始源码后仍与 Python 3.11 不兼容: {joined}")
        restored.update(current)
        joined = ", ".join(str(item) for item in current[:10])
        print(f"以下文件与 Python 3.11 不兼容，已回退原始源码: {joined}")


def build_docker_image(release_dir: Path, tag: str, version: str) -> None:
    docker = locate_docker()
    if not docker:
        raise RuntimeError("未找到 docker 可执行文件，无法自动构建镜像")
    env = os.environ.copy()
    env.setdefault("DOCKER_BUILDKIT", "1")
    cmd = [docker, "build"]
    # 复用同标签或通用缓存镜像中的层（仅改业务代码时 apt/pip 层可命中缓存）
    for cache_ref in (tag, "edgeops:buildcache"):
        probe = subprocess.run(
            [docker, "image", "inspect", cache_ref],
            capture_output=True,
        )
        if probe.returncode == 0:
            cmd.extend(["--cache-from", cache_ref])
            break
    cmd.extend(
        [
            "-t",
            tag,
            "--build-arg",
            f"EDGEOPS_VERSION={version}",
            ".",
        ]
    )
    subprocess.run(cmd, cwd=release_dir, check=True, env=env)
    subprocess.run(
        [docker, "tag", tag, "edgeops:buildcache"],
        check=False,
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="混淆 毛竹 Python 代码并生成发行 Docker 上下文")
    parser.add_argument("--output-root", default="build", help="输出根目录，默认 build")
    parser.add_argument("--platform", default=DEFAULT_PLATFORM, help="PyArmor 目标平台，默认 linux.x86_64")
    parser.add_argument("--tag", default=None, help="Docker 镜像标签，默认 edgeops:v<当前版本>")
    parser.add_argument("--build-image", action="store_true", help="生成镜像构建上下文后构建 Docker 镜像")
    parser.add_argument(
        "--export-tar",
        action="store_true",
        help="构建镜像后将 edgeops-v<版本>.tar 写入发行包 edgeops-<版本>/ 目录（通常与 --build-image 联用）",
    )
    parser.add_argument(
        "--keep-image-context",
        action="store_true",
        help="保留 build/_edgeops-<版本>-image/ 目录（默认构建成功后删除）",
    )
    parser.add_argument(
        "--mode",
        choices=["auto", "pyarmor", "minify", "pyc"],
        default="auto",
        help=(
            "代码处理方式："
            "auto=优先 pyarmor，失败后根据 docker 是否可用自动选 pyc/minify；"
            "pyarmor=商业混淆；"
            "minify=源码精简（去注释/docstring，保留 .py，无需 docker）；"
            "pyc=发布为字节码（.py 全部编译成 .pyc 并删除源码，需要 docker 可用）"
        ),
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    version = load_version()
    tag = args.tag or f"edgeops:v{version}"
    output_root = (ROOT_DIR / args.output_root).resolve()
    image_ctx = output_root / f"_edgeops-{version}-image"
    app_dir = output_root / f"edgeops-{version}"
    tar_name = f"edgeops-v{version}.tar"
    export_tar = args.export_tar or args.build_image

    # 若用户要求构建镜像，先快速自检 docker 守护进程，避免漫长的混淆后在最后一步才失败。
    if args.build_image or export_tar:
        docker = locate_docker()
        if not docker:
            print(
                "[前置检查] ❌ 未找到 docker 可执行文件，但你传了 --build-image。\n"
                "  请先安装并启动 Docker Desktop，然后重新运行 build-and-export.bat。"
            )
            return 1
        ok, reason = _docker_daemon_available(docker)
        if not ok:
            print(
                "[前置检查] ❌ Docker 守护进程不可达，但你传了 --build-image（或你在用 build-and-export.bat）。\n"
                "  该脚本最后会执行 `docker build` / `docker save`，必须有可用的 Docker 守护进程。\n"
                f"  原因：{reason}\n"
                "  请启动 Docker Desktop，等它显示『Running』后再重试。\n"
                "  如只想生成发行目录不构建镜像，可直接运行：\n"
                "      python scripts/build_release.py --platform linux.x86_64\n"
                "  （不传 --build-image，纯离线打包，无需 Docker）"
            )
            return 1
        print(f"[前置检查] ✅ Docker 守护进程可用：{reason or 'OK'}")

    print(f"[1/5] 准备镜像构建上下文: {image_ctx}")
    output_root.mkdir(parents=True, exist_ok=True)
    reset_dir(image_ctx)

    print("[2/5] 复制运行资源（仅用于 Docker 镜像）")
    copy_runtime_assets(image_ctx)

    used_mode = args.mode
    print(f"[3/5] 处理 Python 代码 -> {args.platform}")
    if args.mode == "pyarmor":
        pyarmor_exe = locate_pyarmor()
        run_pyarmor(pyarmor_exe, image_ctx, args.platform)
    elif args.mode == "minify":
        run_minifier(image_ctx)
    elif args.mode == "pyc":
        compile_to_pyc(image_ctx)
    else:
        try:
            pyarmor_exe = locate_pyarmor()
            run_pyarmor(pyarmor_exe, image_ctx, args.platform)
            used_mode = "pyarmor"
        except Exception as exc:
            print(f"PyArmor 不可用或受限：{exc}")
            reset_dir(image_ctx)
            copy_runtime_assets(image_ctx)
            docker_path = locate_docker()
            daemon_ok = False
            if docker_path:
                daemon_ok, _ = _docker_daemon_available(docker_path)
            if daemon_ok:
                print("  Docker 守护进程可用，自动回退到 pyc 模式（.py → .pyc，删除源码）。")
                try:
                    compile_to_pyc(image_ctx)
                    used_mode = "pyc"
                except Exception as pyc_exc:
                    print(f"  pyc 模式失败，再次回退到 minify 源码精简：{pyc_exc}")
                    reset_dir(image_ctx)
                    copy_runtime_assets(image_ctx)
                    run_minifier(image_ctx)
                    used_mode = "minify"
            else:
                print("  Docker 守护进程不可用，回退到 minify 源码精简模式（可离线完成，但保护弱）。")
                run_minifier(image_ctx)
                used_mode = "minify"

    print("[4/5] 生成 Dockerfile / .dockerignore，并组装发行压缩包目录")
    generate_dockerfile(image_ctx, version)
    generate_dockerignore(image_ctx, used_mode=used_mode)
    if used_mode == "minify":
        ensure_target_python_compatibility(image_ctx)
    if used_mode != "pyc":
        verify_release_python(image_ctx)
    (image_ctx / "BUILD_INFO.txt").write_text(
        f"version={version}\nmode={used_mode}\nplatform={args.platform}\ntag={tag}\n",
        encoding="utf-8",
    )

    prepare_release_dir(app_dir, version, tag)

    print(f"  发行目录（将打入 .tgz 为 edgeops-{version}/）: {app_dir}")
    print(f"代码处理方式: {used_mode}")
    print(f"Docker 标签: {tag}")

    if args.build_image:
        print(f"[5/5] 构建镜像: {tag}")
        build_docker_image(image_ctx, tag, version)
        print(f"镜像构建完成: {tag}")
    else:
        print("[5/5] 跳过 docker build（未指定 --build-image）")

    if export_tar:
        docker = locate_docker()
        tar_path = app_dir / tar_name
        if not docker:
            print(f"警告: 未找到 docker，无法导出 {tar_name}")
        elif subprocess.run(
            [docker, "image", "inspect", tag],
            capture_output=True,
        ).returncode != 0:
            print(f"警告: 镜像 {tag} 不存在，无法导出 {tar_name}")
        else:
            print(f"导出镜像到: {tar_path}")
            export_docker_image(tag, tar_path)

    if args.build_image and not args.keep_image_context and image_ctx.exists():
        print(f"清理镜像构建上下文: {image_ctx}")
        shutil.rmtree(image_ctx, ignore_errors=True)

    # 清理旧版 bundle 包装目录
    legacy_bundle = output_root / f"edgeops-v{version}-bundle"
    if legacy_bundle.exists():
        print(f"清理旧版 bundle 目录: {legacy_bundle}")
        shutil.rmtree(legacy_bundle, ignore_errors=True)
    legacy_tar = output_root / tar_name
    if legacy_tar.is_file():
        legacy_tar.unlink()

    tgz_name = f"edgeops-v{version}.tgz"
    print(f"\n完成。打包示例:")
    print(f"  tar -acf {output_root / tgz_name} -C {output_root} edgeops-{version}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
