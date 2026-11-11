# Sample Shop：故意含有 bug 的合成仓库

任务：`discounted_total(200, 10)` 应返回 `180`，当前返回负数。请定位百分比单位错误，提出一个最小补丁，保留输入检查，并用现有测试验证。

- `pricing.py`：可编辑业务代码。
- `tests/test_pricing.py`：受保护的验收条件；工具拒绝修改 tests 目录。
- 此仓库不包含真实业务、真实用户或私有代码。
- `verify_python` 仅检查 AST 语法，不代表测试通过。
- `sandbox_test` 仅在 Docker 守护进程可用、已存在 `python:3.12-slim` 镜像时运行测试；不会自动拉取镜像，也不会在宿主机执行 Agent 修改过的 Python。

手动准备可选容器环境：`docker pull python:3.12-slim`。生产系统还需要镜像摘要锁定、容器运行时补丁、独立执行主机和专门的权限策略。
