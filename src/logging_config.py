import logging
import json
import sys

class JsonFormatter(logging.Formatter):
    def format(self, record):
        # 将 record 转换为字典
        log_record = {
            "timestamp": self.formatTime(record, self.datefmt),
            "level": record.levelname,
            "name": record.name,
            "message": record.getMessage()
        }
        # 如果日志消息本身就是字典，则合并
        try:
            msg_dict = json.loads(record.getMessage())
            log_record.update(msg_dict)
        except (json.JSONDecodeError, TypeError):
            pass

        return json.dumps(log_record, ensure_ascii=False)

def setup_logging(log_file_suffix: str):
    """
    配置应用的日志系统，包括输出到文件和控制台。
    这个函数只应该在应用启动时调用一次。
    """
    # 获取根日志记录器，并设置最低日志级别
    root_logger = logging.getLogger()
    root_logger.setLevel(logging.DEBUG)

    # 创建文件处理器，级别为 DEBUG
    file_handler = logging.FileHandler(f"app-{log_file_suffix}.log")
    file_handler.setLevel(logging.DEBUG)
    
    # 创建控制台处理器，级别为 INFO
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setLevel(logging.INFO)

    # 定义日志格式
    formatter = logging.Formatter(
        "%(asctime)s - %(name)s - %(levelname)s - %(message)s"
    )

    # 设置处理器的格式
    console_handler.setFormatter(formatter)
    file_handler.setFormatter(JsonFormatter())

    # 添加处理器到根记录器
    root_logger.addHandler(file_handler)
    root_logger.addHandler(console_handler)

    # 配置特殊日志系统
    # 创建一个名为 'stat_logger' 的新记录器
    stat_file_handler = logging.FileHandler(f"stats-{log_file_suffix}.log")
    stat_file_handler.setLevel(logging.INFO) # 可以设置独立的级别
    stat_file_handler.setFormatter(JsonFormatter())
    stat_logger = logging.getLogger("stat_logger")
    stat_logger.setLevel(logging.INFO)  # 设置该记录器的最低级别
    stat_logger.propagate = False       # !!! 阻止日志传播到父记录器
    stat_logger.addHandler(stat_file_handler)

    logging.info("Logging system configured successfully.")
