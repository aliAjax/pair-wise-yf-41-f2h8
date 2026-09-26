# 地震台网事件编目与修订

这是一个只使用Python标准库和SQLite的模块化项目，默认端口为`8307`。所有业务规则集中在`src/rules.py`，`app.py`只负责组装依赖和启动服务。

## 模块结构

- `app.py`：命令行参数、依赖组装、启动和信号处理。
- `src/domain.py`：角色、数据结构、领域异常和基础校验。
- `src/rules.py`：状态机、权限、领域计算、冲突和跨对象校验。
- `src/repository.py`：SQLite建表、查询、事务和乐观锁。
- `src/service.py`：用例编排、幂等处理、版本控制和审计写入。
- `src/http_api.py`：HTTP路由、请求解析和统一错误响应。
- `src/audit.py`：实体操作审计时间线。
- `static/index.html`：可查看事件修订链、提交带版本号动作的演示页面。
- `tests/`：完整流程、规则和失败场景测试。

## 初始化与启动

```bash
python3 app.py --db ./data.db --port 8307
```

服务启动时会自动建表。`--host`可修改监听地址，`--db`可指定其他SQLite文件。

## 核心对象

- `station`：观测台站；`event`：地震事件及其多个修订版本。

## 主要接口

- `GET /health`：健康检查。
- `GET /api/<kind>`：按对象类型查询，可用`?status=`过滤。
- `POST /api/<kind>`：创建对象；请求体为JSON。
- `GET /api/entities/<id>`：读取对象当前版本（事件会顺带登记“旧事件首次查看”基线）。
- `POST /api/entities/<id>/actions`：提交`{"action":"动作名","data":{...},"expected_version":数字}`。
- `GET /api/events/<id>/revisions`：事件修订链（`/api/entities/<id>/revisions`为别名）。
- `GET /api/audit`：读取审计记录。

请求身份通过`X-User-Id`和`X-Role`请求头传入。创建和动作的可执行角色由规则引擎控制。

## 事件修订追溯

事件的每一次变化（`create`、`associate`、`review`、`publish`、`revise`、`withdraw`）都会在
`event_revisions`表中追加一条不可改动的记录，按自增序号`seq`排列，包含：

- 动作前后的版本号（`from_version`/`to_version`）与状态（`from_status`/`to_status`）；
- 动作前后的震级（`from_magnitude`/`to_magnitude`，未标定时为`null`）；
- 修订原因（`reason`，revise/withdraw取提交的reason，publish取发布文号）；
- 提交人`actor_id`、角色`actor_role`与UTC时间`created_at`；
- 完整补丁`detail.patch`，可据此回到任一指定版本。

修订记录与业务更新在同一事务内写入；表上建有`UPDATE`/`DELETE`触发器，任何篡改尝试都会被数据库拒绝。

**过期版本号**：提交的`expected_version`与当前版本不一致时返回`409 ConflictError`，
响应体给出`expected_version`、`current_version`与重取提示，且实体与修订链均不发生任何变化。

**旧事件首次查看**：对功能上线前没有修订记录的历史事件，任何带身份的查看（`GET`实体或修订链）
都会在锁定事务中补一条`first_view`基线，记录当时的状态、震级、版本与查看人；并发查看只生成一条。

## 测试

```bash
python3 -m unittest discover -s tests -v
```

## 局限

事件关联使用简化时间差和距离阈值，不包含完整地震定位、震级标定或台站仪器响应。
