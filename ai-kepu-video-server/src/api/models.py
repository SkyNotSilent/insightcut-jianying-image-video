"""
API 数据模型
定义请求和响应的数据结构
"""

from typing import Dict, Literal, Optional, List
from pydantic import BaseModel, Field
from enum import Enum


class TaskStatus(str, Enum):
    """任务状态枚举"""
    PENDING = "pending"
    PROCESSING = "processing"
    AWAITING_CONFIRMATION = "awaiting_confirmation"
    AWAITING_FINALIZATION = "awaiting_finalization"
    INTERRUPTED = "interrupted"
    COMPLETED = "completed"
    FAILED = "failed"
    DELETING = "deleting"


class StepStatus(str, Enum):
    """步骤状态枚举"""
    PENDING = "pending"
    PROCESSING = "processing"
    COMPLETED = "completed"
    FAILED = "failed"


class TTSOptions(BaseModel):
    """可按任务或分段固化的 TTS 参数。"""

    speed_level: Literal["very_slow", "slow", "normal", "fast", "very_fast"] = "normal"
    volume_ratio: Optional[float] = Field(None, ge=0.5, le=2.0)
    style_prompt: Optional[str] = Field(None, max_length=300)


class SubtitleOptions(BaseModel):
    """跨即时预览、完整视频和剪映草稿共享的字幕快照。"""

    size: Literal["small", "standard", "large"] = "standard"
    position: Literal["low", "standard", "high"] = "standard"
    outline: Literal["light", "standard", "strong"] = "standard"


class GenerationOptions(BaseModel):
    """任务级生成策略；只影响后续生成操作。"""

    prompt_concurrency: int = Field(4, ge=1, le=8)
    image_concurrency: int = Field(8, ge=1, le=8)
    retry_count: int = Field(2, ge=0, le=5)
    retry_interval_seconds: int = Field(5, ge=1, le=60)


class RegenerateAudioRequest(BaseModel):
    """单段重配音的 JSON 请求；查询参数仍保留兼容。"""

    voice_type: Optional[str] = Field(None, description="统一音色 ID")
    tts_options: Optional[TTSOptions] = None


class CreateTaskRequest(BaseModel):
    """创建任务请求"""
    theme: str = Field(..., min_length=1, max_length=5000, description="视频主题或剧本文案")
    name: Optional[str] = Field(None, max_length=100, description="项目名称")
    input_mode: str = Field(default="script", description="输入模式：script=写作模式，theme=主题模式")
    style: str = Field(default="温暖感人", description="文章风格")
    ratio: str = Field(default="16:9", description="视频比例：16:9/9:16/3:4")
    length: int = Field(default=300, ge=0, le=2000, description="主题模式下的目标脚本字数；0 表示自动")
    voice_type: Optional[str] = Field(None, description="TTS 音色 ID")
    tts_options: Optional[TTSOptions] = None
    execution_mode: Literal["full", "review_first"] = Field(
        default="full", description="执行模式：full=兼容旧流程，review_first=先生成预案等待确认"
    )
    script_policy: Literal["rewrite", "verbatim"] = Field(
        default="rewrite", description="文稿处理：rewrite=改写，verbatim=脚本模式保留原文"
    )
    source_draft_id: Optional[str] = Field(None, max_length=120)
    template_id: Optional[str] = Field(None, max_length=64)
    generation_options: Optional[GenerationOptions] = None
    subtitle_options: Optional[SubtitleOptions] = None


class CreateTaskFromImagesRequest(BaseModel):
    """从本地图片创建任务请求"""
    style: Optional[str] = Field(default="温暖感人", description="文章风格|画面风格")
    ratio: Optional[str] = Field(default="16:9", description="视频比例：16:9/9:16/1:1")
    voice_type: Optional[str] = Field(None, description="TTS 音色 ID")
    name: Optional[str] = Field(None, max_length=100, description="项目名称")
    tts_options: Optional[TTSOptions] = None


class StepProgress(BaseModel):
    """步骤进度"""
    name: str = Field(..., description="步骤名称")
    status: StepStatus = Field(..., description="步骤状态")
    progress: Optional[int] = Field(None, description="当前进度")
    total: Optional[int] = Field(None, description="总数")
    duration: Optional[float] = Field(None, description="耗时（秒）")


class TaskProgress(BaseModel):
    """任务进度"""
    current_step: str = Field(..., description="当前步骤")
    steps: List[StepProgress] = Field(..., description="所有步骤")


class TaskResult(BaseModel):
    """任务结果"""
    draft_path: str = Field(..., description="草稿路径")
    draft_url: Optional[str] = Field(None, description="草稿下载链接")
    video_url: Optional[str] = Field(None, description="视频下载链接")
    theme: str = Field(..., description="视频主题")
    segments_count: int = Field(..., description="段落数")
    total_duration: Optional[float] = Field(None, description="总时长（秒）")
    created_at: str = Field(..., description="创建时间")


class TaskResponse(BaseModel):
    """任务响应"""
    task_id: str = Field(..., description="任务ID")
    status: TaskStatus = Field(..., description="任务状态")
    voice_type: Optional[str] = Field(None, description="任务创建时使用的 TTS 音色 ID")
    tts_options: Optional[TTSOptions] = Field(None, description="任务创建时固化的 TTS 参数")
    progress: Optional[TaskProgress] = Field(None, description="任务进度")
    result: Optional[TaskResult] = Field(None, description="任务结果")
    extract_path: Optional[str] = Field(None, description="用户上次使用的解压路径")
    error: Optional[str] = Field(None, description="错误信息")
    error_code: Optional[str] = Field(None, description="稳定错误分类")
    error_meta: Optional[Dict] = Field(None, description="脱敏错误元数据")
    can_resume: bool = Field(False, description="是否存在可继续生成的检查点")
    workflow_phase: Optional[str] = Field(None, description="工作台阶段")
    plan_version: int = Field(0, description="预案版本")
    execution_mode: str = Field("full", description="任务执行模式")
    voice_confirmed: bool = Field(False, description="是否已确认全片音色")


class CreateTaskResponse(BaseModel):
    """创建任务响应"""
    task_id: str = Field(..., description="任务ID")
    status: TaskStatus = Field(..., description="任务状态")
