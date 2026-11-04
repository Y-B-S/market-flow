#!/bin/bash
# 一键推送 + 开启 GitHub Pages
# 用法: ./setup_github.sh [token文件路径]
#
# 前置条件：已在 GitHub 创建 Public 仓库 Y-B-S/market-flow（不要初始化任何文件）
set -eu

USER_NAME="Y-B-S"
REPO="market-flow"
BRANCH="main"
TOKFILE="${1:-/tmp/.gh_tok}"

echo "· 目标: github.com/$USER_NAME/$REPO"
echo "· 分支: $BRANCH  |  Pages: $BRANCH /docs"
echo

if [ ! -f "$TOKFILE" ]; then
    echo "✗ 找不到 token 文件: $TOKFILE"
    exit 1
fi
TOK=$(cat "$TOKFILE")

# 检查仓库是否已创建
CODE=$(curl -s -m 20 -o /dev/null -w "%{http_code}" -H "Authorization: Bearer $TOK" \
       "https://api.github.com/repos/$USER_NAME/$REPO")
if [ "$CODE" != "200" ]; then
    echo "✗ 仓库尚不存在（HTTP $CODE）"
    echo "  请先到 https://github.com/new 创建 Public 仓库 $REPO（不要初始化文件）"
    exit 1
fi
echo "✓ 仓库存在"

# 用 token 推送（token 只放内存，不写入 .git/config）
git remote remove origin 2>/dev/null || true
git remote add origin "https://github.com/$USER_NAME/$REPO.git"

CUR=$(git rev-parse --abbrev-ref HEAD)
[ "$CUR" = "$BRANCH" ] || { git branch -M "$BRANCH"; echo "✓ 分支重命名为 $BRANCH"; }

echo "· 推送中…"
git -c http.extraheader="Authorization: Bearer $TOK" push -u origin "$BRANCH"
echo "✓ 代码已推送"

# 尝试通过 API 开启 Pages
echo "· 尝试开启 GitHub Pages…"
RESP=$(curl -s -m 25 -X POST \
  -H "Authorization: Bearer $TOK" \
  -H "Accept: application/vnd.github+json" \
  -H "X-GitHub-Api-Version: 2022-11-28" \
  "https://api.github.com/repos/$USER_NAME/$REPO/pages" \
  -d "{\"source\":{\"branch\":\"$BRANCH\",\"path\":\"/docs\"}}")

echo "$RESP" | python3 -c "
import json,sys
d=json.load(sys.stdin)
if d.get('html_url'):
    print('✓ Pages 已开启:', d['html_url'])
elif 'already exists' in str(d.get('message','')).lower():
    print('· Pages 已存在')
else:
    print('· API 未能开启 Pages（token 权限不足）:', d.get('message'))
    print('  请手动操作: Settings → Pages → Source: Deploy from a branch')
    print('              Branch: main  目录: /docs')
"
echo
echo "网站地址（Pages 首次部署需 1-2 分钟）:"
echo "  https://$USER_NAME.github.io/$REPO/"
