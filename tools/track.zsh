#!/bin/zsh

# IP 属地抓取管线（数据源：events.db 的 ip_cache + ev_* 分表）。
# 取代已退役的 track_with_duckdb.zsh —— 旧版基于 DuckDB 的
# logs/user_attr/chatters/报告 现已全部废弃，DuckDB 不再是依赖；
# 抓取器 fetch_ip_location.py 不变。
#
# 用法: ./track.zsh [子命令] [选项]
#
# 子命令 (默认 enrich):
#   enrich   从 events.db 取待抓 sec_uid -> fetch_ip_location.py 抓取
#            -> ip_cache.py import-csv 回灌（断点续跑）
#   user ID  单用户速查：性别/抖音号/IP属地及抓取状态；
#            没抓过就当场抓一次再展示
#   status   抓取进度与候选池（ip_cache.py status）
#
# 选项 (enrich 用):
#   --limit N        每轮最多抓多少个 (默认 0=不限)
#   --rounds N       抓取轮数 (默认 1)
#   --interval S     每次请求间隔秒，下限 1 (默认 1)
#   --pause S        轮间停顿秒 (默认 30)
#   --fans           只抓曾有粉丝团的用户 (默认)
#   --all            抓全部用户
#   --chatters       只抓发过言的用户
#   --retry-errors   抓取失败的行也重试
#   --refresh-days N N 天前抓过的行过期重抓
#   --active-days N  只抓最后出现在 N 天内的用户 (默认 0=不限)
#
# 待抓清单如需单独生成: python src/ip_cache.py pending [选项] > list.txt
# （需在项目根目录运行）

SELF="$0"
usage() { sed -n '3,30p' "$SELF"; }

# ---------- 配置 ----------
PROJ=${0:A:h:h}
DB=$PROJ/data/events.db
IPCLI=$PROJ/src/ip_cache.py
PY=/home/zsy/miniconda3/envs/AsyncDouyinLiveWebFetcher/bin/python
FETCHER_DIR=${0:A:h}
FETCHER="$FETCHER_DIR/fetch_ip_location.py"
FETCH_PY=/home/zsy/miniconda3/envs/scrapling/bin/python3

# ---------- 参数 ----------
SUB=enrich
USER_ID=""
LIMIT=0
ROUNDS=1
INTERVAL=1
PAUSE=30
SCOPE=(--fans)
EXTRA=()

while (( $# )); do
    case $1 in
        enrich|status) SUB=$1; shift ;;
        user)          SUB=user; shift; USER_ID=${1:?user 需要一个 id}; shift ;;
        --limit)        LIMIT=${2:?--limit 需要数值}; shift 2 ;;
        --rounds)       ROUNDS=${2:?--rounds 需要数值}; shift 2 ;;
        --interval)     INTERVAL=${2:?--interval 需要数值}; shift 2 ;;
        --pause)        PAUSE=${2:?--pause 需要数值}; shift 2 ;;
        --fans)         SCOPE=(--fans); shift ;;
        --all)          SCOPE=(--all); shift ;;
        --chatters)     SCOPE=(--chatters); shift ;;
        --retry-errors) EXTRA+=(--retry-errors); shift ;;
        --refresh-days) EXTRA+=(--refresh-days ${2:?--refresh-days 需要数值}); shift 2 ;;
        --active-days)  EXTRA+=(--active-days ${2:?--active-days 需要数值}); shift 2 ;;
        -h|--help)      usage; exit 0 ;;
        *)              echo "未知参数: $1（用 --help 看用法）"; exit 2 ;;
    esac
done

(( INTERVAL < 1 )) && INTERVAL=1
(( LIMIT < 0 ))    && LIMIT=0
(( ROUNDS < 1 ))   && ROUNDS=1

[[ -f "$DB" ]] || { echo "数据库不存在: $DB（先跑迁移）"; exit 1 }
[[ -f "$IPCLI" ]] || { echo "找不到 $IPCLI"; exit 1 }

WORK=$(mktemp -d /tmp/track.XXXXXX)
trap 'rm -rf "$WORK"' EXIT

# python 退出码 -> 人话；0=正常
explain_rc() {
    local rc=$1
    if (( rc == 10 )); then
        echo ""
        echo "⚠ 抖音签名 cookie 已失效（拿到 challenge 页）。"
        echo "  请从浏览器 devtools 复制新的 __ac_nonce / __ac_signature，"
        echo "  更新到 fetch_ip_location.py 顶部的 COOKIES，然后重跑同一条命令续跑。"
    elif (( rc == 11 )); then
        echo ""
        echo "⚠ 已自动降速（5s → 20s）仍连续失败，疑似被限流，现在停止等你决策。"
        echo "  已完成的行都已落库；过一段时间重跑即可续跑，"
        echo "  想把本轮失败的行也一起重试就加 --retry-errors。"
    fi
    return $rc
}

# ---------- enrich ----------
enrich_round() {
    local batch="$WORK/batch.txt" out="$WORK/out.csv"
    local rc
    "$PY" "$IPCLI" --db "$DB" pending "${SCOPE[@]}" "${EXTRA[@]}" > "$batch"
    if (( LIMIT > 0 )); then
        head -n "$LIMIT" "$batch" > "$batch.limited"
        mv "$batch.limited" "$batch"
    fi
    local total
    total=$(wc -l < "$batch" | tr -d ' ')
    if (( total == 0 )); then
        echo "没有待抓取的用户（${SCOPE} ${EXTRA}）"
        NO_PENDING=1
        return 0
    fi
    NO_PENDING=0
    echo "本轮待抓取 $total 个 | 间隔 ${INTERVAL}s | ${SCOPE}"
    rm -f "$out"
    "$FETCH_PY" "$FETCHER" --list "$batch" --csv "$out" --interval "$INTERVAL"
    rc=$?
    "$PY" "$IPCLI" --db "$DB" import-csv "$out" || return 1
    return $rc
}

enrich() {
    local r rc
    for (( r = 1; r <= ROUNDS; r++ )); do
        echo "--- 第 $r/$ROUNDS 轮 ---"
        enrich_round
        rc=$?
        (( rc == 10 || rc == 11 )) && { explain_rc $rc; return $rc }
        (( rc != 0 )) && { echo "抓取返回异常码 $rc"; return $rc }
        (( NO_PENDING )) && { echo "已无待抓取用户，提前结束"; return 0 }
        if (( r < ROUNDS )); then
            echo "轮间暂停 ${PAUSE}s ..."
            sleep "$PAUSE"
        fi
    done
    return 0
}

# ---------- user ----------
user() {
    local id=$USER_ID out sec
    [[ -n "$id" ]] || { echo "user 需要一个用户 id"; exit 2 }
    out=$("$PY" "$IPCLI" --db "$DB" user "$id") || { print -r -- "$out"; return 1 }
    print -r -- "$out"
    [[ "$out" == *"尚未抓取"* ]] || return 0

    # 没抓过：当场抓一次再展示
    sec=$(print -r -- "$out" | sed -n 's/.*sec_uid \([^ ]*\).*/\1/p' | head -1)
    if [[ -z "$sec" || "$sec" == - ]]; then
        echo "该用户无 sec_uid，无法抓取"
        return 0
    fi
    echo "尚未抓取，正在抓取 $id ..."
    local batch="$WORK/user_batch.txt" outcsv="$WORK/user_out.csv" rc
    print -r -- "$sec" > "$batch"
    rm -f "$outcsv"
    "$FETCH_PY" "$FETCHER" --list "$batch" --csv "$outcsv" \
        --interval "$INTERVAL" >/dev/null 2>&1
    rc=$?
    "$PY" "$IPCLI" --db "$DB" import-csv "$outcsv"
    (( rc != 0 )) && explain_rc $rc
    echo
    "$PY" "$IPCLI" --db "$DB" user "$id"
    return $rc
}

# ---------- 入口 ----------
case $SUB in
    enrich) enrich ;;
    user)   user ;;
    status) "$PY" "$IPCLI" --db "$DB" status ;;
esac
