# 剪映兼容性测试指南

## 测试工具说明

本项目提供了两个测试工具来验证生成的草稿是否符合剪映规范：

### 1. 单项目测试 (`test_jianying_compatibility.py`)

测试单个草稿项目的兼容性。

**用法：**
```bash
source venv/bin/activate
python tests/test_jianying_compatibility.py output/项目名称
```

**示例：**
```bash
python tests/test_jianying_compatibility.py output/行行出状元
```

**测试内容：**
- ✅ 文件存在性检查（draft_content.json, draft_meta_info.json）
- ✅ JSON 格式验证
- ✅ 必需字段检查（id, duration, fps, materials, tracks 等）
- ✅ 轨道结构检查（视频轨、音频轨、文本轨）
- ✅ 时间轴连续性检查（无间隙、无重叠）
- ✅ 素材引用完整性检查
- ✅ 文件路径有效性检查
- ✅ 时长一致性检查

### 2. 批量测试 (`batch_test_all_drafts.py`)

批量测试所有草稿项目。

**用法：**
```bash
source venv/bin/activate
python tests/batch_test_all_drafts.py
```

**输出示例：**
```
============================================================
批量测试总结
============================================================

✅ 完全通过: 9 个
   - 11
   - 3:4测试-短
   - 书
   - 毅力
   ...

总计: 9 个项目
成功率: 100.0%
```

## 实际导入测试步骤

自动化测试只能验证格式正确性，**最终验证需要在剪映中实际导入**。

### macOS 剪映专业版导入步骤

#### 方法1：直接复制到剪映草稿目录（推荐）

1. **找到剪映草稿目录：**
   ```bash
   ~/Movies/JianyingPro Drafts/
   ```

2. **复制草稿文件夹：**
   ```bash
   # 示例：导入"行行出状元"项目
   cp -r output/行行出状元 ~/Movies/JianyingPro\ Drafts/
   ```

3. **打开剪映专业版：**
   - 启动剪映专业版
   - 在草稿列表中应该能看到新导入的项目
   - 点击打开项目

4. **验证检查清单：**
   - [ ] 草稿能否在列表中显示
   - [ ] 打开草稿是否报错
   - [ ] 视频轨素材是否按顺序排列
   - [ ] 音频轨是否正确对齐
   - [ ] 字幕轨是否显示
   - [ ] 播放是否流畅无卡顿
   - [ ] 时长是否正确
   - [ ] 素材是否完整加载

#### 方法2：使用剪映的"导入草稿"功能

1. 打开剪映专业版
2. 点击"文件" -> "导入草稿"
3. 选择草稿目录（如 `output/行行出状元`）
4. 等待导入完成

### Windows 剪映专业版导入步骤

1. **找到剪映草稿目录：**
   ```
   C:\Users\你的用户名\AppData\Local\JianyingPro\User Data\Projects\
   ```

2. **复制草稿文件夹到该目录**

3. **打开剪映专业版验证**

## 常见问题排查

### 问题1：草稿在列表中不显示

**可能原因：**
- `draft_meta_info.json` 缺失或格式错误
- 草稿目录名称包含特殊字符

**解决方法：**
```bash
# 检查 meta 文件是否存在
ls -la output/项目名称/draft_meta_info.json

# 运行兼容性测试
python tests/test_jianying_compatibility.py output/项目名称
```

### 问题2：打开草稿时报错

**可能原因：**
- `draft_content.json` 格式错误
- 必需字段缺失
- 素材文件路径错误

**解决方法：**
```bash
# 运行详细测试
python tests/test_jianying_compatibility.py output/项目名称

# 检查素材文件是否存在
ls -la output/项目名称/images/
ls -la output/项目名称/voiceovers/
```

### 问题3：素材无法加载

**可能原因：**
- 素材文件路径不正确
- 素材文件损坏
- 素材格式不支持

**解决方法：**
```bash
# 检查素材文件完整性
file output/项目名称/images/*.png
file output/项目名称/voiceovers/*.wav

# 验证文件路径
python tests/test_jianying_compatibility.py output/项目名称
```

### 问题4：时间轴不连续或有间隙

**可能原因：**
- 片段时间计算错误
- 素材时长与实际不符

**解决方法：**
- 测试工具会自动检测时间轴问题
- 查看测试报告中的警告信息

## 测试最佳实践

### 1. 开发阶段测试

每次生成新草稿后立即运行测试：

```bash
# 生成草稿后
python tests/test_jianying_compatibility.py output/最新项目

# 如果测试通过，再进行实际导入
```

### 2. 批量验证

定期对所有历史项目进行批量测试：

```bash
# 每周运行一次
python tests/batch_test_all_drafts.py > test_report_$(date +%Y%m%d).txt
```

### 3. 回归测试

修改代码后，确保不影响已有项目：

```bash
# 修改代码前
python tests/batch_test_all_drafts.py > before.txt

# 修改代码后
python tests/batch_test_all_drafts.py > after.txt

# 对比结果
diff before.txt after.txt
```

### 4. 创建测试用例

为特殊场景创建最小测试用例：

```bash
# 创建简单测试项目（3段视频 + 1段音频）
# 用于快速验证基本功能

# 测试边界条件
# - 超长视频（10分钟+）
# - 大量片段（50+）
# - 特殊字符文件名
# - 不同画幅比例（16:9, 9:16, 3:4）
```

## 自动化集成

### CI/CD 集成

在 GitHub Actions 或其他 CI 中集成测试：

```yaml
# .github/workflows/test.yml
name: Test Jianying Compatibility

on: [push, pull_request]

jobs:
  test:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v2
      - name: Set up Python
        uses: actions/setup-python@v2
        with:
          python-version: '3.11'
      - name: Install dependencies
        run: |
          pip install -r requirements.txt
      - name: Run compatibility tests
        run: |
          python tests/batch_test_all_drafts.py
```

### Pre-commit Hook

在提交前自动运行测试：

```bash
# .git/hooks/pre-commit
#!/bin/bash
source venv/bin/activate
python tests/batch_test_all_drafts.py
if [ $? -ne 0 ]; then
    echo "❌ 兼容性测试失败，请修复后再提交"
    exit 1
fi
```

## 测试报告解读

### 完全通过示例

```
✅ 通过的检查:
  ✓ draft_content.json 存在
  ✓ JSON 格式有效
  ✓ 所有必需字段存在 (7 个)
  ✓ 轨道结构正确: {'video': 1, 'audio': 1, 'text': 1}
  ✓ 时间轴连续，无间隙或重叠
  ✓ 素材引用正确: 36 个素材, 36 个被引用
  ✓ 所有文件路径有效 (24 个文件)
  ✓ 时长: 27.30秒 (轨道: 27.30秒)

🎉 所有关键测试通过！草稿应该可以在剪映中正常打开。
```

### 有警告示例

```
⚠️  警告 (不影响导入):
  轨道 video 存在间隙: 0.033秒 (片段 2 -> 3)
  有 2 个素材未被引用
```

**说明：** 警告不会阻止导入，但可能影响播放效果。

### 有错误示例

```
❌ 错误 (可能导致导入失败):
  缺少必需字段: duration, fps
  引用了 3 个不存在的素材
  有 5 个文件不存在

💥 发现 3 个错误，需要修复后才能导入剪映。
```

**说明：** 必须修复所有错误才能成功导入。

## 总结

1. **开发时**：每次生成草稿后运行单项测试
2. **提交前**：运行批量测试确保所有项目正常
3. **定期**：在实际剪映中导入测试，验证真实效果
4. **问题**：根据测试报告定位和修复问题

测试工具可以捕获大部分格式问题，但**最终验证仍需在剪映中实际导入测试**。
