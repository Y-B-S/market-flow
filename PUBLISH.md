# 发布信息

## 网站地址

| 页面 | 地址 |
|---|---|
| 资金流动大盘 | https://y-b-s.github.io/market-flow/ |
| 信息不对称面板 | https://y-b-s.github.io/market-flow/asym.html |
| 背离解释器 | https://y-b-s.github.io/market-flow/explain.html |
| 历史归档 | https://y-b-s.github.io/market-flow/archive/ |

## 仓库

https://github.com/Y-B-S/market-flow （Public）

## 每次更新网站

```bash
cd market-flow
python3 main.py              # 拉数据 + 生成 out/latest.html
python3 build_site.py        # 重建 docs/（含三页面 + 归档）
git add -A && git commit -m "更新 $(date +%F)"
git push
```

推送后 GitHub Pages 会自动重新部署（约 1-2 分钟）。

## 配置细节

- **Pages 源**：`main` 分支，`/docs` 目录
- **`.nojekyll`**：已放在 `docs/`，避免 Jekyll 忽略下划线文件
- **自包含**：三个页面零外链、零脚本，可离线打开
- **数据不入库**：`data/*.json`、`flows.jsonl` 在 `.gitignore` 中

## 自动更新脚本

`run_daily.sh` 每个工作日 15:45 由 launchd 触发（见 README「每日用法」）。
如需连网站一起更新，在脚本末尾加一行 `python3 build_site.py && git add -A && git commit -m "auto $(date +%F)" && git push`。
