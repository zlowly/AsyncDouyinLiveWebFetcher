#!/usr/bin/env python3
"""从 events.db 的 ev_rank 分表提取 totalUserCount / audienceCount 时序。

与旧版读 stats 日志的实现估算与绘图逻辑完全一致，
数据源换成 SQLite：audienceCount ← ev_rank.total（=message.total），
totalUserCount ← ev_rank.total_user（=totalPvForAnchor，实测与 payload
中的 totalUser 恒等）。

场次指定（位置参数）：
  2026-10-11:1     10-11 号第一场
  2026-10-11:-1    10-11 号最后一场
  2026-10-11       裸日期：当天仅 1 场直选，多场报错列候选
  2026-10-11_11-46-56   精确 session id
  不给时取最新场次。--compare 接第二个场次进入 A/B 比对。
"""
import json
import os
import re
import select
import sys
import time
import argparse
from datetime import datetime, timedelta
from pathlib import Path
import collections

sys.path.insert(0, str(Path(__file__).resolve().parent / "_lib"))
import session_db  # noqa: E402

try:
    import termios
    import tty
except ImportError:      # 非 POSIX 平台没有 termios/tty，光标查询自动禁用
    termios = tty = None

try:
    import sixel_plot
except ImportError:
    # Sixel 绘图层缺失时自动退回字符柱状图，主流程不受影响
    sixel_plot = None

# 全局变量，用于控制调试模式是否开启
DEBUG_MODE = False

# 全局变量，用于控制终端着色是否开启（由 --color 决定）
COLOR_ENABLED = False

# 全局变量，用于控制柱状图渲染后端：'sixel' 出二维图形，'ascii' 出字符条
CHART_FORMAT = 'ascii'

# 比对模式下的合成基准时刻。两份日志都会被平移，使各自首个真实数据点落在
# 该时刻上。取分钟位为 0 的整点，可确保 interpolate_data 的 5 分钟栅格
# 无需额外取整对齐，两侧算出的时间戳逐点完全相同，从而可以按行并排比对。
COMPARISON_EPOCH = datetime(2000, 1, 1)

# 并排比对时用于区分 A/B 两个日志的柱子字符（密度差异明显，黑白打印也可辨）
# 仅在 --bar-style ascii 时使用；八分块模式下两侧柱形一致，靠 A/B 标签与颜色区分。
BAR_CHAR_A = '#'
BAR_CHAR_B = '@'

# 八分块方块字符，由细到粗：第 i 个字符表示 i/8 格，末尾的 █ 即满格
BLOCK_EIGHTHS = "▏▎▍▌▋▊▉█"

# 并排比对时 A/B 两个日志的着色（青 / 品红）
COLOR_A = "\x1b[36m"
COLOR_B = "\x1b[35m"
COLOR_RESET = "\x1b[0m"

# 指标名称映射
METRIC_DISPLAY_NAMES = {
    'totalUserCount': '用户总数',
    'audienceCount': '观众计数'
}
METRIC_SHORT_NAMES = {
    'totalUserCount': '总数',
    'audienceCount': '观众'
}
METRIC_CHART_TITLES = {
    'totalUserCount': 'Total User Count 趋势图',
    'audienceCount': 'Audience Count 趋势图'
}

STATS_LOG_RE = re.compile(
    r"^stats-(\d{4}-\d{2}-\d{2})_(\d{2}-\d{2}-\d{2})\.log$"
)


def extract_session_data(conn, session_id):
    """
    从 ev_rank 提取指定场次的时间戳、totalUserCount 和 audienceCount。
    只取 WebcastRoomUserSeqMessage（对应旧 stats 日志的 RoomUserSeq 行）。
    两个计数都为 NULL 的行不纳入结果（与旧版"都不存在则跳过"一致）。

    Returns:
        list: [{'timestamp': datetime, 'totalUserCount'?: int,
                'audienceCount'?: int}]，按时间升序。
    """
    results = []
    rows = conn.execute(
        """
        SELECT ts, total, total_user
        FROM ev_rank
        WHERE session = ? AND method = 'WebcastRoomUserSeqMessage'
        ORDER BY ts
        """,
        (session_id,),
    ).fetchall()
    for ts_str, total, total_user in rows:
        entry = {"timestamp": session_db.parse_ts(ts_str)}
        if total is not None:
            entry["audienceCount"] = int(total)
        if total_user is not None:
            entry["totalUserCount"] = int(total_user)
        if "totalUserCount" in entry or "audienceCount" in entry:
            results.append(entry)
    if DEBUG_MODE:
        print(f"DEBUG: 场次 {session_id} 提取到 {len(results)} 条原始数据。",
              file=sys.stderr)
    return results



def calculate_ewma_trend(values_with_timestamps, span_minutes):
    """
    计算 EWMA 及趋势。
    Args:
        values_with_timestamps (list): 包含 (timestamp_seconds, value) 元组的列表。
                                       要求已按 timestamp_seconds 排序。
        span_minutes (int): EWMA 的时间跨度（分钟），影响平滑程度。

    Returns:
        tuple: (last_ewma_value, average_change_per_second)
               如果数据不足，则返回 (None, None)。
    """
    if len(values_with_timestamps) < 2:
        return None, None

    alpha = 2 / (span_minutes * 60 + 1)
    
    ewma_values = []
    
    ewma = values_with_timestamps[0][1]
    ewma_values.append(ewma)

    for i in range(1, len(values_with_timestamps)):
        ewma = alpha * values_with_timestamps[i][1] + (1 - alpha) * ewma
        ewma_values.append(ewma)

    if len(ewma_values) < 2:
        return ewma_values[-1] if ewma_values else None, None

    last_ewma = ewma_values[-1]
    second_last_ewma = ewma_values[-2]
    
    last_ts_sec = values_with_timestamps[-1][0]
    second_last_ts_sec = values_with_timestamps[-2][0]

    if last_ts_sec == second_last_ts_sec:
        avg_change_per_second = 0
    else:
        avg_change_per_second = (last_ewma - second_last_ewma) / (last_ts_sec - second_last_ts_sec)
    
    return last_ewma, avg_change_per_second


def interpolate_data(data, interval_minutes=5, target_keys=['totalUserCount', 'audienceCount'], 
                     max_extrapolation_minutes=60, default_max_value=100000, 
                     ewma_span_minutes=30, audience_decay_factor=0.95,
                     audience_extrapolation_minutes=5):
    """
    对非均匀采样的数据进行线性插值和EWMA平滑外推，生成指定间隔的数据。
    只对 target_keys 中指定的指标进行估算。
    引入外推值抑制策略，并针对 audienceCount 引入边界衰减。
    同时确保 totalUserCount 大于等于0且严格递增。

    Args:
        data (list): 包含字典的列表，每个字典至少包含 'timestamp' (datetime)
                     以及要插值的数值（如 'totalUserCount', 'audienceCount'）。
        interval_minutes (int): 插值的时间间隔，单位为分钟。
        target_keys (list): 一个字符串列表，包含要估算的指标键名（例如 ['totalUserCount']）。
        max_extrapolation_minutes (int): 允许进行**通用外推**的最大时间距离（分钟），影响 EWMA 外推。
        default_max_value (int): 估算值的最大限制。防止值过大。
        ewma_span_minutes (int): EWMA 平滑的时间跨度（分钟），影响趋势的平滑程度。
        audience_decay_factor (float): audienceCount 在边界外推时每分钟的衰减因子。
        audience_extrapolation_minutes (int): audienceCount 允许进行**边界衰减外推**的最大时间距离（分钟）。

    Returns:
        list: 包含字典的列表，每个字典代表一个估算点的数据。
              如果某个值无法估算，则该键将不会存在于该字典中。
    """
    if not data:
        return []

    interpolated_results = []
    
    start_time_data = data[0]['timestamp']
    end_time_data = data[-1]['timestamp']

    # 扩展的估算范围，确保所有可能的估算点（包括 audienceCount 的长距离衰减点）都被遍历到。
    max_overall_extrap_minutes_for_loop = max(max_extrapolation_minutes, audience_extrapolation_minutes)

    actual_start_time = start_time_data - timedelta(minutes=max_overall_extrap_minutes_for_loop)
    actual_end_time = end_time_data + timedelta(minutes=max_overall_extrap_minutes_for_loop)

    # 调整到最近的 interval_minutes 的倍数
    current_time_point = actual_start_time.replace(second=0, microsecond=0)
    if current_time_point.minute % interval_minutes != 0:
        current_time_point = current_time_point - timedelta(minutes=current_time_point.minute % interval_minutes)
        
    actual_end_time = actual_end_time.replace(second=0, microsecond=0)
    if actual_end_time.minute % interval_minutes != 0:
        actual_end_time = actual_end_time + timedelta(minutes=interval_minutes - (actual_end_time.minute % interval_minutes))


    def find_nearest_points_for_key(current_ts, key, sorted_data):
        p1_for_key = None
        p2_for_key = None
        
        # 寻找 p2_for_key (第一个时间戳 >= current_ts 且包含 key 的点)
        for i in range(len(sorted_data)):
            if sorted_data[i]['timestamp'] >= current_ts and key in sorted_data[i] and sorted_data[i][key] is not None:
                p2_for_key = sorted_data[i]
                break
        
        # 寻找 p1_for_key (最后一个时间戳 <= current_ts 且包含 key 的点)
        search_start_idx = len(sorted_data) - 1 if p2_for_key is None else sorted_data.index(p2_for_key)
        for i in range(search_start_idx, -1, -1):
            if sorted_data[i]['timestamp'] <= current_ts and key in sorted_data[i] and sorted_data[i][key] is not None:
                p1_for_key = sorted_data[i]
                break
                
        return p1_for_key, p2_for_key

    # 用于追踪上一个 totalUserCount 的值，以确保递增
    last_total_user_count = 0 

    while current_time_point <= actual_end_time: # 遍历扩展后的时间范围
        interpolated_entry = {'timestamp': current_time_point}
        
        for key_to_interpolate in target_keys:
            p1_for_key, p2_for_key = find_nearest_points_for_key(current_time_point, key_to_interpolate, data)

            current_value = None

            # --- 估算逻辑优先级 ---

            # 1. 精确匹配 (如果当前时间点就是某个原始数据点)
            if p1_for_key and p1_for_key['timestamp'] == current_time_point:
                current_value = p1_for_key[key_to_interpolate]
            elif p2_for_key and p2_for_key['timestamp'] == current_time_point:
                current_value = p2_for_key[key_to_interpolate]
            # 2. 双向线性插值 (必须有明确的左右两侧点，且时间不重合)
            elif p1_for_key and p2_for_key and p1_for_key['timestamp'] < p2_for_key['timestamp']:
                x1 = p1_for_key['timestamp'].timestamp()
                y1 = p1_for_key[key_to_interpolate]
                x2 = p2_for_key['timestamp'].timestamp()
                y2 = p2_for_key[key_to_interpolate]
                x_interp = current_time_point.timestamp()

                current_value = y1 + (y2 - y1) * ((x_interp - x1) / (x2 - x1))
                current_value = int(round(current_value))
            # 3. 单向外推 (针对 audienceCount 和其他指标分别处理)
            elif p1_for_key or p2_for_key: # 存在一个或两个点，但不是双向插值情况

                if key_to_interpolate == 'audienceCount':
                    # 针对 audienceCount 的边界衰减逻辑，使用独立的 audience_extrapolation_minutes
                    # 优先检查边界衰减逻辑，因为这是 audienceCount 的特殊需求
                    
                    if current_time_point < start_time_data: # 估算点在数据起始之前 (向左衰减)
                        first_audience_point = None
                        for d in data: # 找到第一个有 audienceCount 的点
                            if 'audienceCount' in d and d['audienceCount'] is not None:
                                first_audience_point = d
                                break
                        
                        if first_audience_point:
                            time_diff_minutes = (first_audience_point['timestamp'] - current_time_point).total_seconds() / 60
                            # 只有在 audience_extrapolation_minutes 范围内才进行衰减
                            if time_diff_minutes >= 0 and time_diff_minutes <= audience_extrapolation_minutes:
                                current_value = first_audience_point['audienceCount'] * (audience_decay_factor ** time_diff_minutes)
                                current_value = int(round(current_value))
                                if DEBUG_MODE:
                                    print(f"DEBUG: 时间点 {current_time_point.strftime('%Y-%m-%d %H:%M:%S')} - audienceCount 向左外推 (边界衰减): 原始 {first_audience_point['audienceCount']}, 衰减 {time_diff_minutes}分钟, 结果 {current_value}", file=sys.stderr)
                            elif DEBUG_MODE:
                                print(f"DEBUG: 时间点 {current_time_point.strftime('%Y-%m-%d %H:%M:%S')} - audienceCount 向左外推 (边界衰减) 距离 {time_diff_minutes:.1f}分钟 超出其限制 {audience_extrapolation_minutes}分钟。", file=sys.stderr)
                        elif DEBUG_MODE:
                            print(f"DEBUG: 时间点 {current_time_point.strftime('%Y-%m-%d %H:%M:%S')} - audienceCount 无法向左外推 (没有找到有效的 audienceCount 初始点)。", file=sys.stderr)

                    elif current_time_point > end_time_data: # 估算点在数据结束之后 (向右衰减)
                        last_audience_point = None
                        for i in range(len(data) -1, -1, -1): # 找到最后一个有 audienceCount 的点
                            if 'audienceCount' in data[i] and data[i]['audienceCount'] is not None:
                                last_audience_point = data[i]
                                break
                        
                        if last_audience_point:
                            time_diff_minutes = (current_time_point - last_audience_point['timestamp']).total_seconds() / 60
                            # 只有在 audience_extrapolation_minutes 范围内才进行衰减
                            if time_diff_minutes >= 0 and time_diff_minutes <= audience_extrapolation_minutes:
                                current_value = last_audience_point['audienceCount'] * (audience_decay_factor ** time_diff_minutes)
                                current_value = int(round(current_value))
                                if DEBUG_MODE:
                                    print(f"DEBUG: 时间点 {current_time_point.strftime('%Y-%m-%d %H:%M:%S')} - audienceCount 向右外推 (边界衰减): 原始 {last_audience_point['audienceCount']}, 衰减 {time_diff_minutes}分钟, 结果 {current_value}", file=sys.stderr)
                            elif DEBUG_MODE:
                                print(f"DEBUG: 时间点 {current_time_point.strftime('%Y-%m-%d %H:%M:%S')} - audienceCount 向右外推 (边界衰减) 距离 {time_diff_minutes:.1f}分钟 超出其限制 {audience_extrapolation_minutes}分钟。", file=sys.stderr)
                        elif DEBUG_MODE:
                            print(f"DEBUG: 时间点 {current_time_point.strftime('%Y-%m-%d %H:%M:%S')} - audienceCount 无法向右外推 (没有找到有效的 audienceCount 结束点)。", file=sys.stderr)

                    # 如果 audienceCount 已经通过边界衰减计算出值，则不再尝试 EWMA 外推
                    # 否则（即不在衰减范围内，或没有找到有效的 audienceCount 点），
                    # 并且当前时间点在通用 EWMA 外推范围内，才尝试 EWMA 外推。
                    if current_value is None: # 只有当边界衰减没有给出值时，才考虑 EWMA
                        # 尝试通用 EWMA 外推，但受 max_extrapolation_minutes 限制
                        if p1_for_key and current_time_point > end_time_data: # 只有左侧点，尝试向右 EWMA 外推
                            extrapolation_distance = (current_time_point - p1_for_key['timestamp']).total_seconds() / 60
                            if extrapolation_distance <= max_extrapolation_minutes: # 使用通用外推限制
                                relevant_points_for_ewma = []
                                # 收集EWMA计算所需的数据点
                                for i in range(data.index(p1_for_key), -1, -1):
                                    point = data[i]
                                    if key_to_interpolate in point and point[key_to_interpolate] is not None:
                                        if (p1_for_key['timestamp'] - point['timestamp']).total_seconds() / 60 <= ewma_span_minutes:
                                            relevant_points_for_ewma.append((point['timestamp'].timestamp(), point[key_to_interpolate]))
                                        else:
                                            break
                                relevant_points_for_ewma.reverse() # 保持时间升序
                                
                                ewma_val, ewma_change_rate = calculate_ewma_trend(relevant_points_for_ewma, ewma_span_minutes)
                                if ewma_val is not None and ewma_change_rate is not None:
                                    time_diff_seconds = current_time_point.timestamp() - relevant_points_for_ewma[-1][0]
                                    current_value = ewma_val + ewma_change_rate * time_diff_seconds
                                    current_value = int(round(current_value))
                                elif DEBUG_MODE:
                                    print(f"DEBUG: 时间点 {current_time_point.strftime('%Y-%m-%d %H:%M:%S')} - {key_to_interpolate} 无法向右EWMA外推 (数据不足或无趋势)。", file=sys.stderr)
                            elif DEBUG_MODE:
                                print(f"DEBUG: 时间点 {current_time_point.strftime('%Y-%m-%d %H:%M:%S')} - {key_to_interpolate} 无法向右EWMA外推 (距离 {extrapolation_distance:.1f}分钟 超出通用限制)。", file=sys.stderr)
                        
                        elif p2_for_key and current_time_point < start_time_data: # 只有右侧点，尝试向左 EWMA 外推
                            extrapolation_distance = (p2_for_key['timestamp'] - current_time_point).total_seconds() / 60
                            if extrapolation_distance <= max_extrapolation_minutes: # 使用通用外推限制
                                relevant_points_for_ewma = []
                                # 收集EWMA计算所需的数据点
                                for i in range(data.index(p2_for_key), len(data)):
                                    point = data[i]
                                    if key_to_interpolate in point and point[key_to_interpolate] is not None:
                                        if (point['timestamp'] - p2_for_key['timestamp']).total_seconds() / 60 <= ewma_span_minutes:
                                            relevant_points_for_ewma.append((point['timestamp'].timestamp(), point[key_to_interpolate]))
                                        else:
                                            break
                                
                                ewma_val, ewma_change_rate = calculate_ewma_trend(relevant_points_for_ewma, ewma_span_minutes)
                                if ewma_val is not None and ewma_change_rate is not None:
                                    time_diff_seconds = current_time_point.timestamp() - relevant_points_for_ewma[0][0]
                                    current_value = ewma_val + ewma_change_rate * time_diff_seconds
                                    current_value = int(round(current_value))
                                elif DEBUG_MODE:
                                    print(f"DEBUG: 时间点 {current_time_point.strftime('%Y-%m-%d %H:%M:%S')} - {key_to_interpolate} 无法向左EWMA外推 (数据不足或无趋势)。", file=sys.stderr)
                            elif DEBUG_MODE:
                                print(f"DEBUG: 时间点 {current_time_point.strftime('%Y-%m-%d %H:%M:%S')} - {key_to_interpolate} 无法向左EWMA外推 (距离 {extrapolation_distance:.1f}分钟 超出通用限制)。", file=sys.stderr)

                else: # 对于非 audienceCount 的指标 (例如 totalUserCount)，只使用 EWMA 平滑外推，受 max_extrapolation_minutes 限制
                    if p1_for_key and current_time_point > end_time_data: # 只有左侧点，尝试向右外推
                        extrapolation_distance = (current_time_point - p1_for_key['timestamp']).total_seconds() / 60
                        if extrapolation_distance <= max_extrapolation_minutes:
                            relevant_points_for_ewma = []
                            for i in range(data.index(p1_for_key), -1, -1):
                                point = data[i]
                                if key_to_interpolate in point and point[key_to_interpolate] is not None:
                                    if (p1_for_key['timestamp'] - point['timestamp']).total_seconds() / 60 <= ewma_span_minutes:
                                        relevant_points_for_ewma.append((point['timestamp'].timestamp(), point[key_to_interpolate]))
                                    else:
                                        break
                            relevant_points_for_ewma.reverse()
                            
                            ewma_val, ewma_change_rate = calculate_ewma_trend(relevant_points_for_ewma, ewma_span_minutes)
                            if ewma_val is not None and ewma_change_rate is not None:
                                time_diff_seconds = current_time_point.timestamp() - relevant_points_for_ewma[-1][0]
                                current_value = ewma_val + ewma_change_rate * time_diff_seconds
                                current_value = int(round(current_value))
                            elif DEBUG_MODE:
                                print(f"DEBUG: 时间点 {current_time_point.strftime('%Y-%m-%d %H:%M:%S')} - {key_to_interpolate} 无法向右EWMA外推 (数据不足或无趋势)。", file=sys.stderr)
                        elif DEBUG_MODE:
                            print(f"DEBUG: 时间点 {current_time_point.strftime('%Y-%m-%d %H:%M:%S')} - {key_to_interpolate} 无法向右EWMA外推 (距离 {extrapolation_distance:.1f}分钟 超出限制)。", file=sys.stderr)
                    
                    elif p2_for_key and current_time_point < start_time_data: # 只有右侧点，尝试向左外推
                        extrapolation_distance = (p2_for_key['timestamp'] - current_time_point).total_seconds() / 60
                        if extrapolation_distance <= max_extrapolation_minutes:
                            relevant_points_for_ewma = []
                            for i in range(data.index(p2_for_key), len(data)):
                                point = data[i]
                                if key_to_interpolate in point and point[key_to_interpolate] is not None:
                                    if (point['timestamp'] - p2_for_key['timestamp']).total_seconds() / 60 <= ewma_span_minutes:
                                        relevant_points_for_ewma.append((point['timestamp'].timestamp(), point[key_to_interpolate]))
                                    else:
                                        break
                            
                            ewma_val, ewma_change_rate = calculate_ewma_trend(relevant_points_for_ewma, ewma_span_minutes)
                            if ewma_val is not None and ewma_change_rate is not None:
                                time_diff_seconds = current_time_point.timestamp() - relevant_points_for_ewma[0][0]
                                current_value = ewma_val + ewma_change_rate * time_diff_seconds
                                current_value = int(round(current_value))
                            elif DEBUG_MODE:
                                print(f"DEBUG: 时间点 {current_time_point.strftime('%Y-%m-%d %H:%M:%S')} - {key_to_interpolate} 无法向左EWMA外推 (数据不足或无趋势)。", file=sys.stderr)
                        elif DEBUG_MODE:
                            print(f"DEBUG: 时间点 {current_time_point.strftime('%Y-%m-%d %H:%M:%S')} - {key_to_interpolate} 无法向左EWMA外推 (距离 {extrapolation_distance:.1f}分钟 超出限制)。", file=sys.stderr)
            
            # --- 钳制估算值 (优先级最高，最后应用) ---
            if current_value is not None:
                current_value = max(0, current_value) # 确保非负

                # 确保 totalUserCount 递增
                if key_to_interpolate == 'totalUserCount':
                    # 如果当前估算值小于上一个 totalUserCount，则修正为上一个值
                    # 并且只能从上一个估算点获取，不能从原始数据点获取。
                    # 这是一个局部性原则，确保在插值或外推过程中保持单调性。
                    if current_value < last_total_user_count:
                        if DEBUG_MODE:
                            print(f"DEBUG: 时间点 {current_time_point.strftime('%Y-%m-%d %H:%M:%S')} - totalUserCount 修正: 原始估算 {current_value}, 前一个值 {last_total_user_count}。修正为 {last_total_user_count}", file=sys.stderr)
                        current_value = last_total_user_count
                    last_total_user_count = current_value # 更新上一个 totalUserCount 的值
                
                current_value = min(default_max_value, current_value) # 确保不超过最大值

            # 如果成功估算出值，则添加到结果字典中
            if current_value is not None:
                interpolated_entry[key_to_interpolate] = current_value
            elif DEBUG_MODE:
                print(f"DEBUG: 时间点 {current_time_point.strftime('%Y-%m-%d %H:%M:%S')} 未估算出任何有效计数，跳过此条目。", file=sys.stderr)

        # 只有当估算结果中包含除时间戳以外的任何一个计数，才加入结果列表
        # 并且如果在当前时间点估算了 totalUserCount，更新 last_total_user_count
        if len(interpolated_entry) > 1:
            interpolated_results.append(interpolated_entry)
        elif DEBUG_MODE:
            print(f"DEBUG: 时间点 {current_time_point.strftime('%Y-%m-%d %H:%M:%S')} 未估算出任何有效计数，跳过此条目。", file=sys.stderr)

        current_time_point += timedelta(minutes=interval_minutes)
        
    return interpolated_results

def print_ascii_bar_chart(data, value_key, chart_title, bar_char='#',
                          max_width=50, block_style=True):
    """
    在控制台打印柱状图。

    Args:
        data (list): 包含字典的列表，每个字典至少包含 'timestamp' 和 value_key。
        value_key (str): 要绘制柱状图的数值键名。
        chart_title (str): 图表的标题。
        bar_char (str): 非 block 模式下用于整格填充的字符。
        max_width (int): 柱状图的最大宽度（字符数）。
        block_style (bool): 是否使用八分块字符细化刻度。
    """
    chart_data = [entry for entry in data if value_key in entry and entry[value_key] is not None]

    if not chart_data:
        print(f"\n{chart_title}: 无有效数据可绘制。")
        return

    values = [entry[value_key] for entry in chart_data]

    max_value = max(values)
    if max_value == 0:
        print(f"\n{chart_title}: 所有 '{value_key}' 值为零，无法绘制有意义的图表。")
        return

    print(f"\n--- {chart_title} ---")
    print(f"最大值: {max_value}")

    max_ts_len = max(len(entry['timestamp'].strftime("%Y-%m-%d %H:%M")) for entry in chart_data)
    labels = [entry['timestamp'].strftime("%Y-%m-%d %H:%M").ljust(max_ts_len)
              for entry in chart_data]

    # 单文件图的柱子保持参差长度（不补齐），因此 pad=False
    for line in build_bar_lines(labels, values, max_value, max_width,
                                block_style, bar_char):
        print(line)


def resolve_color_mode(mode):
    """
    决定是否启用终端着色。

    Args:
        mode (str): 'auto' | 'always' | 'never'

    Returns:
        bool: True 表示应当输出 ANSI 颜色序列。
    """
    if mode == "always":
        return True
    if mode == "never":
        return False

    # auto 模式：仅当输出到交互式终端时才着色，避免重定向到文件时写入转义乱码
    if os.environ.get("NO_COLOR"):
        return False
    if os.environ.get("TERM") == "dumb":
        return False
    try:
        return sys.stdout.isatty()
    except Exception:
        return False


def paint(text, color):
    """按需为文本包裹 ANSI 颜色序列。"""
    if not COLOR_ENABLED or not color:
        return text
    return f"{color}{text}{COLOR_RESET}"


def resolve_color(color):
    """
    按着色开关返回可用的颜色码，供纯函数直接嵌入 ANSI 序列。

    与 paint() 的区别是返回颜色码本身而非已着色的文本，从而让绘图纯函数
    无需读取 COLOR_ENABLED 全局量。

    Args:
        color (str): 原始 ANSI 颜色码，None 或空串表示无色。

    Returns:
        str: 着色开启且 color 非空时返回 color，否则返回 None。
    """
    return color if (COLOR_ENABLED and color) else None


def render_bar(value, max_value, max_width, block_style=True, bar_char='#'):
    """
    按比例渲染一根柱子。

    block_style 为真时用八分块字符（BLOCK_EIGHTHS）补足尾数，把刻度从
    1/max_width 细化到 1/(8*max_width)；为假时保持原来的整格填充行为。

    渲染宽度恒不超过 max_width，因此调用方仍可用 ljust(max_width) 对齐数值列。

    Args:
        value (int): 当前数值。None 或 0 一律渲染为空柱。
        max_value (int): 本图的最大值（标尺上限）。
        max_width (int): 柱子的最大宽度（字符数）。
        block_style (bool): 是否使用八分块字符。
        bar_char (str): 非 block 模式下用于整格填充的字符。

    Returns:
        str: 柱子字符串。
    """
    if value is None or value <= 0 or max_value <= 0 or max_width <= 0:
        return ''

    ratio = min(1.0, value / max_value)

    if block_style:
        steps = len(BLOCK_EIGHTHS)
        # 保证非零值至少占 1/8 格，否则极小值会渲染成空柱而看不出区别
        units = max(1, int(round(ratio * steps * max_width)))
        full, rem = divmod(units, steps)
        return BLOCK_EIGHTHS[-1] * full + (BLOCK_EIGHTHS[rem - 1] if rem else '')

    # 保证非零值至少有一格，否则小数值会渲染成空柱而看不出区别
    return bar_char * max(1, int(ratio * max_width))


def collect_series(data, value_key, times):
    """
    按给定的时间戳序列取出某个指标的值序列，缺测的位置为 None。

    times 通常取两侧时间戳的并集，因此本函数天然表达「逐行可对齐、缺失为 None」
    的语义，供绘图层区分「真的是 0」与「没有数据」。

    Args:
        data (list): 估算结果列表。
        value_key (str): 指标键名。
        times (list): 时间戳序列（datetime对象），通常已排序。

    Returns:
        list: 与 times 等长的列表，元素为该时刻的数值或 None。
    """
    index = index_by_timestamp(data)
    return [get_metric(index.get(ts), value_key) for ts in times]


def build_bar_row(label, value, max_value, max_width, block_style=True,
                  bar_char='#', pad=False, color=None):
    """
    渲染「标签 | 柱子 数值」一整行。

    value 为 None（缺测）时渲染空柱并在数值位显示 '-'；value 为 0 时同样是空柱，
    但数值位显示真实的 0。

    给定 color 时柱子与数值各自独立包裹 ANSI 序列：补齐必须发生在着色之前，
    否则 ljust 会把转义序列算进宽度，导致数值列错位。

    Args:
        label (str): 行首标签（时间戳、相对分钟等）。
        value (int): 该行的数值，None 表示缺测。
        max_value (int): 标尺上限。
        max_width (int): 柱子最大宽度（字符数）。
        block_style (bool): 是否使用八分块字符。
        bar_char (str): 非 block 模式下用于整格填充的字符。
        pad (bool): True 时把柱子右侧补空格到 max_width，使数值列对齐。
        color (str): 施加到柱子与数值上的 ANSI 颜色码；None 表示不着色。

    Returns:
        str: 一行文本，不含换行符。
    """
    bar = render_bar(value, max_value, max_width, block_style, bar_char)
    if pad:
        bar = bar.ljust(max_width)
    text = '-' if value is None else str(value)
    if color:
        return (f"{label} | {color}{bar}{COLOR_RESET} "
                f"{color}{text}{COLOR_RESET}")
    return f"{label} | {bar} {text}"


def build_bar_lines(labels, values, max_value, max_width, block_style=True,
                    bar_char='#', pad=False):
    """
    逐行渲染柱状图内容，返回与输入一一对应的文本列表。

    Args:
        labels (list): 行标签列表。
        values (list): 与 labels 等长的数值列表，元素可为 None。
        max_value (int): 标尺上限。
        max_width (int): 柱子最大宽度（字符数）。
        block_style (bool): 是否使用八分块字符。
        bar_char (str): 非 block 模式下用于整格填充的字符。
        pad (bool): 是否把柱子补齐到 max_width。

    Returns:
        list: 行文本列表。
    """
    return [build_bar_row(label, value, max_value, max_width, block_style,
                          bar_char, pad)
            for label, value in zip(labels, values)]


def shift_to_relative_epoch(raw_data):
    """
    将 raw_data 整体平移，使首个数据点落在 COMPARISON_EPOCH 上。

    两场直播的起播时刻不同（墙钟无法对齐），且 interpolate_data 会把估算栅格
    对齐到墙钟的 interval_minutes 整数倍。若直接对原始时间戳估算，两侧的栅格
    会相差一个无法整除的余数，导致每一行只有一侧有数据、完全无法比对。
    平移到统一基准后，两侧栅格相位一致。

    Args:
        raw_data (list): extract_log_data 的结果。

    Returns:
        tuple: (平移后的数据列表, 原始起始时间)
    """
    base = raw_data[0]['timestamp']
    shifted = []
    for entry in raw_data:
        item = {
            'timestamp': COMPARISON_EPOCH
            + timedelta(seconds=(entry['timestamp'] - base).total_seconds())
        }
        for key, value in entry.items():
            if key != 'timestamp':
                item[key] = value
        shifted.append(item)
    return shifted, base


def relative_minutes(ts):
    """返回时间点相对于比对基准的分钟数（可为负，代表起播前的外推区间）。"""
    return int((ts - COMPARISON_EPOCH).total_seconds() // 60)


def index_by_timestamp(data):
    """把估算结果列表转成 {timestamp: entry} 字典，便于按行合并比对。"""
    return {entry['timestamp']: entry for entry in data}


def get_metric(entry, value_key):
    """安全取值：entry 不存在或缺少该键时返回 None。"""
    if entry is None:
        return None
    value = entry.get(value_key)
    return value


def format_metric_cell(value, width, color=None):
    """把单个指标值格式化为定宽单元格，缺失时显示短横线。"""
    text = "-" if value is None else str(value)
    return f"{paint(text.rjust(width), color)}"


def print_comparison_table(data_a, data_b, target_metrics):
    """
    打印 A/B 并排的数据表。

    每个指标的 A、B 两列相邻，使同一指标的两个数字可直接横向比对。
    行取两侧时间戳的并集，缺失的一方显示 '-'。

    Args:
        data_a (list): A 日志的估算结果。
        data_b (list): B 日志的估算结果。
        target_metrics (list): 要展示的指标键名。
    """
    index_a = index_by_timestamp(data_a)
    index_b = index_by_timestamp(data_b)
    all_times = sorted(set(index_a) | set(index_b))

    metric_width = 6
    cell_width = metric_width + 1
    group_width = cell_width * 2 + 3  # 每组指标占用的宽度（含两侧空格与分隔符）

    header_cells = ""
    for metric in target_metrics:
        short = METRIC_SHORT_NAMES.get(metric, metric)
        header_cells += (
            f" {paint(short + ' A', COLOR_A).rjust(cell_width)}"
            f" {paint(short + ' B', COLOR_B).rjust(cell_width)} |"
        )

    print("\n--- 估算后的数据（A / B 并排比对）---")
    print(f"{'T+':>5} |{header_cells}")
    print('-' * (8 + group_width * len(target_metrics)))

    for ts in all_times:
        line = f"{relative_minutes(ts):>5} |"
        for metric in target_metrics:
            line += (
                f" {format_metric_cell(get_metric(index_a.get(ts), metric), metric_width, COLOR_A)}"
                f" {format_metric_cell(get_metric(index_b.get(ts), metric), metric_width, COLOR_B)} |"
            )
        print(line)


def print_comparison_bar_chart(data_a, data_b, value_key, chart_title,
                               max_width=24, block_style=True):
    """
    打印 A/B 交错的柱状图。

    每个时间点先打印 A 行再打印 B 行，A/B 各自使用颜色与标签以便区分；
    着色开启时柱体与数值都带上各自一侧的颜色，标签列同样着色，
    ascii 模式下还额外用不同柱字符区分，黑白打印同样可辨。
    同一指标的两侧共用一把标尺（取两侧该指标的最大值），保证柱长可跨方比较。

    Args:
        data_a (list): A 日志的估算结果。
        data_b (list): B 日志的估算结果。
        value_key (str): 要绘制的指标键名。
        chart_title (str): 图表标题。
        max_width (int): 单侧柱子的最大宽度（字符数）。
        block_style (bool): 是否使用八分块字符细化刻度。
    """
    all_times = sorted(set(index_by_timestamp(data_a))
                       | set(index_by_timestamp(data_b)))

    # 两侧都按并集时间戳取值，缺测为 None，从而与「真实为 0」区分开
    series_a = collect_series(data_a, value_key, all_times)
    series_b = collect_series(data_b, value_key, all_times)

    known = [v for v in series_a + series_b if v is not None]

    if not known:
        print(f"\n--- {chart_title} ---")
        print(f"A/B: 无有效数据可绘制。")
        return

    shared_max = max(known)
    peak_a = max([v for v in series_a if v is not None], default=None)
    peak_b = max([v for v in series_b if v is not None], default=None)

    print(f"\n--- {chart_title}（A / B 并排比对）---")
    if shared_max == 0:
        print(f"共同标尺最大值: 0（所有值均为零，无法绘制有意义的图表）")
        return
    print(f"共同标尺最大值: {shared_max}   (A峰值 {peak_a} / B峰值 {peak_b})")

    for i, ts in enumerate(all_times):
        for tag, values, bar_char, color in (
            ('A', series_a, BAR_CHAR_A, COLOR_A),
            ('B', series_b, BAR_CHAR_B, COLOR_B),
        ):
            # 着色只在这一层施加，绘图函数本身保持无副作用
            label = f"{relative_minutes(ts):>5} {paint(tag, color)}"
            print(build_bar_row(label, values[i], shared_max, max_width,
                                block_style, bar_char, pad=True,
                                color=resolve_color(color)))


# --- Sixel 绘图层 ---

def resolve_chart_format(mode):
    """
    决定柱状图用 Sixel 图形还是字符条。

    不做终端能力探测（DA1 之类）：Sixel 支持情况靠 --chart-format 显式控制，
    探测需要把 stdin 切进 raw 模式，对一个数据处理脚本来说代价太大。

    Args:
        mode (str): 'auto' | 'sixel' | 'ascii'

    Returns:
        str: 'sixel' 或 'ascii'
    """
    if mode == 'ascii':
        return 'ascii'
    if mode == 'sixel':
        return 'sixel'

    # auto 模式：只有交互式终端才出图。输出被重定向到文件或管道时，
    # 写进去的只会是几 KB 没法看的转义序列，不如退回字符图。
    if sixel_plot is None:
        return 'ascii'
    try:
        return 'sixel' if sys.stdout.isatty() else 'ascii'
    except Exception:
        return 'ascii'


def read_cursor_position(timeout=0.4):
    """
    查询当前光标所在行/列，返回 0 基 (row, col)；拿不到时返回 None。

    用的是 DEC 的 DSR 查询（ESC[6n），终端会回一个 ESC[row;colR。
    查询期间要把终端切成 raw 模式（否则回应会被行规程吃掉），
    结束后原样恢复；任何一步出问题都安静地返回 None——
    拿不到位置只是画不了真字体刻度，不该拖垮整张图。

    输入来源优先 /dev/tty：即使 stdin 被管道占着（`cmd | less`），
    只要 stdout 还连着终端，就能问到控制终端的位置。
    """
    if termios is None or tty is None:
        return None
    try:
        if not sys.stdout.isatty():
            return None
    except Exception:
        return None

    fd, owned = None, False
    try:
        try:
            fd = os.open('/dev/tty', os.O_RDONLY | os.O_NOCTTY)
            owned = True
        except OSError:
            if not sys.stdin.isatty():
                return None
            fd = sys.stdin.fileno()
        saved = termios.tcgetattr(fd)
        try:
            tty.setraw(fd)
            sys.stdout.write('\x1b[6n')
            sys.stdout.flush()
            buf = b''
            deadline = time.monotonic() + timeout
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return None
                ready, _, _ = select.select([fd], [], [], remaining)
                if not ready:
                    return None
                chunk = os.read(fd, 64)
                if not chunk:
                    return None
                buf += chunk
                m = re.search(rb'\x1b\[(\d+);(\d+)R', buf)
                if m:
                    return int(m.group(1)) - 1, int(m.group(2)) - 1
        finally:
            try:
                termios.tcsetattr(fd, termios.TCSADRAIN, saved)
            except Exception:
                pass
    except Exception:
        return None
    finally:
        if owned and fd is not None:
            try:
                os.close(fd)
            except Exception:
                pass


def y_label_placements(tick_rows, image_rows, chart_height):
    """
    把 Y 轴刻度所在的 band 换算成「图像占用的终端行」下标。

    图像在终端里被压缩成 image_rows 行，band 与终端行之间是纯比例关系：
    取该 band 中心在整幅图高里的比例，乘上行数即落点。

    Args:
        tick_rows (list): (刻度值, band 行号)，自下而上，来自 y_tick_rows()。
        image_rows (int): 图像实际占用的终端行数。
        chart_height (int): 图像像素高度。

    Returns:
        list: [(刻度值, 0 基行下标), ...]，行下标保证落在 [0, image_rows)。
              两档撞到同一行时只保留先出现的那档（更靠下、值更小），
              免得两串数字叠在同一行互相涂花。
    """
    if image_rows <= 0 or chart_height <= 0:
        return []
    placed, seen = [], set()
    for value, band in tick_rows:
        y_px = band * sixel_plot.BAND_HEIGHT + sixel_plot.BAND_HEIGHT / 2.0
        idx = int(y_px / chart_height * image_rows)
        idx = max(0, min(idx, image_rows - 1))
        if idx in seen:
            continue
        seen.add(idx)
        placed.append((value, idx))
    return placed


def format_axis_line(point_labels, plot_x0=0, plot_width=None):
    """
    把数据点标签铺成一条与图像等宽的坐标轴刻度行。

    Sixel 图里画不了文字，所以坐标轴只能用终端文本另起一行画在图下方；
    标签按各自数据点所在的列定位，放不下就跳过，绝不互相压字。

    Args:
        point_labels (list): 每个数据点一个字符串标签。
        plot_x0 (int): 绘图区左边界在画布上的像素列（左侧是 Y 轴刻度栏）。
        plot_width (int): 绘图区像素宽度，None 表示「画布减掉刻度栏」。

    Returns:
        str: 一行刻度文字（右侧多余空格已去掉）。
    """
    if plot_width is None:
        plot_width = sixel_plot.CANVAS_WIDTH - plot_x0
    gutter_cols = plot_x0 // sixel_plot.CELL_WIDTH_PX
    cols = sixel_plot.canvas_columns(plot_width)
    n = len(point_labels)
    if not n or cols <= 0:
        return ''

    # 每个标签至少留 12 列间隔，否则 800px 宽的图上会挤成一坨
    step = max(1, -(-n // max(1, cols // 12)))
    cells = [' '] * (gutter_cols + cols)
    for i in range(0, n, step):
        text = point_labels[i]
        if len(text) > cols:
            continue
        col = gutter_cols + sixel_plot.column_of_point(i, n, plot_width)
        col = max(gutter_cols, min(col, len(cells) - len(text)))
        if any(c != ' ' for c in cells[col:col + len(text)]):
            continue
        cells[col:col + len(text)] = text
    return ''.join(cells).rstrip()


def emit_sixel_chart(series_values, point_labels, max_value, header_lines,
                     chart_height):
    """
    渲染并输出一张 Sixel 图；自检不通过时返回 False，由调用方回退字符图。

    Y 轴刻度优先走「真字体」路线：图内只留一条空刻度栏，画完图再用
    CUP 把刻度值写进那几列——字号与 X 轴刻度完全一致。多数终端不允许
    在 sixel 图像区域内写字符（会把整张图擦掉），所以图必须整体右移
    gutter_cols 列，让刻度栏成为图像从未占据的干净字符格。
    查不到光标位置（输出被重定向、终端不支持 ESC[6n……）就退回
    原先的图内点阵数字方案，输出与旧版逐字节一致。

    Args:
        series_values (list): 每个系列一个数值列表，None 表示缺测。
        point_labels (list): 每个数据点一个坐标轴标签。
        max_value (int): 各系列共用的标尺上限。
        header_lines (list): 图前打印的文字（标题、标尺说明）。
        chart_height (int): 图高（像素），必须是 6 的整数倍。

    Returns:
        bool: True 表示图已输出。
    """
    # 第一次探测只问「这个终端肯不肯回光标位置」，据此决定用哪种刻度：
    # 查得到位置才敢把点阵字换成真字体（要靠 CUP 写字），否则照旧画点阵
    can_probe = read_cursor_position() is not None
    payload, layout, ok, problems, stats = sixel_plot.render_chart(
        series_values, max_value, sixel_plot.CANVAS_WIDTH, chart_height,
        pixel_ticks=not can_probe)
    if not ok:
        reason = problems[0] if problems else '未知原因'
        print(f"Sixel 绘图自检未通过，已退回字符图：{reason}", file=sys.stderr)
        return False

    plot_x0 = stats['plot_x0']
    plot_width = stats['plot_width']
    ticks = stats['y_ticks']

    for line in header_lines:
        print(line)
    print(f"Y 轴刻度（自下而上）: {' / '.join(str(t) for t in ticks)}")
    if layout.overflow:
        print(f"注意: {len(point_labels)} 个数据点超出 {plot_width}px "
              f"绘图区宽度，已按 1px 连续绘制（靠后的点会覆盖靠前的点）。")

    image_written = False
    if not stats['pixel_ticks']:
        # 图像起点要现问：前面刚打印过标题行，光标已经不在探测时的位置了
        cursor0 = read_cursor_position()
        if cursor0 is None:
            # 两次探测之间丢了能力：改画点阵字重走普通路径（标题行不受影响）
            payload, layout, ok, problems, stats = sixel_plot.render_chart(
                series_values, max_value, sixel_plot.CANVAS_WIDTH, chart_height,
                pixel_ticks=True)
            if not ok:
                reason = problems[0] if problems else '未知原因'
                print(f"Sixel 绘图自检未通过，已退回字符图：{reason}",
                      file=sys.stderr)
                return False
            plot_x0 = stats['plot_x0']
            plot_width = stats['plot_width']
        else:
            gutter_cols = stats['gutter_cols']
            # DECSC 存一个还原点：万一图后量不到位置，还能退回到图像起点
            sys.stdout.write('\x1b7')
            sys.stdout.write(f'\x1b[{gutter_cols + 1}G')   # CHA：让开刻度栏
            sys.stdout.write(payload)
            sys.stdout.flush()

            cursor1 = read_cursor_position()
            image_rows = (cursor1[0] - cursor0[0]
                          if cursor1 and cursor1[0] > cursor0[0] else 0)
            if image_rows:
                placements = y_label_placements(
                    sixel_plot.y_tick_rows(max_value, chart_height),
                    image_rows, chart_height)
                if placements:
                    sys.stdout.write('\x1b8')   # 回图像起点，再绝对定位逐行写
                    for value, idx in placements:
                        text = str(value).rjust(gutter_cols)
                        if len(text) > gutter_cols:
                            continue
                        sys.stdout.write(
                            f'\x1b[{cursor0[0] + idx + 1};1H{text}')
                    sys.stdout.write(
                        f'\x1b[{cursor1[0] + 1};{cursor1[1] + 1}H')  # 归位
                    sys.stdout.flush()
            # 量不出图高就不动光标：留在终端自己停的位置，照旧发 X 轴刻度
            image_written = True

    if not image_written:
        sys.stdout.write(payload)
        sys.stdout.flush()

    print()
    print(format_axis_line(point_labels, plot_x0, plot_width))
    return True


def print_sixel_bar_chart(data, value_key, chart_title, chart_height=None):
    """
    用 Sixel 画单文件趋势图：时间沿 X 轴展开、值沿 Y 轴上升。

    Args:
        data (list): 估算结果列表。
        value_key (str): 要绘制的指标键名。
        chart_title (str): 图表标题。
        chart_height (int): 图高（像素），None 表示用默认值。
    """
    if chart_height is None:
        chart_height = sixel_plot.DEFAULT_CHART_HEIGHT

    chart_data = [e for e in data
                  if value_key in e and e[value_key] is not None]
    if not chart_data:
        print(f"\n{chart_title}: 无有效数据可绘制。")
        return

    values = [e[value_key] for e in chart_data]
    max_value = max(values)
    if max_value == 0:
        print(f"\n{chart_title}: 所有 '{value_key}' 值为零，无法绘制有意义的图表。")
        return

    labels = [e['timestamp'].strftime("%H:%M") for e in chart_data]
    emit_sixel_chart(
        [values], labels, max_value,
        [f"\n--- {chart_title} ---", f"最大值: {max_value}"],
        chart_height)


def print_sixel_comparison_chart(data_a, data_b, value_key, chart_title,
                                 chart_height=None, block_style=True):
    """
    用 Sixel 画 A/B 并排比对图：同一时间点两根柱（青=A、品红=B），共用一把标尺。

    Args:
        data_a (list): A 日志的估算结果。
        data_b (list): B 日志的估算结果。
        value_key (str): 要绘制的指标键名。
        chart_title (str): 图表标题。
        chart_height (int): 图高（像素），None 表示用默认值。
        block_style (bool): 自检不过而回退字符图时使用的刻度风格。
    """
    if chart_height is None:
        chart_height = sixel_plot.DEFAULT_CHART_HEIGHT

    all_times = sorted(set(index_by_timestamp(data_a))
                       | set(index_by_timestamp(data_b)))
    series_a = collect_series(data_a, value_key, all_times)
    series_b = collect_series(data_b, value_key, all_times)

    known = [v for v in series_a + series_b if v is not None]
    title = f"\n--- {chart_title}（A / B 并排比对）---"
    if not known:
        print(title)
        print("A/B: 无有效数据可绘制。")
        return

    shared_max = max(known)
    if shared_max == 0:
        print(title)
        print("共同标尺最大值: 0（所有值均为零，无法绘制有意义的图表）")
        return

    peak_a = max([v for v in series_a if v is not None], default=None)
    peak_b = max([v for v in series_b if v is not None], default=None)
    labels = [str(relative_minutes(ts)) for ts in all_times]

    if not emit_sixel_chart(
            [series_a, series_b], labels, shared_max,
            [title,
             f"共同标尺最大值: {shared_max}   (A峰值 {peak_a} / B峰值 {peak_b})"],
            chart_height):
        # 自检没过就退回字符图，保证用户总能拿到可读的输出
        print_comparison_bar_chart(data_a, data_b, value_key, chart_title,
                                   block_style=block_style)


# --- 主执行块 ---
if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="从 events.db 提取、估算并可视化 totalUserCount 和 audienceCount。",
        formatter_class=argparse.RawTextHelpFormatter
    )
    # 场次选择参数（位置参数）
    parser.add_argument(
        "session",
        nargs="?",
        default=None,
        help="场次：'2026-10-11:1'（当天第1场）、'2026-10-11:-1'（最后一场）、"
             "'2026-10-11'（仅1场时直选）、或精确 session id。"
             "缺省取最新场次。用 --list-sessions 查看清单。"
    )
    # 并排比对的第二个场次
    parser.add_argument(
        "--compare", "-c",
        default=None,
        help="用于并排比对的第二个场次（写法同位置参数）。指定后进入 A/B 比对模式："
             "两场的估算结果与柱状图并排显示。"
             "横轴为**各自开播后的相对分钟 T+X**（两场直播起播时刻不同，墙钟无法直接对齐），"
             "同一指标的 A、B 共用同一把标尺，"
             "默认靠 A/B 标签与颜色区分（--bar-style ascii 时改用 #/@ 柱字符区分）。"
             "不指定时行为与原来完全一致。"
    )
    parser.add_argument(
        "--list-sessions",
        action="store_true",
        help="列出全部场次（含天内序号）后退出。"
    )
    parser.add_argument(
        "-r", "--room",
        dest="rooms",
        default=None,
        metavar="ROOMS",
        help="房间号，逗号分隔（过滤场次池与 --list-sessions 输出）。"
    )
    parser.add_argument(
        "--db",
        dest="db",
        default=None,
        metavar="PATH",
        help=f"events.db 路径（缺省 {session_db.DEFAULT_DB_PATH}）。"
    )
    # 终端着色
    parser.add_argument(
        "--color",
        choices=['auto', 'always', 'never'],
        default='auto',
        help="并排比对时是否对 A/B 着色。"
             "auto(默认): 仅当输出到交互式终端且未设置 NO_COLOR 时着色，"
             "重定向到文件时自动退回纯字符形态，避免写入转义乱码。"
             "always: 始终着色。never: 从不着色。"
    )
    # 柱状图刻度风格
    parser.add_argument(
        "--bar-style",
        choices=['block', 'ascii'],
        default='block',
        help="柱状图的刻度风格。"
             "block(默认): 使用八分块字符 ▏▎▍▌▋▊▉█ 填充，"
             "刻度细化到 1/(8×柱宽)，比对模式下 A/B 柱形一致，靠 A/B 标签与颜色区分。"
             "ascii: 保持整格填充（比对模式用 #/@ 区分 A/B），适合终端不支持方块字符时使用。"
             "仅在 --chart-format ascii 时生效。"
    )
    # 柱状图渲染后端
    parser.add_argument(
        "--chart-format",
        choices=['auto', 'sixel', 'ascii'],
        default='auto',
        help="柱状图的渲染后端。"
             "sixel: 渲染成 Sixel 二维图形（时间沿 X 轴、值沿 Y 轴），"
             "需要终端支持 Sixel 协议（Windows Terminal 1.22+）。"
             "ascii: 渲染成字符柱状图。"
             "auto(默认): 输出到交互式终端时用 sixel，被重定向到文件或管道时用 ascii。"
             "auto 不做终端能力探测，终端不支持 Sixel 时请显式指定 ascii。"
    )
    # Sixel 图高
    parser.add_argument(
        "--chart-height",
        type=int,
        default=None,
        help="Sixel 图的高度（像素），必须是 6 的整数倍。"
             "默认 240（相当于 40 个终端行）。仅在 --chart-format sixel 时生效。"
    )
    # 调试模式
    parser.add_argument(
        "--debug", "-d",
        action="store_true",
        help="启用调试模式以打印额外的运行时信息。"
    )
    # 估算间隔
    parser.add_argument(
        "--interval", "-i",
        type=int,
        default=5,
        help="估算数据的时间间隔（分钟），默认为5分钟。"
    )
    # 估算指标
    parser.add_argument(
        "--metrics", "-m",
        nargs='*',
        choices=['totalUserCount', 'audienceCount'],
        help="""选择要估算的指标。
可选值: totalUserCount, audienceCount。
可以同时指定多个，例如: -m totalUserCount audienceCount
如果未指定，默认将估算所有可用指标。
必须至少选择一个指标进行估算。"""
    )
    # 通用最大外推距离
    parser.add_argument(
        "--max-extrapolation-minutes", "-e", # 新增短参数 -e
        type=int,
        default=5,
        help="允许进行**除 audienceCount 以外的通用 EWMA 外推**的最大时间距离（分钟）。"
             "超出此距离则不外推。默认为5分钟。"
    )
    # 估算值最大限制
    parser.add_argument(
        "--max-value", "-v", # 新增短参数 -v
        type=int,
        default=100000,
        help="估算值的最大限制。外推值不会超过此值。默认为100000。"
    )
    # EWMA 平滑跨度
    parser.add_argument(
        "--ewma-span-minutes", "-s", # 新增短参数 -s
        type=int,
        default=30,
        help="用于平滑外推的指数加权移动平均 (EWMA) 时间跨度（分钟）。"
             "跨度越大，趋势越平滑。默认为30分钟。"
    )
    # audienceCount 衰减因子
    parser.add_argument(
        "--audience-decay-factor", "-a", # 新增短参数 -a
        type=float,
        default=0.95,
        help="audienceCount 在原始数据范围外进行估算时，每分钟的衰减因子。"
             "例如 0.95 表示每分钟减少 5%%。值应在 0 到 1 之间。默认为0.95。"
    )
    # audienceCount 衰减外推距离
    parser.add_argument(
        "--audience-extrapolation-minutes", "-x", # 新增短参数 -x
        type=int,
        default=5,
        help="audienceCount 允许进行**边界衰减外推**的最大时间距离（分钟）。"
             "此参数独立于 --max-extrapolation-minutes。默认为 5 分钟。"
    )

    args = parser.parse_args()

    conn = session_db.open_db(args.db)
    room_filter = (
        [r.strip() for r in args.rooms.split(",") if r.strip()]
        if args.rooms else None
    )

    if args.list_sessions:
        session_db.print_session_list(
            session_db.list_sessions(conn, rooms=room_filter)
        )
        sys.exit(0)

    sessions_pool = session_db.list_sessions(conn, rooms=room_filter)
    args.session_a = session_db.resolve_session(
        args.session, sessions_pool
    )["session"] if args.session else session_db.latest_session(
        sessions_pool
    )["session"]
    args.session_b = session_db.resolve_session(
        args.compare, sessions_pool
    )["session"] if args.compare else None

    DEBUG_MODE = args.debug

    # 只有进入比对模式才需要着色，单文件模式保持原样输出
    COLOR_ENABLED = bool(args.session_b) and resolve_color_mode(args.color)

    # 八分块模式细化柱状图刻度；ascii 为终端不支持方块字符时的回退
    block_style = args.bar_style == 'block'

    # 绘图后端：sixel 出二维图形，ascii 出字符条
    CHART_FORMAT = resolve_chart_format(args.chart_format)
    if args.chart_format == 'sixel' and sixel_plot is None:
        print("错误: 找不到 sixel_plot.py，无法渲染 Sixel 图形。"
              "请把它与本脚本放在同一目录，或改用 --chart-format ascii。",
              file=sys.stderr)
        sys.exit(1)

    chart_height = args.chart_height
    if chart_height is None:
        chart_height = (sixel_plot.DEFAULT_CHART_HEIGHT
                        if sixel_plot is not None else 0)
    if CHART_FORMAT == 'sixel':
        if chart_height % sixel_plot.BAND_HEIGHT or chart_height <= 0:
            print(f"错误: --chart-height 必须是 {sixel_plot.BAND_HEIGHT} 的正整数倍，"
                  f"当前为 {chart_height}。", file=sys.stderr)
            sys.exit(1)

    target_metrics = []
    if args.metrics:
        target_metrics = args.metrics
    else:
        target_metrics = ['totalUserCount', 'audienceCount']

    if not target_metrics:
        print("错误: 必须至少选择一个指标进行估算 (totalUserCount 或 audienceCount)。", file=sys.stderr)
        sys.exit(1)

    if not (0 <= args.audience_decay_factor <= 1):
        print("错误: --audience-decay-factor 必须在 0 到 1 之间。", file=sys.stderr)
        sys.exit(1)

    if args.session_b and args.session_a == args.session_b:
        print("警告: 两个场次相同，A/B 比对将得到完全相同的两列。", file=sys.stderr)

    compare_mode = bool(args.session_b)

    if DEBUG_MODE:
        print(f"DEBUG: 正在以调试模式处理场次: {args.session_a}", file=sys.stderr)
        if compare_mode:
            print(f"DEBUG: 比对场次: {args.session_b}", file=sys.stderr)
        print(f"DEBUG: 估算间隔: {args.interval} 分钟", file=sys.stderr)
        print(f"DEBUG: 目标估算指标: {target_metrics}", file=sys.stderr)
        print(f"DEBUG: 通用最大外推距离 (EWMA): {args.max_extrapolation_minutes} 分钟", file=sys.stderr)
        print(f"DEBUG: audienceCount 边界衰减最大外推距离: {args.audience_extrapolation_minutes} 分钟", file=sys.stderr)
        print(f"DEBUG: 估算值最大限制: {args.max_value}", file=sys.stderr)
        print(f"DEBUG: EWMA 平滑跨度: {args.ewma_span_minutes} 分钟", file=sys.stderr)
        print(f"DEBUG: audienceCount 衰减因子: {args.audience_decay_factor}", file=sys.stderr)
        print(f"DEBUG: 着色: {COLOR_ENABLED} (--color {args.color})", file=sys.stderr)
        print(f"DEBUG: 柱状图刻度风格: {args.bar_style}", file=sys.stderr)
        print(f"DEBUG: 绘图后端: {CHART_FORMAT} (--chart-format {args.chart_format})", file=sys.stderr)
        if CHART_FORMAT == 'sixel':
            print(f"DEBUG: Sixel 图高: {chart_height}px", file=sys.stderr)
    else:
        if compare_mode:
            print(f"正在比对场次:")
            print(f"  A: {args.session_a}")
            print(f"  B: {args.session_b}")
        else:
            print(f"正在处理场次: {args.session_a}")
        print(f"估算间隔: {args.interval} 分钟")
        print(f"目标估算指标: {target_metrics}")
        print(f"通用最大外推距离 (EWMA): {args.max_extrapolation_minutes} 分钟")
        print(f"audienceCount 边界衰减最大外推距离: {args.audience_extrapolation_minutes} 分钟")
        print(f"估算值最大限制: {args.max_value}")
        print(f"EWMA 平滑跨度: {args.ewma_span_minutes} 分钟")
        print(f"audienceCount 衰减因子: {args.audience_decay_factor}\n")

    # 待处理的场次列表: (标签, session id)。单场模式只有 A。
    runs = [('A', args.session_a)]
    if compare_mode:
        runs.append(('B', args.session_b))

    results = []

    for tag, session_id in runs:
        # 1. 提取原始数据
        raw_data = extract_session_data(conn, session_id)

        if not raw_data:
            if compare_mode:
                print(f"错误: 场次 '{session_id}' ({tag}) 未提取到任何符合条件的数据，无法比对。", file=sys.stderr)
            else:
                print("未从该场次提取到任何符合条件的数据，无法进行估算和可视化。")
            sys.exit(0)

        if DEBUG_MODE:
            print(f"DEBUG: [{tag}] 提取到 {len(raw_data)} 条原始数据。", file=sys.stderr)

        # 比对模式下平移到统一基准，确保两侧估算栅格相位一致、逐行可对齐
        estimate_input = raw_data
        session_start = None
        if compare_mode:
            estimate_input, session_start = shift_to_relative_epoch(raw_data)
            if DEBUG_MODE:
                print(f"DEBUG: [{tag}] 开播时刻 {session_start.strftime('%Y-%m-%d %H:%M:%S')}"
                      f" -> 平移至基准 {COMPARISON_EPOCH.strftime('%Y-%m-%d %H:%M:%S')}", file=sys.stderr)

        # 2. 估算数据
        interpolated_data = interpolate_data(
            estimate_input,
            args.interval,
            target_metrics,
            args.max_extrapolation_minutes,
            args.max_value,
            args.ewma_span_minutes,
            args.audience_decay_factor,
            args.audience_extrapolation_minutes
        )

        if not interpolated_data:
            print(f"无法为 {args.interval} 分钟间隔估算出任何有效数据"
                  f"{f' (场次 {tag}: {session_id})' if compare_mode else ''}，"
                  f"请检查数据或调整估算参数。")
            sys.exit(0)

        if DEBUG_MODE:
            print(f"DEBUG: [{tag}] 估算后生成 {len(interpolated_data)} 条数据。", file=sys.stderr)
            for entry in interpolated_data:
                print(f"DEBUG: [{tag}] 估算数据 - {entry}", file=sys.stderr)

        results.append((tag, session_id, session_start, interpolated_data))

    # 3. 结果可视化
    if not compare_mode:
        # 单文件模式：保持原有输出格式不变
        tag, sess_id, _, interpolated_data = results[0]

        print("\n--- 估算后的数据 ---")
        for entry in interpolated_data:
            ts_str = entry['timestamp'].strftime("%Y-%m-%d %H:%M:%S")
            output_parts = [f"时间点: {ts_str}"]
            for metric in target_metrics:
                if metric in entry:
                    display_name = METRIC_DISPLAY_NAMES.get(metric, metric)
                    output_parts.append(f"{display_name}: {entry[metric]}")

            print(", ".join(output_parts))

        for metric in target_metrics:
            display_title = METRIC_CHART_TITLES.get(metric, f"{metric} 趋势图")
            if CHART_FORMAT == 'sixel':
                print_sixel_bar_chart(interpolated_data, metric, display_title,
                                      chart_height=chart_height)
            else:
                print_ascii_bar_chart(interpolated_data, metric, display_title,
                                      block_style=block_style)
    else:
        # 比对模式：并排输出
        (_, path_a, start_a, data_a) = results[0]
        (_, path_b, start_b, data_b) = results[1]

        print(">>> 横轴为各自开播后的相对分钟 T+X（两场直播起播时刻不同，墙钟无法直接对齐）；")
        print(">>> 负数 T+ 表示起播前的外推区间；同一指标的 A/B 共用一把标尺。")
        print(f">>> A 开播时刻: {start_a.strftime('%Y-%m-%d %H:%M:%S')}   "
              f"B 开播时刻: {start_b.strftime('%Y-%m-%d %H:%M:%S')}")

        print_comparison_table(data_a, data_b, target_metrics)

        for metric in target_metrics:
            display_title = METRIC_CHART_TITLES.get(metric, f"{metric} 趋势图")
            if CHART_FORMAT == 'sixel':
                print_sixel_comparison_chart(data_a, data_b, metric, display_title,
                                             chart_height=chart_height,
                                             block_style=block_style)
            else:
                print_comparison_bar_chart(data_a, data_b, metric, display_title,
                                           block_style=block_style)
