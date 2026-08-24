"""
生成预置音效 wav 文件
运行一次即可，生成后被 DraftBuilder 复用
"""

import wave, struct, math, random
from pathlib import Path

OUT = Path(__file__).parent
RATE = 44100


def write_wav(path, frames):
    with wave.open(str(path), "w") as f:
        f.setnchannels(1)
        f.setsampwidth(2)
        f.setframerate(RATE)
        f.writeframes(b"".join(frames))


def sample(val):
    return struct.pack("<h", max(-32768, min(32767, int(val * 32767))))


# ── 1. 打字机哒声（30秒循环，随机节奏）────────────────────────────────────
def gen_typewriter(duration_s=30):
    """模拟打字机：随机间隔的短促哒声"""
    total = duration_s * RATE
    buf = [sample(0)] * total

    rng = random.Random(42)
    t = int(RATE * 0.05)  # 第一声从50ms开始
    while t < total - 2000:
        # 单次哒声：60ms 衰减正弦波
        for i in range(min(int(RATE * 0.06), total - t)):
            tt = i / RATE
            env = math.exp(-tt * 50)
            v = env * (
                0.5 * math.sin(2 * math.pi * 900 * tt)
                + 0.3 * math.sin(2 * math.pi * 1800 * tt)
                + 0.2 * math.sin(2 * math.pi * 450 * tt)
            ) * 0.6
            buf[t + i] = sample(v)
        # 随机间隔 80~200ms（模拟真实打字节奏）
        t += int(RATE * rng.uniform(0.08, 0.20))

    write_wav(OUT / "typewriter.wav", buf)
    print("生成 typewriter.wav")


# ── 2. 滑入嗖声（0.3秒，频率下扫）─────────────────────────────────────────
def gen_swoosh(duration_s=0.35):
    n = int(RATE * duration_s)
    buf = []
    for i in range(n):
        t = i / RATE
        progress = i / n
        # 频率从 1200Hz 扫到 300Hz
        freq = 1200 - 900 * progress
        env = math.sin(math.pi * progress) * (1 - progress * 0.5)
        v = env * math.sin(2 * math.pi * freq * t) * 0.35
        buf.append(sample(v))
    write_wav(OUT / "swoosh.wav", buf)
    print("生成 swoosh.wav")


# ── 3. 弹出砰声（0.2秒，低频冲击）──────────────────────────────────────────
def gen_pop(duration_s=0.25):
    n = int(RATE * duration_s)
    buf = []
    for i in range(n):
        t = i / RATE
        env = math.exp(-t * 18)
        v = env * (
            0.7 * math.sin(2 * math.pi * 180 * t)
            + 0.3 * math.sin(2 * math.pi * 90 * t)
        ) * 0.7
        buf.append(sample(v))
    write_wav(OUT / "pop.wav", buf)
    print("生成 pop.wav")


# ── 4. 胶片快门声（图片切换用，~120ms）──────────────────────────────────────
def gen_click(duration_s=0.12):
    """
    模拟胶片相机快门：
    - 0~15ms：机械撞击瞬态（宽频白噪声爆发）
    - 15~50ms：金属弹簧回弹（中高频衰减）
    - 50~120ms：尾音消散
    """
    n = int(RATE * duration_s)
    rng = random.Random(7)
    buf = []
    for i in range(n):
        t = i / RATE

        # 第一段：撞击瞬态（宽频噪声，极快衰减）
        noise = rng.uniform(-1, 1)
        attack = math.exp(-t * 400) * noise * 0.9

        # 第二段：金属共鸣（中高频，慢一点衰减）
        resonance = (
            math.exp(-t * 80) * math.sin(2 * math.pi * 4200 * t) * 0.5
            + math.exp(-t * 60) * math.sin(2 * math.pi * 2800 * t) * 0.3
            + math.exp(-t * 40) * math.sin(2 * math.pi * 1400 * t) * 0.15
        )

        # 第三段：低频机械闷响（快门帘幕闭合感）
        thud = math.exp(-t * 200) * math.sin(2 * math.pi * 280 * t) * 0.4

        v = (attack + resonance + thud) * 0.6
        buf.append(sample(v))
    write_wav(OUT / "click.wav", buf)
    print("生成 click.wav")


if __name__ == "__main__":
    gen_typewriter(30)
    gen_swoosh()
    gen_pop()
    gen_click()
    print("所有音效生成完毕")
