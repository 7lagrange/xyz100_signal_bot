#!/bin/bash
set -u

project_dir="$(cd "$(dirname "$0")" && pwd)"
python_bin=""
for candidate in python3 python "$HOME/miniconda3/bin/python" "$HOME/anaconda3/bin/python" /opt/homebrew/bin/python3 /usr/local/bin/python3; do
    candidate_path="$(command -v "$candidate" 2>/dev/null || true)"
    if [[ -n "$candidate_path" ]] && "$candidate_path" -c 'import sys; sys.exit(sys.version_info < (3, 11))' >/dev/null 2>&1; then
        python_bin="$candidate_path"
        break
    fi
done

if [[ -z "$python_bin" ]]; then
    echo "需要 Python 3.11 或更新版本。请先安装 Python，再运行此文件。"
    if [[ -t 0 ]]; then read -r -p "按回车退出。"; fi
    exit 1
fi

if ! "$python_bin" -c 'import requests' >/dev/null 2>&1; then
    printf '缺少依赖，请在终端运行：\n"%s" -m pip install -r "%s/requirements.txt"\n' "$python_bin" "$project_dir"
    if [[ -t 0 ]]; then read -r -p "按回车退出。"; fi
    exit 1
fi

entry="$project_dir/bot.py"
if [[ "${1:-}" == "--install-agent" ]]; then
    entry="$project_dir/install_launch_agent.py"
    shift
fi
"$python_bin" -u "$entry" "$@"
status=$?
if [[ $status -ne 0 && -t 0 ]]; then read -r -p "运行结束，请检查上方提示。按回车退出。"; fi
exit "$status"
