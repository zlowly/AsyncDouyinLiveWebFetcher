# tools/_dev/ —— 重构验收脚手架（不属于工具本身）

`tools/` 下的 `chart` / `messages` / `search` 正在合并成单一入口
`tool <子命令>`。本目录是这次重构的**唯一安全网**：仓库没有测试框架、
没有 CI，所以验收靠「输出逐字节比对」。

## golden.py

```bash
python tools/_dev/golden.py diff      # 重跑全部 fixture 与基线比对（默认）
python tools/_dev/golden.py capture   # 重新捕获基线（仅在有意改变输出时用）
```

比对口径：每个 fixture 的 **stdout + stderr + exit code** 必须逐字节相同。
可比对的前提是去抖，脚本已固定 `TERM=dumb`、`NO_COLOR=1`、
`PYTHONHASHSEED=0`、`COLUMNS=100`，并剥离裸 ANSI 序列行（终端能力探测输出）。

路径全部自定位（`Path(__file__).parent` 逐级上溯），任意 CWD 下可运行。

## golden_base/

重构**之前**捕获的 36 份快照。**只有在有意改变某个命令的输出时才重新
capture**，否则 diff 红了就是行为回归，必须修代码而不是刷新基线。

基线里保留了几条「明知故犯」的真实行为，它们是重构时的红线：

| fixture | 现象 | 含义 |
|---|---|---|
| `chart_bad_chart_h` | exit 0 | `--chart-height` 的 6 倍数校验**只在 `--chart-format sixel` 分支内**，ascii 模式下不生效 |
| `chart_list_sessions` | exit 0 | chart 的 3 个「成功但无输出」早退点之一，必须保持 exit 0 |
| `search_session` | exit 1 | 零命中 |
| `search_nohit` | exit 1 | 零命中返回 **1** 而非 0 |
| `search_list_sessions` | exit 2 | **已知 bug**：`-k` 是 `required=True`，argparse 先校验必填就退出，`--list-sessions` 在 search 里永远走不到 |
| `msg_default` vs `search_k3` | tsv 列数不同 | `messages` 是 3 列（`timestamp/speaker/content`，仅在有消息时写表头）；`search` 是 4 列（含 `room_id`，始终写表头） |

## `_import_smoke`

单独的一组断言，从 `tools/_lib/` 直接导入 `dbcli` / `session_db` /
`sixel_plot` 并检查关键导出。单独存在的理由：`chart` 对 `sixel_plot`
用的是 `try: import sixel_plot / except ImportError: sixel_plot = None`，
导入失败会被**静默吞掉**，而所有 fixture 都走 ascii 分支，照样全绿。
这个冒烟把「静默降级」变成「显式失败」。

它同时会打印 `DEFAULT_DB_PATH` 的解析结果——该常量按 `__file__` 逐级上溯
推算仓库根，**模块搬迁会改变层级数**，P4 搬进 `commands/` 时要复查它。
