# Qwen Memory 发布链路

这个仓库以后固定按“两份副本”协作，不再直接从运行目录发布。

## 目录分工

- 运行副本：`C:/Users/小陶/.qwen/memory`
- 发布副本：当前 Git 仓库工作区

运行副本负责日常使用和迭代，发布副本负责整理、测试、提交、打 tag、发布。

## 每次更新的固定步骤

1. 从运行副本挑选一个稳定版本
2. 将需要发布的源码同步到发布副本
3. 只在发布副本里做这些整理：
   - 修正导入路径
   - 更新 `README.md`
   - 更新 `pyproject.toml`
   - 清理本机绝对路径
   - 清理数据库、索引、日志等运行产物
4. 运行发布前验证
5. 检查 `git diff --stat`
6. 提交到 `main`
7. 打版本 tag
8. 创建 GitHub Release

## 发布前检查清单

- `py -m pip install -e .[dev]`
- `py -X utf8 tests/test_regression.py`
- `py -X utf8 tests/test_budget.py`
- `py -X utf8 tests/test_trigger.py`
- `py -X utf8 tests/test_experience.py`
- `py -X utf8 tests/test_rollback.py`
- `py -X utf8 -m qwen_memory.mem --help`
- `py -X utf8 -m qwen_memory.web_viewer --help`

## 发布前必须确认

- 仓库中不包含真实数据库
- 仓库中不包含语义索引缓存
- 仓库中不包含日志和临时文件
- 仓库中不包含个人绝对路径
- README 与当前版本能力一致
- 版本号在 `pyproject.toml`、`src/qwen_memory/__init__.py`、发布文案中一致

## 推荐版本策略

- 修复型更新：`1.1.1`
- 兼容的小版本升级：`1.2.0`
- 明显不兼容的大版本升级：`2.0.0`

## 这次发布使用的基准

- 发布版本：`1.1.0`
- 发布目标：把本地新版整理成标准可发布仓库
- 发布重点：结构升级、文档修复、测试整理、发布面清理
