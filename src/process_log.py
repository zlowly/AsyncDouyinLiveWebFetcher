import argparse
import json
import re
import sys
import csv

def process_log_file(log_file_path, skip_lines=0, output_format="tsv"):
    """
    处理日志文件，提取指定类型的消息，并以指定格式输出。

    Args:
        log_file_path (str): 日志文件的路径。
        skip_lines (int): 从文件开头跳过的行数。默认为 0。
        output_format (str): 输出格式，可选 "tsv" 或 "json"。默认为 "tsv"。
    """
    # 匹配 JSON 数据的正则表达式
    log_pattern = re.compile(r'({.*})')

    extracted_messages = []

    try:
        with open(log_file_path, 'r', encoding='utf-8') as f:
            for i, line in enumerate(f):
                if i < skip_lines:
                    continue

                match = log_pattern.search(line)
                if not match:
                    continue

                json_data_str = match.group(1)

                try:
                    data = json.loads(json_data_str)
                    timestamp_str = data.get('timestamp')
                    message_str = data.get('message')

                    if not message_str:
                        continue
                    
                    try:
                        message_data = json.loads(message_str.replace("'", '"'))
                    except json.JSONDecodeError:
                        continue

                    items_to_process = []
                    if isinstance(message_data, list):
                        items_to_process.extend(message_data)
                    elif isinstance(message_data, dict):
                        items_to_process.append(message_data)

                    for item in items_to_process:
                        user_name = item.get('userName')
                        content_text = None

                        method = item.get('method')
                        if method == 'WebcastChatMessage':
                            content_text = item.get('content')
                        elif method == 'WebcastGiftMessage':
                            gift_name = item.get('giftName')
                            gift_count = item.get('giftCount')
                            if gift_name and gift_count is not None:
                                content_text = f"送出 {gift_name}x{gift_count}"
                            elif gift_name:
                                content_text = f"送出 {gift_name}"
                        # 新增对 WebcastMemberMessage 的处理
                        elif method == 'WebcastMemberMessage':
                            continue
                            # 为进场消息创建一个描述性的内容，有需要可以注释掉上面的continue加入下面语句
                            # content_text = "进入直播间"

                        if user_name and content_text is not None:
                            message = {
                                "timestamp": timestamp_str,
                                "speaker": user_name,
                                "content": content_text
                            }
                            extracted_messages.append(message)

                except json.JSONDecodeError as e:
                    print(f"错误: 解析行 {i+1} 的 JSON 失败: {e} - 内容: {json_data_str}", file=sys.stderr)
                except Exception as e:
                    print(f"错误: 处理行 {i+1} 时发生未知错误: {e} - 内容: {json_data_str}", file=sys.stderr)

        if output_format == "json":
            print(json.dumps(extracted_messages, indent=2, ensure_ascii=False))
        elif output_format == "tsv":
            writer = csv.writer(sys.stdout, delimiter='\t', lineterminator='\n')
            if extracted_messages:
                writer.writerow(["timestamp", "name", "content"])
            for msg in extracted_messages:
                writer.writerow([msg.get("timestamp", ""), msg.get("name", ""), msg.get("content", "")])
        else:
            print(f"错误: 不支持的输出格式 '{output_format}'。可选 'tsv' 或 'json'。", file=sys.stderr)
            sys.exit(1)

    except FileNotFoundError:
        print(f"错误: 文件 '{log_file_path}' 未找到。", file=sys.stderr)
        sys.exit(1)
    except Exception as e:
        print(f"发生意外错误: {e}", file=sys.stderr)
        sys.exit(1)

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="从带时间戳的日志文件中提取特定的聊天和礼物消息。")
    parser.add_argument(
        "-f", "--file",
        dest="log_file_path",
        required=True,
        help="要处理的日志文件路径。"
    )
    parser.add_argument(
        "-s", "--skip",
        dest="skip_lines",
        type=int,
        default=0,
        help="从文件开头跳过的行数 (默认为 0)。"
    )
    parser.add_argument(
        "-o", "--output-format",
        dest="output_format",
        choices=["tsv", "json"],
        default="tsv",
        help="输出格式，可选 'tsv' 或 'json' (默认为 'tsv')。"
    )

    args = parser.parse_args()

    process_log_file(args.log_file_path, args.skip_lines, args.output_format)
