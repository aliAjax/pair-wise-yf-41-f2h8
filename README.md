# 地震台网事件编目与修订

这是一个只使用Python标准库和SQLite的模块化项目，默认端口为`8307`。所有业务规则集中在`src/rules.py`，`app.py`只负责组装依赖和启动服务。

## 模块结构

- `app.py`：命令行参数、依赖组装、启动和信号处理。
- `src/domain.py`：角色、数据结构、领域异常和基础校验。
- `src/rules.py`：状态机、权限、领域计算、冲突和跨对象校验。
- `src/repository.py`：SQLite建表、查询、事务和乐观锁、事件修订链存储。
- `src/service.py`：用例编排、幂等处理、版本控制、审计与修订记录写入。
- `src/http_api.py`：HTTP路由、请求解析和统一错误响应。
- `src/audit.py`：实体操作审计时间线。
- `static/index.html`：最小演示页面。
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
- `GET /api/entities/<id>`：读取对象当前版本。
- `POST /api/entities/<id>/actions`：提交`{"action":"动作名","data":{...},"expected_version":数字}`。
- `GET /api/events/<id>/revisions`：查看事件修订记录（按顺序）。
- `GET /api/events/<id>/revisions/<seq>`：查看指定版本的完整快照。
- `GET /api/audit`：读取审计记录。

### 事件修订追溯

每次创建、关联、复核、发布、修订、撤回都会在`event_revisions`中追加一条不可变记录，
包含顺序号`seq`、动作、前后版本号、前后状态、前后震级、修订原因、提交人/角色、提交时间
以及该版本的完整数据快照（用于回到指定版本查看）。修订记录与实体更新在同一事务内写入。

- 提交动作时若`expected_version`已过期，返回`409 StaleVersionError`，响应体带
  `expected_version`与`current_version`，明确提示重新获取最新版本；本次修改与修订记录
  都不会写入，原记录保持不变。
- 修订追溯功能上线前创建的旧事件，在**首次被查看**时（`GET /api/events/<id>`或
  修订记录接口，含任意角色身份）自动补写一条`first_view`基线记录，且只补一次。
- 根页面提供事件列表、修订时间线、指定版本快照查看和带版本号的修订/撤回操作。

请求身份通过`X-User-Id`和`X-Role`请求头传入。创建和动作的可执行角色由规则引擎控制。

## 测试

```bash
python3 -m unittest discover -s tests -v
```

## 局限

事件关联使用简化时间差和距离阈值，不包含完整地震定位、震级标定或台站仪器响应。
