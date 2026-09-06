#!/usr/bin/env python3
"""
数据恢复脚本
用于恢复因数据库错误而未保存段落数据的失败任务
"""

import sys
import os
import re
import sqlite3
import uuid
import wave
from pathlib import Path
from datetime import datetime


def get_audio_duration(audio_path: str) -> float:
    """获取音频时长（秒）"""
    try:
        with wave.open(audio_path, 'rb') as audio_file:
            frames = audio_file.getnframes()
            rate = audio_file.getframerate()
            duration = frames / float(rate)
            return round(duration, 2)
    except Exception as e:
        # 如果 wave 失败，尝试使用 mutagen
        try:
            from mutagen.wave import WAVE
            audio = WAVE(audio_path)
            return round(audio.info.length, 2)
        except:
            return None


def parse_task_log(task_id: str, log_file: Path) -> dict:
    """从日志文件中提取任务信息"""
    segments = []
    current_segment = None

    with open(log_file, 'r', encoding='utf-8') as f:
        for line in f:
            if task_id not in line:
                continue

            # 提取段落文本
            text_match = re.search(r'段落 (\d+) 文案: (.+)', line)
            if text_match:
                index = int(text_match.group(1))
                text = text_match.group(2).strip()
                if index >= len(segments):
                    segments.append({'index': index, 'text': text})
                else:
                    segments[index]['text'] = text

            # 提取图像 prompt
            prompt_match = re.search(r'段落 (\d+) 图像描述: (.+)', line)
            if prompt_match:
                index = int(prompt_match.group(1))
                prompt = prompt_match.group(2).strip()
                if index >= len(segments):
                    segments.append({'index': index, 'image_prompt': prompt})
                else:
                    segments[index]['image_prompt'] = prompt

    return {'segments': segments}


def scan_output_directory(output_dir: Path) -> dict:
    """扫描输出目录，获取已生成的文件"""
    files = {
        'images': [],
        'audios': []
    }

    # 扫描图片
    images_dir = output_dir / 'images'
    if images_dir.exists():
        for img_file in sorted(p for p in images_dir.iterdir() if p.suffix.lower() in {'.png', '.jpg', '.jpeg', '.webp'}):
            files['images'].append(img_file)

    # 扫描音频
    voiceovers_dir = output_dir / 'voiceovers'
    if voiceovers_dir.exists():
        for audio_file in sorted(voiceovers_dir.glob('*.wav')):
            files['audios'].append(audio_file)

    return files


def recover_task_data(task_id: str, db_path: Path, output_base: Path, log_file: Path):
    """恢复任务数据"""

    # 1. 从数据库获取任务信息
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()

    cursor.execute("SELECT * FROM tasks WHERE task_id=?", (task_id,))
    task = cursor.fetchone()

    if not task:
        print(f"❌ 任务不存在: {task_id}")
        conn.close()
        return False

    task_name = task['name'] or task['theme'][:20]
    print(f"📋 任务名称: {task_name}")
    print(f"📋 任务状态: {task['status']}")

    # 2. 查找输出目录
    output_dir = output_base / task_name
    owned = output_base / task_id
    if owned.is_dir():
        candidates = [p for p in owned.iterdir() if p.is_dir() and not p.name.startswith(".")]
        if len(candidates) == 1:
            output_dir = candidates[0]
    if not output_dir.exists():
        print(f"❌ 输出目录不存在: {output_dir}")
        conn.close()
        return False

    print(f"📁 输出目录: {output_dir}")

    # 3. 扫描已生成的文件
    files = scan_output_directory(output_dir)
    print(f"🖼️  找到图片: {len(files['images'])} 个")
    print(f"🎵 找到音频: {len(files['audios'])} 个")

    if not files['images'] and not files['audios']:
        print("❌ 没有找到任何资源文件")
        conn.close()
        return False

    # 4. 从日志提取段落信息
    log_data = parse_task_log(task_id, log_file)
    segments_from_log = log_data.get('segments', [])
    print(f"📝 从日志提取段落: {len(segments_from_log)} 个")

    # 5. 合并数据：以文件为准，日志为辅
    def by_segment(paths):
        grouped = {}
        for fallback, path in enumerate(paths):
            match = re.search(r"(?:segment|seg|audio|voiceover|voice)_(\d+)", path.stem)
            index = int(match.group(1)) if match else fallback
            if index not in grouped or path.stat().st_mtime_ns > grouped[index].stat().st_mtime_ns:
                grouped[index] = path
        return grouped
    images_by_segment = by_segment(files['images'])
    audio_by_segment = by_segment(files['audios'])
    segments_data = []
    relative_dir = output_dir.relative_to(output_base).as_posix()

    for i in sorted(set(images_by_segment) | set(audio_by_segment)):

        # 获取日志中的文本和 prompt
        log_segment = next((s for s in segments_from_log if s['index'] == i), {})
        text = log_segment.get('text', f'段落 {i + 1}')
        image_prompt = log_segment.get('image_prompt', '')

        # 图片信息
        image_path = str(images_by_segment[i]) if i in images_by_segment else None
        image_url = f"/media/{relative_dir}/images/{images_by_segment[i].name}" if image_path else None

        # 音频信息
        audio_path = str(audio_by_segment[i]) if i in audio_by_segment else None
        audio_url = f"/media/{relative_dir}/voiceovers/{audio_by_segment[i].name}" if audio_path else None

        # 计算音频时长
        duration = None
        if audio_path:
            try:
                duration = get_audio_duration(audio_path)
            except Exception as e:
                print(f"⚠️  无法获取音频时长 (段落 {i}): {e}")

        segments_data.append({
            'segment_index': i,
            'text': text,
            'image_prompt': image_prompt,
            'image_path': image_path,
            'image_url': image_url,
            'audio_path': audio_path,
            'audio_url': audio_url,
            'duration': duration,
        })

    # 6. 保存到数据库
    print(f"\n💾 开始保存数据...")

    cursor.execute("BEGIN IMMEDIATE")
    # 6.1 保存段落数据
    for seg in segments_data:
        cursor.execute("""
            INSERT INTO task_segments
            (task_id, segment_index, text, image_prompt, image_path, image_url, audio_path, audio_url, duration)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(task_id,segment_index) DO UPDATE SET
                text=COALESCE(NULLIF(task_segments.text,''),excluded.text),
                image_prompt=COALESCE(NULLIF(task_segments.image_prompt,''),excluded.image_prompt),
                image_path=COALESCE(NULLIF(task_segments.image_path,''),excluded.image_path),
                image_url=COALESCE(NULLIF(task_segments.image_url,''),excluded.image_url),
                audio_path=COALESCE(NULLIF(task_segments.audio_path,''),excluded.audio_path),
                audio_url=COALESCE(NULLIF(task_segments.audio_url,''),excluded.audio_url),
                duration=COALESCE(task_segments.duration,excluded.duration)
        """, (
            task_id,
            seg['segment_index'],
            seg['text'],
            seg['image_prompt'],
            seg['image_path'],
            seg['image_url'],
            seg['audio_path'],
            seg['audio_url'],
            seg['duration'],
        ))

    print(f"✅ 保存段落数据: {len(segments_data)} 条")

    # 6.2 保存资产数据
    asset_count = 0
    for seg in segments_data:
        i = seg['segment_index']

        # 保存图片资产
        if seg['image_path'] and not cursor.execute("SELECT 1 FROM task_assets WHERE task_id=? AND segment_index=? AND asset_type='image' AND path=?", (task_id,i,seg['image_path'])).fetchone():
            asset_id = uuid.uuid4().hex
            cursor.execute("""
                INSERT OR IGNORE INTO task_assets
                (asset_id, task_id, segment_index, asset_type, source, path, url, label, prompt, text, status)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                asset_id,
                task_id,
                i,
                'image',
                'generated',
                seg['image_path'],
                seg['image_url'],
                f"AI 生成 · 分镜 {i + 1}",
                seg['image_prompt'],
                seg['text'],
                'completed',
            ))
            asset_count += 1

        # 保存音频资产
        if seg['audio_path'] and not cursor.execute("SELECT 1 FROM task_assets WHERE task_id=? AND segment_index=? AND asset_type='audio' AND path=?", (task_id,i,seg['audio_path'])).fetchone():
            asset_id = uuid.uuid4().hex
            cursor.execute("""
                INSERT OR IGNORE INTO task_assets
                (asset_id, task_id, segment_index, asset_type, source, path, url, label, text, voice_type, status)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                asset_id,
                task_id,
                i,
                'audio',
                'generated',
                seg['audio_path'],
                seg['audio_url'],
                f"配音 · 分镜 {i + 1}",
                seg['text'],
                task['voice_type'],
                'completed',
            ))
            asset_count += 1

    print(f"✅ 保存资产数据: {asset_count} 条")

    conn.commit()
    conn.close()

    print(f"\n🎉 数据恢复完成！")
    print(f"   - 段落: {len(segments_data)} 个")
    print(f"   - 资产: {asset_count} 个")
    print(f"\n💡 现在可以在前端编辑页面查看该项目了")

    return True


def main():
    if len(sys.argv) < 2:
        print("用法: python scripts/recover_failed_task.py <task_id>")
        print("示例: python scripts/recover_failed_task.py 18fae1cbbf8d4a2ca1ffc2787b491df9")
        sys.exit(1)

    task_id = sys.argv[1]

    # 项目路径
    project_root = Path(__file__).parent.parent
    db_path = project_root / "data" / "local.db"
    output_base = project_root / "output"
    log_file = project_root / "logs" / "task.log"

    print(f"🔧 数据恢复工具")
    print(f"=" * 60)
    print(f"任务 ID: {task_id}")
    print(f"数据库: {db_path}")
    print(f"输出目录: {output_base}")
    print(f"日志文件: {log_file}")
    print(f"=" * 60)
    print()

    if not db_path.exists():
        print(f"❌ 数据库文件不存在: {db_path}")
        sys.exit(1)

    if not log_file.exists():
        print(f"⚠️  日志文件不存在: {log_file}")
        print(f"   将仅使用文件名作为段落文本")

    success = recover_task_data(task_id, db_path, output_base, log_file)

    sys.exit(0 if success else 1)


if __name__ == "__main__":
    main()
