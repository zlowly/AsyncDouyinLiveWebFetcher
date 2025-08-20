import json
import re
import sys
import argparse
from datetime import datetime, timedelta
import collections

# 全局变量，用于控制调试模式是否开启
DEBUG_MODE = False

def extract_log_data(log_file_path):
    """
    处理日志文件，提取时间戳、totalUserCount 和 audienceCount。
    如果 totalUserCount 和 audienceCount 都不存在，则不将该行包含在结果中。
    如果 DEBUG_MODE 为 True，将打印额外的调试信息。

    Args:
        log_file_path (str): 日志文件的路径。

    Returns:
        list: 包含字典的列表，每个字典代表一个处理过的日志条目。
              每个字典可能包含 'timestamp' (datetime对象), 'totalUserCount' (int), 'audienceCount' (int) 键。
    """
    results = []
    
    # 新的正则表达式，匹配新的日志格式
    # 注意：这里我们匹配的是整个 JSON 对象，而不是时间戳和 JSON 字符串分开
    # 这是因为整个行现在都是一个合法的 JSON 对象
    log_line_regex = re.compile(r'^{.*}$')

    try:
        with open(log_file_path, 'r', encoding='utf-8') as f:
            for line_num, line in enumerate(f, 1):
                line = line.strip()

                if not log_line_regex.match(line):
                    if DEBUG_MODE:
                        print(f"DEBUG: 行 {line_num} - 行格式不匹配预期模式。跳过。行内容: '{line}'", file=sys.stderr)
                    continue
                
                try:
                    # 第一次解析: 解析整行 JSON
                    full_log_entry = json.loads(line)
                    
                    # 提取时间和消息
                    timestamp_str = full_log_entry.get('timestamp')
                    message_str = full_log_entry.get('message')

                    if not timestamp_str or not message_str:
                        if DEBUG_MODE:
                            print(f"DEBUG: 行 {line_num} - 缺少 'timestamp' 或 'message' 字段。跳过。", file=sys.stderr)
                        continue

                    # 第二次解析: 解析 message 字段中的 JSON 字符串
                    # message_str 的值是一个字符串，需要先替换单引号为双引号才能正确解析
                    message_str = message_str.replace("'", '"')
                    data = json.loads(message_str)

                    found_counts = {}
                    
                    def find_counts_recursively(item):
                        if 'totalUserCount' in found_counts and 'audienceCount' in found_counts:
                            return
                            
                        if isinstance(item, dict):
                            if "totalUserCount" in item and 'totalUserCount' not in found_counts:
                                try:
                                    found_counts['totalUserCount'] = int(item["totalUserCount"])
                                except (ValueError, TypeError):
                                    if DEBUG_MODE:
                                        print(f"DEBUG: 行 {line_num} - totalUserCount '{item.get('totalUserCount')}' 不是有效数字。跳过此值。", file=sys.stderr)
                            if "audienceCount" in item and 'audienceCount' not in found_counts:
                                try:
                                    found_counts['audienceCount'] = int(item["audienceCount"])
                                except (ValueError, TypeError):
                                    if DEBUG_MODE:
                                        print(f"DEBUG: 行 {line_num} - audienceCount '{item.get('audienceCount')}' 不是有效数字。跳过此值。", file=sys.stderr)
                            
                            for key, value in item.items():
                                find_counts_recursively(value)
                        elif isinstance(item, list):
                            for element in item:
                                find_counts_recursively(element)

                    find_counts_recursively(data)

                    if 'totalUserCount' in found_counts or 'audienceCount' in found_counts:
                        # 解析时间戳，注意加上 '%f' 来处理毫秒
                        dt_object = datetime.strptime(timestamp_str, "%Y-%m-%d %H:%M:%S,%f")
                        
                        entry = {'timestamp': dt_object}
                        if 'totalUserCount' in found_counts:
                            entry['totalUserCount'] = found_counts['totalUserCount']
                        if 'audienceCount' in found_counts:
                            entry['audienceCount'] = found_counts['audienceCount']
                        results.append(entry)

                except json.JSONDecodeError as e:
                    if DEBUG_MODE:
                        print(f"DEBUG: 行 {line_num} - JSON 解析失败: {e}。原始 JSON 字符串 (前100字符): '{line[:100]}...'。跳过。", file=sys.stderr)
                    pass
                except ValueError as e:
                     if DEBUG_MODE:
                        print(f"DEBUG: 行 {line_num} - 时间戳格式不正确: {e}。原始时间戳: '{timestamp_str}'。跳过。", file=sys.stderr)
                     pass
                except Exception as e:
                    if DEBUG_MODE:
                        print(f"DEBUG: 行 {line_num} - 处理日志时发生意外错误: {e}。跳过。行内容: '{line}'", file=sys.stderr)
                    pass

    except FileNotFoundError:
        print(f"错误: 日志文件 '{log_file_path}' 未找到。请检查路径。", file=sys.stderr)
        sys.exit(1)
    except Exception as e:
        print(f"发生意外错误: {e}", file=sys.stderr)
        sys.exit(1)

    results.sort(key=lambda x: x['timestamp'])
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

def print_ascii_bar_chart(data, value_key, chart_title, bar_char='#', max_width=50):
    """
    在控制台打印 ASCII 柱状图。

    Args:
        data (list): 包含字典的列表，每个字典至少包含 'timestamp' 和 value_key。
        value_key (str): 要绘制柱状图的数值键名。
        chart_title (str): 图表的标题。
        bar_char (str): 用于构建柱子的字符。
        max_width (int): 柱状图的最大宽度（字符数）。
    """
    chart_data = [entry for entry in data if value_key in entry and entry[value_key] is not None]

    if not chart_data:
        print(f"\n{chart_title}: 无有效数据可绘制。")
        return

    values = [entry[value_key] for entry in chart_data]
    if not values:
        print(f"\n{chart_title}: '{value_key}' 字段无有效数据可绘制。")
        return

    max_value = max(values)
    if max_value == 0:
        print(f"\n{chart_title}: 所有 '{value_key}' 值为零，无法绘制有意义的图表。")
        return

    print(f"\n--- {chart_title} ---")
    print(f"最大值: {max_value}")
    
    max_ts_len = max(len(entry['timestamp'].strftime("%Y-%m-%d %H:%M")) for entry in chart_data)

    for entry in chart_data:
        ts_str = entry['timestamp'].strftime("%Y-%m-%d %H:%M")
        value = entry.get(value_key)

        bar_length = int((value / max_value) * max_width)
        bar = bar_char * bar_length
        print(f"{ts_str.ljust(max_ts_len)} | {bar} {value}")

# --- 主执行块 ---
if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="从日志文件中提取、估算并可视化 totalUserCount 和 audienceCount。",
        formatter_class=argparse.RawTextHelpFormatter
    )
    # 日志文件路径参数没有短参数，因为它是一个位置参数
    parser.add_argument(
        "log_file_path",
        help="要处理的日志文件路径。"
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
        default=60,
        help="允许进行**除 audienceCount 以外的通用 EWMA 外推**的最大时间距离（分钟）。"
             "超出此距离则不外推。默认为60分钟。"
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

    DEBUG_MODE = args.debug

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


    if DEBUG_MODE:
        print(f"DEBUG: 正在以调试模式处理日志文件: {args.log_file_path}", file=sys.stderr)
        print(f"DEBUG: 估算间隔: {args.interval} 分钟", file=sys.stderr)
        print(f"DEBUG: 目标估算指标: {target_metrics}", file=sys.stderr)
        print(f"DEBUG: 通用最大外推距离 (EWMA): {args.max_extrapolation_minutes} 分钟", file=sys.stderr)
        print(f"DEBUG: audienceCount 边界衰减最大外推距离: {args.audience_extrapolation_minutes} 分钟", file=sys.stderr)
        print(f"DEBUG: 估算值最大限制: {args.max_value}", file=sys.stderr)
        print(f"DEBUG: EWMA 平滑跨度: {args.ewma_span_minutes} 分钟", file=sys.stderr)
        print(f"DEBUG: audienceCount 衰减因子: {args.audience_decay_factor}\n", file=sys.stderr)
    else:
        print(f"正在处理日志文件: {args.log_file_path}")
        print(f"估算间隔: {args.interval} 分钟")
        print(f"目标估算指标: {target_metrics}")
        print(f"通用最大外推距离 (EWMA): {args.max_extrapolation_minutes} 分钟")
        print(f"audienceCount 边界衰减最大外推距离: {args.audience_extrapolation_minutes} 分钟")
        print(f"估算值最大限制: {args.max_value}")
        print(f"EWMA 平滑跨度: {args.ewma_span_minutes} 分钟")
        print(f"audienceCount 衰减因子: {args.audience_decay_factor}\n")


    # 1. 提取原始数据
    raw_data = extract_log_data(args.log_file_path)

    if not raw_data:
        print("未从日志文件中提取到任何符合条件的数据，无法进行估算和可视化。")
        sys.exit(0)

    if DEBUG_MODE:
        print(f"DEBUG: 提取到 {len(raw_data)} 条原始数据。", file=sys.stderr)


    # 2. 估算数据
    interpolated_data = interpolate_data(
        raw_data, 
        args.interval, 
        target_metrics,
        args.max_extrapolation_minutes,
        args.max_value,
        args.ewma_span_minutes,
        args.audience_decay_factor,
        args.audience_extrapolation_minutes
    )

    if not interpolated_data:
        print(f"无法为 {args.interval} 分钟间隔估算出任何有效数据，请检查日志数据或调整估算参数。")
        sys.exit(0)

    if DEBUG_MODE:
        print(f"\nDEBUG: 估算后生成 {len(interpolated_data)} 条数据。", file=sys.stderr)
        for entry in interpolated_data:
            print(f"DEBUG: 估算数据 - {entry}", file=sys.stderr)


    # 3. 结果可视化
    print("\n--- 估算后的数据 ---")
    for entry in interpolated_data:
        ts_str = entry['timestamp'].strftime("%Y-%m-%d %H:%M:%S")
        output_parts = [f"时间点: {ts_str}"]
        for metric in target_metrics:
            if metric in entry:
                display_name = {
                    'totalUserCount': '用户总数',
                    'audienceCount': '观众计数'
                }.get(metric, metric)
                output_parts.append(f"{display_name}: {entry[metric]}")
        
        print(", ".join(output_parts))

    for metric in target_metrics:
        display_title = {
            'totalUserCount': 'Total User Count 趋势图',
            'audienceCount': 'Audience Count 趋势图'
        }.get(metric, f"{metric} 趋势图")
        print_ascii_bar_chart(interpolated_data, metric, display_title)
