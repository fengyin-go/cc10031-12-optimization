# RPA流程自动化中心

面向企业数据采集场景，提供RPA流程编排、元素定位、执行调度和结果校验的自动化运营平台

这是一个前后端分离的管理平台：前端 Vue 3 + Vite + TypeScript，后端 FastAPI（Python）。
两边各自独立启动，前端 dev server 已关掉自动打开页面，启动后按终端打印的地址手工打开。

## 目录结构

```text
.
├── frontend/                 Vue 3 + Vite + TypeScript 前端
│   ├── src/views/WorkbenchView.vue|tsx  全部模块共享工作台
│   ├── src/views/LocatorWorkbenchView.vue 定位工作台（缓存指纹/失效策略/执行记录）
│   ├── src/data/modules.ts              18个业务模块配置
│   ├── src/api/              统一请求封装
│   └── vite.config.ts        dev server 配置（open: false）
├── backend/                  FastAPI（Python） 后端
│   ├── app/routers/resource.py  通用查询/导出/登记/动作接口
│   ├── app/services/<key>.py    每个模块独立领域逻辑
│   ├── app/services/base.py     共享服务骨架
│   ├── app/services/locator_cache.py    定位缓存/结构指纹/执行台账
│   ├── app/services/locator_workbench.py 定位工作台聚合（处理项/窗口重算）
│   └── app/store.py          内存数据仓库与示例数据
├── .gitignore
└── docker-compose.yml
```

## 元素定位恢复链路

动态页面会反复探测已失效的定位策略，定位链路按下面的规则收敛：

- 失效选择器与**页面结构指纹**一起缓存，指纹变化后缓存立即失效，
  失败恢复不会沿用旧命中。指纹由 `app/services/locator_cache.py`
  的 `summarize_page_model` 统一计算，`element_selector` / `page_model` /
  `runtime_session` 共用同一套判定，不再各写一遍。
- 结构未变时命中缓存，探测次数为 0；缓存失效时每个元素最多探测
  主策略 + 回退两条候选路径，重试只处理仍失效的元素，不会重复扫描整组选择器。
- 结构变化且旧回退仍能命中时，可选 `force_rebuild`（强制重建全部定位）
  或 `probe_then_decide`（先探测再决定重建）；两种策略都不会把旧命中
  发布成新记录，统一落到 **12 号处理终态**「结构冲突待重建」。
- 并发执行由首胜闸门控制，只接受首个成功结果，落败记录标记 9 号终态。
- 未绑定页面模型的旧版策略按合成摘要执行，保持既有版本边界可用。

定位工作台页面：前端 `/locator-workbench`，接口如下：

| 接口 | 用途 |
| --- | --- |
| `GET /api/locator-workbench` | 按页面模型查看缓存指纹、失效策略、可重试边界与最近执行记录 |
| `GET /api/locator-workbench/window?page_model_id=&window=` | 重算窗口内定位策略执行历史，解析可复用选择器边界 |
| `POST /api/locator-workbench/retry` | 重试仍失效的元素（`policy` 二选一） |
| `POST /api/locator-workbench/work-item` | 接收处理项并展开定位策略执行记录数据集 |
| `POST /api/locator-workbench/pipeline` | 发布策略并执行一轮定位（首胜提交） |
| `POST /api/locator-workbench/simulate-refresh` | 模拟动态页面刷新，触发指纹失效 |
| `GET /api/locator-executions` | 定位策略执行记录数据集（按窗口过滤） |

## 启动

### 后端

```bash
cd backend
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
./run.sh
```

健康检查：`curl http://127.0.0.1:8000/api/health`

### 前端

```bash
cd frontend
npm ci
npm run dev
```

前端默认监听 `http://127.0.0.1:5173/`，dev server 不会自动打开浏览器，
需要自己访问。`/api` 由 vite 代理到后端 `http://127.0.0.1:8000`。

## 业务模块

| 模块 | 目录 | 业务对象 | 主要字段 |
| --- | --- | --- | --- |
| 流程定义 | `bot_definition` | RPA流程 | 流程名称、目标系统、触发方式 |
| 步骤编排 | `step_editor` | 流程步骤 | 步骤序号、步骤名称、操作类型 |
| 元素定位 | `element_selector` | 定位规则 | 规则名称、选择器类型、定位表达式 |
| 触发配置 | `trigger_config` | 触发器 | 触发器名称、触发类型、调度表达式 |
| 执行记录 | `execution_log` | 执行日志 | 执行编号、所属流程、开始时间 |
| 数据提取 | `data_extract` | 提取规则 | 规则名称、源页面结构、提取字段 |
| 凭据管理 | `credential_store` | 系统凭据 | 凭据名称、目标系统、认证方式 |
| 环境配置 | `env_profile` | 执行环境 | 环境名称、浏览器版本、操作系统 |
| 反封锁 | `anti_block` | 防护策略 | 策略名称、目标站点、请求间隔 |
| 结果校验 | `result_verify` | 校验规则 | 规则名称、校验字段、校验条件 |
| 批次追踪 | `batch_tracker` | 采集批次 | 批次编号、触发流程、采集时间 |
| 页面建模 | `page_model` | 页面模型 | 模型名称、目标站点、页面结构 |
| 数据补全 | `data_enrich` | 补全规则 | 规则名称、目标字段、补全来源 |
| 代理路由 | `proxy_router` | 代理节点 | 节点名称、IP地址、出口地域 |
| 频控策略 | `rate_limiter` | 频控规则 | 规则名称、目标站点、每秒上限 |
| 输出格式 | `output_format` | 格式模板 | 模板名称、输出类型、字段映射 |
| 异常日志 | `anomaly_log` | 异常事件 | 事件编号、关联流程、异常类型 |
| 调度计划 | `scheduler` | 采集计划 | 计划名称、绑定流程、cron表达式 |
| 运行概览 | `report_dashboard` | 运行数据 | 统计日期、总执行数、成功数 |
| 任务队列 | `task_queue` | 排队任务 | 任务编号、流程名称、提交时间 |

## 约定

- 全部模块共享`frontend/src/views/WorkbenchView.vue|tsx`和一套资源路由。
- 每个模块仍保留独立`backend/app/services/<模块>.py`；纯前端保留
  `frontend/src/domain/<模块>.ts`，用于承载真实业务差异并维持36题位容量。
- 列表接口统一返回 `{ items, total, page, size }`，动作接口统一返回 `{ ok, message }`。
- 状态流转只允许在 `app/services` 里改，路由层不做业务判断。

领域：数据工程
子方向：数据采集、爬虫与RPA
