# Changelog

## 2.0 · 2026-11-06

- 新增逐级升级选车（2–10 级），以及独立的 10→10 同级换车规则。
- 换局后优先从上一局附近重新定位；扫描遇到失效区域后继续前进，并记录搜索进度。
- Windows 包采用内部 V5 的程序逻辑。10 级首胜菜单仍可能使用原候选，第二次获胜菜单已有显示指定车型的实测记录。
- 雷达和地图采用 V5 所用的稳定版本；1.1.1 的首次地图相机实验不在 2.0 中。

## first-map-v10 — 2026-10-26

- Improved initial map camera discovery with periodic metadata refresh and incremental search.
- Preserved search progress during metadata retries and prioritized pending activity samples.
- Retained scan caching, vehicle-level sorting, and opening statistics.

Historical Windows build validation: 443 project tests and 38 frozen-camera tests passed.
First-match map behavior and cold-start speed improvements still await in-game verification.
Source-edition validation is recorded separately in docs/VALIDATION.json.
