# tools/

从 `~/scripts/` 迁入的分析/工具脚本，全是独立工具，不参与 `src/` 抓取主流程。
现已收口成**单一入口** `tool <子命令>`，共 5 个子命令
（`chart` / `messages` / `search` / `sessions` / `fetch`）。

旧脚本 `extract_and_visualize_log_data_sqlite.py`、`process_log_sqlite.py`、
`search_chat.py` **已不存在**——它们分别重命名为 `commands/chart.py`、
`commands/messages.py`、`commands/search.py`，**不能再**用
`python tools/search_chat.py` 之类的方式调用，一律经 `tool` 入口。

## 统一入口

```bash
python tools/tool --help              # 列出全部子命令
python tools/tool <子命令> --help     # 查看某个子命令的完整参数
```

`tool` 自身路径自定位（`Path(__file__)`），任意 CWD 下都可运行；
每个子命令模块内部也自行把 `tools/_lib` 注入 `sys.path`，`--db` 可覆盖
events.db 路径。

> 例外：`fetch` 的模块顶层就 `import scrapling`，所以**必须先切到装了
> scrapling 的环境**才能看到它的 `--help`——本机没装时它走的是 ImportError
> 分支（退出码 3），拿不到参数列表。

## 子命令

| 子命令 | 用途 | 运行要求 |
|--------|------|----------|
| `chart` | 从 `ev_rank` 提取 totalUserCount/audienceCount 时序，估算并画图（字符柱状图 / Sixel 二维图，支持 A/B 比对） | 项目环境（纯标准库） |
| `messages` | 从 `ev_chat/ev_gift/ev_social/ev_notice` 提取聊天/礼物/关注分享/粉丝团消息，tsv（着色）/json 输出 | 项目环境 |
| `search` | 跨**全部场次全部房间**检索 `ev_chat` 弹幕正文，命中明细 tsv/json（≥3 字走 `chat_fts` trigram 索引，1~2 字自动降级 LIKE） | 项目环境 |
| `sessions` | 列出 events.db 的全部场次清单（含天内序号），可直接抄进 `-S/--session` | 项目环境 |
| `fetch` | 串行抓取抖音用户页的 IP 属地/昵称/抖音号/粉丝数/作品数/年龄，写 CSV | **必须 scrapling 环境**（`import scrapling`） |

> `fetch` 在本机（未装 scrapling）会返回退出码 3 并打印环境提示，这是**预期
> 行为**：它把「环境依赖缺失」和「参数写法错误」区分开，不是文档或命令错误。
> 请切换到已安装 scrapling 的 Python 环境后再运行。

## 目录结构

```
tools/
  tool                     统一入口（调度 5 个子命令，惰性导入）
  commands/                命令模块：chart.py / messages.py / search.py / sessions.py
  _lib/                    共享库：dbcli.py / session_db.py / sixel_plot.py
  _dev/                    重构验收脚手架（golden.py 逐字节 diff），用法见 _dev/README.md
  fetch_ip_location.py     属地抓取（仍在 tools/ 根，模块顶层就 import scrapling）
  track.zsh / user_info.zsh  属地抓取管线 / 单用户速查（shell 脚本）
```

- `commands/` 下的命令模块**不共享参数定义**，各自建 parser；`_lib/` 被它们
  import（`dbcli` 定义 `-S`/`--start`/`--end`/`-r` 的统一语义，`session_db`
  负责场次解析与 `DEFAULT_DB_PATH`，`sixel_plot` 是 chart 的 Sixel 渲染库，
  缺库时 chart 自动退回字符图），二者都不单独运行。
- `fetch_ip_location.py` 与两个 `.zsh` 脚本**未搬进** `commands/`：
  `fetch` 只是把 `fetch_ip_location.py` 薄壳转发（其 `main()` 直接读
  `sys.argv`）。

## 数据来源

- 本组工具读 `data/events.db`（抓取器写入的 `ev_*` 分表 + `ip_cache` 表），
  不直接读 `logs/`（sqlite 版已取代读 stats 日志的旧脚本）。
- DB 路径缺省 = 仓库根 `data/events.db`（由文件位置推导）。

## 示例

```bash
# 提取并可视化最新场次的观众数/用户数（终端支持 Sixel 时出二维图）
python tools/tool chart --chart-format sixel

# 10-11 号第一场 + 13 号最后一场并排比对
python tools/tool chart 2026-10-11:1 --compare 2026-10-13:-1

# 列出全部场次（可直接抄进 -S）
python tools/tool sessions -r 249069870647

# 输出某场次弹幕+礼物等，tsv 带着色
python tools/tool messages -S 2026-10-11:-1 -o tsv

# 只看某个用户的发言（* ? 通配符，不含通配符时按子串模糊匹配）
python tools/tool messages -u '昵称*' -o tsv

# 在全库 15 万条弹幕里搜一个词（不限场次、不限房间）
python tools/tool search -k 哈哈哈

# 2 字词自动降级 LIKE 全表扫；--limit 0 看全部命中
python tools/tool search -k 主播 --limit 0

# 限某一场 / 某个时间段 / 输出 json
python tools/tool search -k 6666 -S 2026-10-06:2
python tools/tool search -k 笑 --start 2026-10-01 --end 2026-10-15
python tools/tool search -k 礼物 -o json

# 属地抓取管线（pending → 抓取 → 回灌）
zsh tools/track.zsh enrich --limit 50 --rounds 2

# 单用户速查（模式支持 * ? 通配符）
zsh tools/user_info.zsh '主播*' -n 10

# 手动抓 IP 属地（scrapling 环境下）
tools/fetch_ip_location.py --list pending.txt --csv out.csv --interval 1
```

## 注意事项

- `fetch_ip_location.py` 的 `list.txt` / `ip_locations.csv` 是运行时生成的
  临时工作文件，不随仓库跟踪；`track.zsh` 管线总是显式传 `--list`/`--csv`，
  缺省值仅供手工运行。
- 本组脚本保留原有风格（单引号、中文注释），不参与 `src/` 的 ruff 检查与格式化。

## search 的两条检索路径

`ev_chat` 上的 `chat_fts` 是 FTS5 **trigram** 倒排索引，两个硬约束：

1. **trigram 检索不到 1~2 个字符**，静默返回 0 条而不报错 —— 所以短词
   降级 `content LIKE ? ESCAPE '\'`（全表扫 15 万行约 80ms，够用）。
2. **`MATCH` 必须带 `content:` 列前缀**。`chat_fts` 有 `content` 和
   `user_name` 两列，不带前缀会连昵称一起搜：搜「哈哈哈」时 1655 =
   正文 845 + 昵称 831，而只看正文就该是 845。

两条路径结果已验证严格相等（`content:"X"` 计数 == 转义后 LIKE 计数），
`trigram` 默认 `case_sensitive=0`，ASCII 大小写行为也一致。

## 重构验收脚手架 `_dev/`

`tools/_dev/` 是「命令合并成统一入口」这次重构的验收安全网：仓库没有测试
框架/CI，验收靠输出逐字节比对。用法与红线说明见
[`tools/_dev/README.md`](_dev/README.md)（**该文件是脚手架自己的文档，与本
README 分工不同**）。
