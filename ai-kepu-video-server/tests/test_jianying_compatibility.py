#!/usr/bin/env python3
"""
剪映草稿兼容性测试工具

测试生成的 draft_content.json 和 draft_meta_info.json 是否符合剪映规范
"""

import json
import os
import sys
from pathlib import Path
from typing import Dict, List, Any, Tuple
from datetime import datetime


class JianyingCompatibilityTester:
    """剪映兼容性测试器"""

    def __init__(self, draft_dir: str):
        self.draft_dir = Path(draft_dir)
        self.draft_content_path = self.draft_dir / "draft_content.json"
        self.draft_meta_path = self.draft_dir / "draft_meta_info.json"
        self.errors = []
        self.warnings = []
        self.info = []

    def run_all_tests(self) -> bool:
        """运行所有测试"""
        print(f"\n{'='*60}")
        print(f"剪映兼容性测试")
        print(f"测试目录: {self.draft_dir}")
        print(f"{'='*60}\n")

        # 1. 文件存在性检查
        if not self._test_files_exist():
            return False

        # 2. JSON 格式验证
        draft_content = self._test_json_format()
        if not draft_content:
            return False

        # 后续检查尽量全部执行，一次给出完整修复清单。单项失败由
        # self.errors 汇总，避免第一个错误掩盖后续兼容性问题。
        self._test_required_fields(draft_content)
        self._test_track_structure(draft_content)
        self._test_timeline_continuity(draft_content)
        self._test_material_references(draft_content)
        self._test_file_paths(draft_content)
        self._test_duration_consistency(draft_content)

        # 输出测试结果
        self._print_results()

        return len(self.errors) == 0

    def _test_files_exist(self) -> bool:
        """测试文件是否存在"""
        print("📁 [1/8] 检查文件存在性...")

        if not self.draft_content_path.exists():
            self.errors.append(f"缺少 draft_content.json: {self.draft_content_path}")
            return False

        if not self.draft_meta_path.exists():
            self.warnings.append(f"缺少 draft_meta_info.json: {self.draft_meta_path}")

        self.info.append("✓ draft_content.json 存在")
        if self.draft_meta_path.exists():
            self.info.append("✓ draft_meta_info.json 存在")

        return True

    def _test_json_format(self) -> Dict[str, Any]:
        """测试 JSON 格式是否有效"""
        print("📝 [2/8] 验证 JSON 格式...")

        try:
            with open(self.draft_content_path, 'r', encoding='utf-8') as f:
                draft_content = json.load(f)
            self.info.append("✓ JSON 格式有效")
            return draft_content
        except json.JSONDecodeError as e:
            self.errors.append(f"JSON 格式错误: {e}")
            return None
        except Exception as e:
            self.errors.append(f"读取文件失败: {e}")
            return None

    def _test_required_fields(self, draft: Dict[str, Any]) -> bool:
        """测试必需字段"""
        print("🔍 [3/8] 检查必需字段...")

        required_fields = [
            'id', 'duration', 'fps', 'materials', 'tracks',
            'canvas_config', 'platform'
        ]

        missing_fields = []
        for field in required_fields:
            if field not in draft:
                missing_fields.append(field)

        if missing_fields:
            self.errors.append(f"缺少必需字段: {', '.join(missing_fields)}")
            return False

        self.info.append(f"✓ 所有必需字段存在 ({len(required_fields)} 个)")
        return True

    def _test_track_structure(self, draft: Dict[str, Any]) -> bool:
        """测试轨道结构"""
        print("🎬 [4/8] 检查轨道结构...")

        tracks = draft.get('tracks', [])
        if not tracks:
            self.errors.append("没有轨道数据")
            return False

        track_types = {}
        for track in tracks:
            track_type = track.get('type', 'unknown')
            track_types[track_type] = track_types.get(track_type, 0) + 1

            # 检查轨道必需字段
            if 'segments' not in track:
                self.errors.append(f"轨道 {track_type} 缺少 segments 字段")
                return False

        # 至少需要一个视频轨
        if 'video' not in track_types:
            self.errors.append("缺少视频轨道")
            return False

        video_segments = [
            segment
            for track in tracks if track.get('type') == 'video'
            for segment in track.get('segments', [])
        ]
        if not video_segments:
            self.errors.append("视频轨道为空")
            return False

        segment_counts = {
            track_type: sum(
                len(track.get('segments', []))
                for track in tracks if track.get('type') == track_type
            )
            for track_type in ('video', 'audio', 'text')
        }
        for track_type in ('audio', 'text'):
            count = segment_counts[track_type]
            if count and count != segment_counts['video']:
                self.errors.append(
                    f"视频轨与{track_type}轨分镜数量不一致: "
                    f"{segment_counts['video']} != {count}"
                )

        self.info.append(f"✓ 轨道结构正确: {dict(track_types)}")
        return True

    def _test_timeline_continuity(self, draft: Dict[str, Any]) -> bool:
        """测试时间轴连续性"""
        print("⏱️  [5/8] 检查时间轴连续性...")

        tracks = draft.get('tracks', [])
        has_gaps = False
        has_overlaps = False

        for track in tracks:
            track_type = track.get('type', 'unknown')
            segments = track.get('segments', [])

            if not segments:
                continue

            # 按开始时间排序
            sorted_segments = sorted(
                segments,
                key=lambda s: s.get('target_timerange', {}).get('start', 0)
            )

            # 检查连续性
            for i in range(len(sorted_segments) - 1):
                current = sorted_segments[i].get('target_timerange', {})
                next_seg = sorted_segments[i + 1].get('target_timerange', {})

                current_end = current.get('start', 0) + current.get('duration', 0)
                next_start = next_seg.get('start', 0)

                gap = next_start - current_end

                tolerance = self._frame_tolerance(draft)
                if gap > tolerance:
                    has_gaps = True
                    self.errors.append(
                        f"轨道 {track_type} 存在间隙: {gap/1000000:.3f}秒 "
                        f"(片段 {i} -> {i+1})"
                    )
                elif gap < -tolerance:
                    has_overlaps = True
                    self.errors.append(
                        f"轨道 {track_type} 存在重叠: {-gap/1000000:.3f}秒 "
                        f"(片段 {i} -> {i+1})"
                    )

        if not has_gaps and not has_overlaps:
            self.info.append("✓ 时间轴连续，无间隙或重叠")
        elif has_gaps and not has_overlaps:
            self.info.append("⚠ 时间轴有间隙（可能是有意设计）")

        return not has_gaps and not has_overlaps

    @staticmethod
    def _frame_tolerance(draft: Dict[str, Any]) -> int:
        fps = draft.get('fps') or 30
        try:
            fps = max(1.0, float(fps))
        except (TypeError, ValueError):
            fps = 30.0
        return int(round(1_000_000 / fps))

    def _test_material_references(self, draft: Dict[str, Any]) -> bool:
        """测试素材引用完整性"""
        print("🎨 [6/8] 检查素材引用...")

        materials = draft.get('materials', {})

        # 收集所有素材 ID
        material_ids = set()
        for material_type in ['videos', 'audios', 'images', 'texts']:
            items = materials.get(material_type, [])
            for item in items:
                material_id = item.get('id')
                if material_id:
                    material_ids.add(material_id)

        # 收集轨道中引用的素材 ID
        referenced_ids = set()
        tracks = draft.get('tracks', [])
        for track in tracks:
            segments = track.get('segments', [])
            for segment in segments:
                material_id = segment.get('material_id')
                if material_id:
                    referenced_ids.add(material_id)

        # 检查未引用的素材
        unused_materials = material_ids - referenced_ids
        if unused_materials:
            self.warnings.append(
                f"有 {len(unused_materials)} 个素材未被引用"
            )

        # 检查引用了不存在的素材
        missing_materials = referenced_ids - material_ids
        if missing_materials:
            self.errors.append(
                f"引用了 {len(missing_materials)} 个不存在的素材: "
                f"{list(missing_materials)[:3]}"
            )
            return False

        self.info.append(
            f"✓ 素材引用正确: {len(material_ids)} 个素材, "
            f"{len(referenced_ids)} 个被引用"
        )
        return True

    def _test_file_paths(self, draft: Dict[str, Any]) -> bool:
        """测试文件路径是否存在"""
        print("📂 [7/8] 检查文件路径...")

        materials = draft.get('materials', {})
        missing_files = []
        total_files = 0

        for material_type in ['videos', 'audios', 'images']:
            items = materials.get(material_type, [])
            for item in items:
                path = item.get('path', '')
                if path:
                    total_files += 1
                    full_path = self.draft_dir / path
                    if not full_path.exists():
                        missing_files.append(str(path))

        if missing_files:
            self.errors.append(
                f"有 {len(missing_files)} 个文件不存在: "
                f"{missing_files[:3]}"
            )
            return False

        self.info.append(f"✓ 所有文件路径有效 ({total_files} 个文件)")
        return True

    def _test_duration_consistency(self, draft: Dict[str, Any]) -> bool:
        """测试时长一致性"""
        print("⏲️  [8/8] 检查时长一致性...")

        draft_duration = draft.get('duration', 0)

        # 计算视频轨最大时长
        tracks = draft.get('tracks', [])
        max_track_duration = 0

        for track in tracks:
            if track.get('type') == 'video':
                segments = track.get('segments', [])
                for segment in segments:
                    timerange = segment.get('target_timerange', {})
                    end_time = timerange.get('start', 0) + timerange.get('duration', 0)
                    max_track_duration = max(max_track_duration, end_time)

        # 允许 1 帧的误差 (1/30 秒 = 33333 微秒)
        tolerance = self._frame_tolerance(draft)
        diff = abs(draft_duration - max_track_duration)

        if diff > tolerance:
            self.errors.append(
                f"草稿时长 ({draft_duration/1000000:.3f}s) 与轨道时长 "
                f"({max_track_duration/1000000:.3f}s) 不一致，差异 "
                f"{diff/1000000:.3f}s"
            )

        self.info.append(
            f"✓ 时长: {draft_duration/1000000:.2f}秒 "
            f"(轨道: {max_track_duration/1000000:.2f}秒)"
        )
        return diff <= tolerance

    def _print_results(self):
        """打印测试结果"""
        print(f"\n{'='*60}")
        print("测试结果汇总")
        print(f"{'='*60}\n")

        # 打印信息
        if self.info:
            print("✅ 通过的检查:")
            for msg in self.info:
                print(f"  {msg}")
            print()

        # 打印警告
        if self.warnings:
            print("⚠️  警告 (不影响导入):")
            for msg in self.warnings:
                print(f"  {msg}")
            print()

        # 打印错误
        if self.errors:
            print("❌ 错误 (可能导致导入失败):")
            for msg in self.errors:
                print(f"  {msg}")
            print()

        # 总结
        if not self.errors:
            print("🎉 所有关键测试通过！草稿应该可以在剪映中正常打开。")
        else:
            print(f"💥 发现 {len(self.errors)} 个错误，需要修复后才能导入剪映。")

        print(f"\n{'='*60}\n")


def main():
    """主函数"""
    if len(sys.argv) < 2:
        print("用法: python test_jianying_compatibility.py <草稿目录>")
        print("\n示例:")
        print("  python test_jianying_compatibility.py output/测试项目")
        print("  python test_jianying_compatibility.py output/行行出状元")
        sys.exit(1)

    draft_dir = sys.argv[1]

    if not os.path.exists(draft_dir):
        print(f"❌ 目录不存在: {draft_dir}")
        sys.exit(1)

    tester = JianyingCompatibilityTester(draft_dir)
    success = tester.run_all_tests()

    sys.exit(0 if success else 1)


if __name__ == "__main__":
    main()
