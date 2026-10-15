# tools/

从 `~/scripts/` 迁入的分析/工具脚本（原样拷贝 + 路径便携化），全是独立工具，
不参与 `src/` 抓取主流程。路径均为自定位（`Path(__file__)` / `${0:A:h}`），
任意 CWD 下都可运行；`--db` 可覆盖 events.db 路径。

## 数据来源

- 本组工具读 `data/events.db`（抓取器写入的 `ev_*` 分表 + `ip_cache` 表），
  不直接读 `logs/`（sqlite 版已取代读 stats 日志的旧脚本）。
- DB 路径缺省 = 仓库根 `data/events.db`（由文件位置推导）。

## 脚本

| 脚本 | 用途 | 运行要求 |
|------|------|----------|
| `extract_and_visualize_log_data_sqlite.py` | 从 `ev_rank` 提取 totalUserCount/audienceCount 时序，估算并画图（字符柱状图 / Sixel 二维图，支持 A/B 比对） | 项目环境（纯标准库） |
| `process_log_sqlite.py` | 从 `ev_chat/ev_gift/ev_social/ev_notice` 提取聊天/礼物/关注分享/粉丝团消息，tsv（着色）/json 输出 | 项目环境 |
| `search_chat.py` | 跨**全部场次全部房间**检索 `ev_chat` 弹幕正文，命中明细 tsv/json（≥3 字走 `chat_fts` trigram 索引，1~2 字自动降级 LIKE） | 项目环境 |
| `dbcli.py` | 命令行参数与时间过滤公共模块（`-S`/`--start`/`--end`/`-r` 的语义统一由它定义），被上两个脚本 import | 项目环境，不单独运行 |
| `session_db.py` | 场次选择公共模块（`--session` 解析、场次清单、`DEFAULT_DB_PATH`），被前几个脚本 import | 项目环境，不单独运行 |
| `sixel_plot.py` | 纯标准库 Sixel 柱状图渲染库（`render_chart` 等纯函数 + `verify_chart` 自检），`extract_...` 惰性导入、缺库自动退回字符图 | 不单独运行 |
| `fetch_ip_location.py` | 串行抓取抖音用户页的 IP 属地/昵称/抖音号/粉丝数/作品数/年龄，写 CSV | **必须 scrapling 环境**（`import scrapling`） |
| `track.zsh` | IP 属地抓取管线：`enrich`（pending → 抓取 → `ip_cache.py import-csv` 回灌，断点续跑）/ `user`（速查+当场抓一次）/ `status` | 项目环境 python + scrapling 环境 python3（文件头部 `PY`/`FETCH_PY` 按机器配置） |
| `user_info.zsh` | 单用户速查：跨表搜候选 → 属性 + IP 属地 → 最近 N 条发言 | sqlite3 CLI |

## 示例

```bash
# 提取并可视化最新场次的观众数/用户数（终端支持 Sixel 时出二维图）
python tools/extract_and_visualize_log_data_sqlite.py --chart-format sixel

# 10-11 号第一场 + 12 号最后一场并排比对
python tools/extract_and_visualize_log_data_sqlite.py 2026-10-11:1 --compare 2026-10-12:-1

# 输出某场次弹幕+礼物等，tsv 带着色
python tools/process_log_sqlite.py -S 2026-10-11:-1 -o tsv

# 只看某个用户的发言（* ? 通配符，不含通配符时按子串模糊匹配）
python tools/process_log_sqlite.py -u '昵称*' -o tsv

# 在全库 15 万条弹幕里搜一个词（不限场次、不限房间）
python tools/search_chat.py -k 哈哈哈

# 2 字词自动降级 LIKE 全表扫；--limit 0 看全部命中
python tools/search_chat.py -k 主播 --limit 0

# 限某一场 / 某个时间段 / 输出 json
python tools/search_chat.py -k 6666 -S 2026-10-15:1
python tools/search_chat.py -k 笑 --start 2026-10-01 --end 2026-10-15
python tools/search_chat.py -k 礼物 -o json

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

## search_chat.py 的两条检索路径

`ev_chat` 上的 `chat_fts` 是 FTS5 **trigram** 倒排索引，两个硬约束：

1. **trigram 检索不到 1~2 个字符**，静默返回 0 条而不报错 —— 所以短词
   降级 `content LIKE ? ESCAPE '\'`（全表扫 15 万行约 80ms，够用）。
2. **`MATCH` 必须带 `content:` 列前缀**。`chat_fts` 有 `content` 和
   `user_name` 两列，不带前缀会连昵称一起搜：搜「哈哈哈」时 1655 =
   正文 845 + 昵称 831，而只看正文就该是 845。

两条路径结果已验证严格相等（`content:"X"` 计数 == 转义后 LIKE 计数），
`trigram` 默认 `case_sensitive=0`，ASCII 大小写行为也一致。

## 注意

- `dbcli.py` 是公共模块，不单独运行。