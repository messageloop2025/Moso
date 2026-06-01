#!/usr/bin/env bash
# 从配置文件加载 EDGEOPS_ACCESS_TOKEN / EDGEOPS_BASE_URL 到当前 shell。
# 用法: source claw-skills/scripts/load-edgeops-env.sh
# 或:   eval "$(claw-skills/scripts/load-edgeops-env.sh --export)"

set -euo pipefail

_export_mode=false
if [[ "${1:-}" == "--export" ]]; then
  _export_mode=true
fi

_load_dotenv() {
  local f="$1"
  [[ -f "$f" ]] || return 1
  while IFS= read -r line || [[ -n "$line" ]]; do
    line="${line%%#*}"
    line="$(echo "$line" | sed 's/^[[:space:]]*//;s/[[:space:]]*$//')"
    [[ -z "$line" ]] && continue
    [[ "$line" != *=* ]] && continue
    local key="${line%%=*}"
    local val="${line#*=}"
    val="${val%\"}"; val="${val#\"}"
    val="${val%\'}"; val="${val#\'}"
    case "$key" in
      EDGEOPS_ACCESS_TOKEN|EDGEOPS_BASE_URL|EDGEOPS_API_BASE_URL)
        if $_export_mode; then
          printf 'export %s=%q\n' "$key" "$val"
        else
          export "$key=$val"
        fi
        ;;
    esac
  done < "$f"
  return 0
}

_load_json() {
  local f="$1"
  [[ -f "$f" ]] || return 1
  if ! command -v python3 >/dev/null 2>&1 && ! command -v python >/dev/null 2>&1; then
    echo "load-edgeops-env: 需要 python 解析 JSON: $f" >&2
    return 1
  fi
  local py=python3
  command -v python3 >/dev/null 2>&1 || py=python
  "$py" - "$f" "$_export_mode" <<'PY'
import json, shlex, sys
path, export_mode = sys.argv[1], sys.argv[2] == "True"
with open(path, encoding="utf-8") as f:
    d = json.load(f)
token = (d.get("accessToken") or d.get("access_token") or "").strip()
base = (d.get("baseUrl") or d.get("base_url") or "").strip().rstrip("/")
pairs = []
if token:
    pairs.append(("EDGEOPS_ACCESS_TOKEN", token))
if base:
    pairs.append(("EDGEOPS_BASE_URL", base))
if not pairs:
    sys.exit(1)
for k, v in pairs:
    if export_mode:
        print(f"export {k}={shlex.quote(v)}")
    else:
        print(f"{k}={v}")
PY
  return 0
}

_apply_pairs() {
  if $_export_mode; then
    cat
  else
    while IFS= read -r line; do
      [[ -z "$line" ]] && continue
      export "$line"
    done
  fi
}

_try_load() {
  local f="$1"
  if [[ "$f" == *.json ]]; then
    _load_json "$f" | _apply_pairs && return 0
  else
    _load_dotenv "$f" && return 0
  fi
  return 1
}

_candidates=()
if [[ -n "${EDGEOPS_CONFIG:-}" ]]; then
  candidates+=("$EDGEOPS_CONFIG")
fi
_candidates+=(
  "${HOME}/.config/edgeops/config.json"
  "${HOME}/.config/edgeops/edgeops.config.json"
  "${HOME}/.hermes/edgeops.json"
  "${HOME}/.hermes/edgeops.config.json"
)
if [[ -n "${HERMES_HOME:-}" ]]; then
  _candidates+=("${HERMES_HOME}/edgeops.json" "${HERMES_HOME}/edgeops.config.json")
fi
_script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
_candidates+=(
  "${_script_dir}/../edgeops.config.json"
  "${_script_dir}/../edgeops.env"
  "${PWD}/edgeops.config.json"
  "${PWD}/edgeops.env"
  "${HOME}/.config/edgeops/.env"
)

_loaded=false
for f in "${_candidates[@]}"; do
  if _try_load "$f"; then
    $_export_mode || true
    _loaded=true
    break
  fi
done

if ! $_export_mode; then
  if [[ -z "${EDGEOPS_ACCESS_TOKEN:-}" ]]; then
    echo "load-edgeops-env: 未找到有效配置。请复制 edgeops.config.example.json → edgeops.config.json 并填写 accessToken，或设置 EDGEOPS_ACCESS_TOKEN。" >&2
    return 1 2>/dev/null || exit 1
  fi
fi
