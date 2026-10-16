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
| `session_db.py` | 场次选择公共模块（`--session` 解析、场次清单、`DEFAULT_DB_PATH`），被前两个脚本 import | 项目环境，不单独运行 |
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