"""
未实现的旧批处理占位模块。可用的批量预案入口为 main.py batch / Web 批量预案。
"""

import logging
from typing import List
from pathlib import Path

logger = logging.getLogger(__name__)


class BatchProcessor:
    """批量处理器 - 批量处理多个主题"""
    
    def __init__(self, themes: List[str], output_dir: str = "output"):
        """
        初始化批量处理器
        
        Args:
            themes: 主题列表
            output_dir: 输出目录
        """
        self.themes = themes
        self.output_dir = Path(output_dir)
        self.output_dir.mkdir(parents=True, exist_ok=True)
        
    def run(self):
        """运行批量处理"""
        raise NotImplementedError("旧批处理占位模块未实现，请使用 main.py batch 或 Web 批量预案")


if __name__ == "__main__":
    # 测试代码
    themes = ["人工智能", "机器学习", "深度学习"]
    processor = BatchProcessor(themes)
    
    # processor.run()
