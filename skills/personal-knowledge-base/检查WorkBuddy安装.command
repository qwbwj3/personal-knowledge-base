#!/bin/zsh
cd "${0:A:h}" || exit 1
command -v python3 >/dev/null || { echo "请宿主Agent通过正常授权准备 Python 3。"; exit 2; }
python3 -B scripts/install_workbuddy.py --check "$@"
result=$?
echo
read "?按回车键关闭窗口。"
exit "$result"
