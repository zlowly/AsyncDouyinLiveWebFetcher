# tools/_dev/ —— 重构验收脚手架（不属于工具本身）

`tools/` 的统一入口重构（P0–P5，已完成）全程靠它验收：仓库没有测试框架、
没有 CI，唯一手段是「输出逐字节比对」。

## ⚠️ 先读这段，否则你会误刷新基线

**基线绑定的是 capture 那一刻的 `data/events.db` 快照，而那个库会持续增长**
（抓取器写入，且 `data/` 被 gitignore）。39 条 fixture 里有 12 条是
「随库漂移」的：`chart_default` / `msg_default` / `chart_ascii` /
`*_list_sessions` 取 **latest session**，6 条 `search_*` 是**全库聚合**
（命中总数会随新弹幕变化）。

后果：**新场次落地后，即使代码零改动，diff 也会变红。**

所以：

> diff 变红时，**先人工核对差异是不是纯数据变化**，再决定是否重 capture。
> 盲目 re-capture 正是掩盖真回归的动作。

判断方法：`diff <旧快照> <新快照>`，确认变化的行都是场次名、时间戳、条数、
命中行这类**数据内容**，而不是表头、字段序、错误文案、退出码这类
**结构内容**。只看到后者就是回归，改代码。

## golden.py

```bash
python tools/_dev/golden.py diff      # 重跑全部 fixture 与基线比对（默认）
python tools/_dev/golden.py capture   # 重新捕获基线（仅在有意改变输出时用）
```

比对口径：每个 fixture 的 **stdout + stderr + exit code** 必须逐字节相同。
可比对的前提是去抖，脚本已固定 `TERM=dumb`、`NO_COLOR=1`、
`PYTHONHASHSEED=0`、`COLUMNS=100`，并剥离裸 ANSI 序列行（终端能力探测输出）。

路径全部自定位（`Path(__file__).parent` 逐级上溯），任意 CWD 下可运行。

**覆盖范围与盲区**：fixture 直接跑 `tools/commands/*.py`，**不经过 `tool`
入口**——入口的 dispatch / prog 转发 / 退出码转发目前靠人工 spot-check，
未纳入自动比对。`chart` / `messages` 的 `--help` 也无 fixture（两者 parser
没有 required 参数，不触发 usage 输出）；`tool fetch --help` 在缺 scrapling
的机器上走 ImportError 分支返回 3，永远打不开 fetch 的 help。

## golden_base/

重构**之前**捕获的快照（39 条 fixture + 1 条导入冒烟 = 40 个文件）。
**只有在有意改变某个命令的输出时才重新 capture**，否则 diff 红了就是
行为回归，必须修代码而不是刷新基线。

基线里保留了几条「明知故犯」的真实行为，它们是重构时的红线：

| fixture | 现象 | 含义 |
|---|---|---|
| `chart_bad_chart_h` | exit 0 | `--chart-height` 的 6 倍数校验**只在 `--chart-format sixel` 分支内**，ascii 模式下不生效；`chart_sixel_bad_h` 才是走该分支的用例 |
| `chart_list_sessions` | exit 0 | chart 唯一**可达**的「成功但无输出」早退点，必须保持 exit 0。另两个 exit 0 早退点（场次无原始数据 / 无有效估算数据）在当前数据下不可达，golden 锁不住，属防御性分支 |
| `chart_debug` | — | 锁 `-d`。P2 曾因模块级 `DEBUG_MODE` 在 `main()` 里被局部遮蔽导致 helper 层 16 行调试输出消失，而当时 35 条 fixture 无一触发 |
| `chart_color_always` | — | 锁 `--color always`。同上，`COLOR_ENABLED` 被遮蔽会让 A/B 比对完全不着色。着色**只在比对模式**下可能开启 |
| `search_session` | exit 1 | 零命中 |
| `search_nohit` | exit 1 | 零命中返回 **1** 而非 0 |
| `search_list_sessions` | exit 2 | **已知 bug**：`-k` 是 `required=True`，argparse 先校验必填就退出，`--list-sessions` 在 search 里永远走不到 |
| `msg_default` vs `search_k3` | tsv 列数不同 | `messages` 是 3 列（`timestamp/speaker/content`，仅在有消息时写表头）；`search` 是 4 列（含 `room_id`，始终写表头） |

## argparse 的 prog（P3 已处理）

三个命令的 `build_parser(prog=None)` / `main(argv, prog)` 把 `prog` 透传给
`ArgumentParser`，**传 None 时由 argparse 自己回退 `basename(sys.argv[0])`**。
效果：

```
python tools/commands/search.py --help  →  usage: search.py
python tools/tool search --help         →  usage: tool search
```

P3 实施前的旧脚本直跑会是 `usage: search_chat.py`；P4 脚本改名为
`search.py` 后，`search_help` / `search_list_sessions` / `search_nokw`
三条快照的 usage 行、续行缩进（= prog 名长度差）、`search.py: error:`
前缀随之变化，那次重新 capture 是**预期变更**。

## `_import_smoke`

单独的一组断言，从 `tools/_lib/` 直接导入 `dbcli` / `session_db` /
`sixel_plot` 并检查关键导出。单独存在的理由：`chart` 对 `sixel_plot`
用的是 `try: import sixel_plot / except ImportError: sixel_plot = None`，
导入失败会被**静默吞掉**，而所有 chart fixture 都走 ascii 分支，照样全绿。
这个冒烟把「静默降级」变成「显式失败」。

它同时打印 `DEFAULT_DB_PATH` 的解析结果。该常量在 `session_db.py` 内按
**自身** `__file__` 逐级上溯推算仓库根——所以搬动**命令模块**不影响它，
只有搬 `_lib/` 自身才会。P4 搬完命令模块后该快照未变，即为佐证。
