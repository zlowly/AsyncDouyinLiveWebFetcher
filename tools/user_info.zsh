#!/bin/zsh

# 单用户信息速查（数据源：events.db 的 ev_* 分表 + ip_cache）
# 取代旧版的 rg+jq 扫日志：候选、属性、发言全部走 SQLite。
#
# 用法: ./user_info.zsh '<用户名模式>' [选项]
#
#   <用户名模式>  匹配 昵称 / 抖音号 / 用户ID，支持通配符 * ?；
#                 不含通配符时按子串匹配（自动补成 *模式*）。
#                 模式含 * ? 或空格时请用引号包住。
#
# 选项:
#   -n, --count N  显示的发言条数（默认 10）
#   -y, --yes      命中多个时自动选最近出现的那个，不询问
#   -h, --help     显示本帮助
#
# 流程:
#   1. 跨 user-bearing 表按模式搜索候选（按最近出现排序，最多 50 个）
#   2. 多个命中则列出候选表供选择（tty 下可交互，-y 自动选 #1）
#   3. 展示属性（抖音号/性别/等级）与 ip_cache 的 IP 属地/抓取状态
#   4. 倒序输出最近 N 条发言
# 没抓过属地时不会当场抓：用 ./track.zsh user <id> 触发抓取并回灌。

SELF="$0"
usage() { sed -n '2,26p' "$SELF"; }

# ---------- 配置 ----------
PROJ=${0:A:h:h}
DB=$PROJ/data/events.db
CAND_LIMIT=50

PAT=""
COUNT=10
AUTO=0

while (( $# )); do
    case $1 in
        -n|--count) COUNT=${2:?--count 需要数值}
                    [[ "$COUNT" == <-> ]] || { echo "--count 需要正整数: $2"; exit 2; }
                    shift 2 ;;
        -y|--yes)   AUTO=1; shift ;;
        -h|--help)  usage; exit 0 ;;
        --)         shift
                    [[ -z "$PAT" ]] && PAT=${1:-}
                    [[ -n "$PAT" ]] && shift
                    (( $# )) && { echo "多余的参数: $1（用 --help 看用法）"; exit 2; }
                    break ;;
        -*)         echo "未知参数: $1（用 --help 看用法）"; exit 2 ;;
        *)          if [[ -z "$PAT" ]]; then PAT=$1; shift
                    else echo "多余的参数: $1（用 --help 看用法）"; exit 2; fi ;;
    esac
done

if [[ -z "$PAT" ]]; then
    usage
    exit 2
fi
(( COUNT < 1 )) && COUNT=1

command -v sqlite3 >/dev/null || { echo "缺少依赖命令: sqlite3"; exit 1; }
[[ -f "$DB" ]] || { echo "数据库不存在: $DB（先跑迁移）"; exit 1 }

WORK=$(mktemp -d /tmp/user_info.XXXXXX)
trap 'rm -rf "$WORK"' EXIT

# ---------- glob -> LIKE 模式（* → %, ? → _；字面 % _ 加 \ 转义） ----------
glob_to_like() {
    local out="" c
    local -a chars=("${(s::)1}")
    for c in "${chars[@]}"; do
        case $c in
            '*') out+='%' ;;
            '?') out+='_' ;;
            '%') out+='\%' ;;
            '_') out+='\_' ;;
            *)   out+=$c ;;
        esac
    done
    print -r -- "$out"
}

if [[ "$PAT" != *'*'* && "$PAT" != *'?'* ]]; then
    PAT="*$PAT*"
fi
LIKE=$(glob_to_like "$PAT")
Q=${LIKE//\'/\'\'}   # SQL 单引号翻倍

# 6 张 user-bearing 表的公共列并集（专属列各表不同，不能 SELECT *）
USER_UNION='
    SELECT user_id, user_name, display_id, sec_uid, gender,
           pay_level, fans_level, ts_epoch FROM ev_chat
    UNION ALL
    SELECT user_id, user_name, display_id, sec_uid, gender,
           pay_level, fans_level, ts_epoch FROM ev_like
    UNION ALL
    SELECT user_id, user_name, display_id, sec_uid, gender,
           pay_level, fans_level, ts_epoch FROM ev_member
    UNION ALL
    SELECT user_id, user_name, display_id, sec_uid, gender,
           pay_level, fans_level, ts_epoch FROM ev_gift
    UNION ALL
    SELECT user_id, user_name, display_id, sec_uid, gender,
           pay_level, fans_level, ts_epoch FROM ev_social
    UNION ALL
    SELECT user_id, user_name, display_id, sec_uid, gender,
           pay_level, fans_level, ts_epoch FROM ev_notice'

CONDS="user_name LIKE '$Q' ESCAPE '\\' OR display_id LIKE '$Q' ESCAPE '\\'
    OR user_id LIKE '$Q' ESCAPE '\\'"

# ---------- 候选搜索 ----------
echo "[搜索] 模式「$PAT」..."
hit_total=$(sqlite3 "$DB" "
    WITH p AS ($USER_UNION)
    SELECT count(*) FROM (SELECT DISTINCT user_id FROM p WHERE $CONDS);")

if (( hit_total == 0 )); then
    echo
    echo "未找到匹配「$PAT」的用户"
    exit 1
fi

sqlite3 -noheader -separator $'\t' "$DB" "
    WITH p AS ($USER_UNION)
    SELECT user_id,
           max(user_name),
           max(display_id),
           count(*),
           datetime(min(ts_epoch),'unixepoch'),
           datetime(max(ts_epoch),'unixepoch')
    FROM p WHERE $CONDS
    GROUP BY user_id
    ORDER BY max(ts_epoch) DESC
    LIMIT $CAND_LIMIT;" > "$WORK/cand.tsv" || { echo "候选查询失败"; exit 1 }

[[ -s "$WORK/cand.tsv" ]] || { echo "未找到匹配「$PAT」的用户"; exit 1 }

# ---------- 选择 ----------
pick_line=""
picked=0

if (( hit_total == 1 )); then
    pick_line=$(head -n 1 "$WORK/cand.tsv")
    picked=1
else
    echo
    echo "命中 $hit_total 个用户，最近出现的前 $(wc -l < "$WORK/cand.tsv" | tr -d ' ') 个："
    echo
    print -r -- "| 序号 | 用户ID | 昵称 | 抖音号 | 出没次数 | 最近出现 |"
    print -r -- "|---|---|---|---|---|---|"
    n=0
    while IFS=$'\t' read -r id nick disp cnt first last; do
        (( ++n ))
        print -r -- "| $n | $id | ${nick:-} | ${disp:-} | $cnt | $last |"
    done < "$WORK/cand.tsv"

    interactive=1
    (( AUTO )) && interactive=0
    [[ -t 0 ]] || interactive=0

    if (( interactive )); then
        shown=$(wc -l < "$WORK/cand.tsv" | tr -d ' ')
        while true; do
            read "ans?选择 [1-$shown]: " || { interactive=0; break; }
            if [[ "$ans" == <-> ]] && (( ans >= 1 && ans <= shown )); then
                pick_line=$(sed -n "${ans}p" "$WORK/cand.tsv")
                picked=1
                break
            fi
            echo "  输入无效，请输入 1-$shown 之间的数字"
        done
    fi

    if (( ! picked )); then
        pick_line=$(head -n 1 "$WORK/cand.tsv")
        echo "自动选择最近出现的 #1"
    fi
fi

IFS=$'\t' read -r sid snick sdisp scnt sfirst slast <<< "$pick_line"

echo
echo "选定用户"
echo "  用户ID  $sid"
echo "  昵称    ${snick:-<空>}"
echo "  抖音号  ${sdisp:-<空>}"
echo "  出没    ${scnt} 次 | $sfirst ~ $slast"

# ---------- 属性 + IP 属地（ip_cache） ----------
gendersql="CASE max(gender) WHEN 1 THEN '男' WHEN 2 THEN '女' ELSE '未知' END"
attrs=$(sqlite3 "$DB" "
    WITH p AS ($USER_UNION)
    SELECT max(user_name), $gendersql,
           max(pay_level), max(fans_level), max(sec_uid)
    FROM p WHERE user_id = '$sid';")
IFS='|' read -r anick agender apay afans asec <<< "$attrs"

echo
echo "【用户资料】"
echo "  昵称    ${anick:-<空>}    性别  ${agender:-未知}"
echo "  付费等级 ${apay:--}    粉丝等级 ${afans:--}"

if [[ -z "$asec" ]]; then
    echo "  属地    该用户无 secUid，无法抓取"
else
    iprow=$(sqlite3 -noheader -separator '|' "$DB" "
        SELECT ip_location, age, ip_status, ip_fetched_at
        FROM ip_cache WHERE sec_uid = '$asec';")
    if [[ -z "$iprow" ]]; then
        echo "  属地    尚未抓取（用 ./track.zsh user $sid 当场抓一次）"
    else
        IFS='|' read -r iploc ipage ipst ipat <<< "$iprow"
        case $ipst in
            ok)
                echo "  属地    ${iploc}${ipage:+（$ipage 岁）}"
                echo "  抓取于  $ipat（status=ok）"
                ;;
            no_ip_location)
                echo "  属地    抖音主页未展示 IP 属地"
                echo "  抓取于  $ipat"
                ;;
            *)
                echo "  属地    抓取失败：$ipst"
                echo "  抓取于  $ipat（用 ./track.zsh user $sid 重试）"
                ;;
        esac
    fi
fi

# ---------- 最近 N 条发言 ----------
echo
echo "【最近 ${COUNT} 条发言】"

sqlite3 -noheader -separator $'\t' "$DB" "
    SELECT ts, replace(replace(content, char(9), ' '), char(10), ' ')
    FROM ev_chat WHERE user_id = '$sid'
    ORDER BY ts_epoch DESC LIMIT $COUNT;" > "$WORK/chats.tsv"

if [[ ! -s "$WORK/chats.tsv" ]]; then
    echo "无发言"
else
    n=0
    while IFS=$'\t' read -r ts content; do
        (( ++n > COUNT )) && break
        printf '%2d. %s  %s\n' "$n" "$ts" "${content:-}"
    done < "$WORK/chats.tsv"
fi
