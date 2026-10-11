# TODO List

## 1. 用户登录与权限分级（优先级：高）

目标：Web UI 从「单管理 token」升级为多用户登录体系，三级权限：
**所有者 > 管理员 > 用户**。

### 权限矩阵

| 能力 | 用户 | 管理员 | 所有者 |
|---|---|---|---|
| 聊天页对话（建会话/prompt/审批） | ✅ | ✅ | ✅ |
| MCP 增删改查 / 启停 | ❌ | ✅ | ✅ |
| Skills 增删改查 / 启停 | ❌ | ✅ | ✅ |
| 应用接入（app 增删改查、开关、token 重置） | ❌ | ✅ | ✅ |
| 推理后端（daemon）管理、审计日志查看 | ❌ | ✅ | ✅ |
| 用户管理（新增用户、改密码、停用） | ❌ | ✅ | ✅ |
| 指定/撤销管理员、用户 | ❌ | ❌ | ✅ |
| 转移/指定所有者 | ❌ | ❌ | ✅ |

### 任务拆解

- [ ] **用户存储**：`ui-users.json`（QWEN_HOME 下，原子写），字段：用户名、
  密码哈希（PBKDF2/bcrypt，不存明文）、角色（owner/admin/user）、启用状态、
  创建时间；首次启动无用户时引导创建所有者账号（或环境变量
  `QWEN_UI_OWNER_PASSWORD` 初始化）
- [ ] **登录接口**：`POST /api/auth/login`（用户名+密码 → 签发会话 token，
  带过期时间，如 12 小时）、`POST /api/auth/logout`、`GET /api/auth/me`
  （返回当前用户与角色）
- [ ] **鉴权中间件改造**：现有 `QWEN_UI_ADMIN_TOKEN` 逻辑替换/兼容为
  「登录 token 或 admin token 皆可」；`/api/*` 按路由标注所需最低角色
  （聊天会话类 = user 起；MCP/skills/apps/daemons/audit = admin 起；
  用户管理 = admin 可查、owner 可改角色）
- [ ] **用户管理路由**：`GET/POST /api/users`、`PUT /api/users/:name`
  （改密码/停用）、`POST /api/users/:name/role`（仅 owner）、
  `DELETE /api/users/:name`（owner 不可删自己）
- [ ] **前端**：登录页（未登录跳转）、顶栏显示当前用户与角色、按角色
  隐藏无权限的页面入口和按钮、用户管理卡片（仅 admin/owner 可见，
  角色操作仅 owner 可见）
- [ ] **审计接入**：登录成功/失败、用户增删改、角色变更全部落
  `ui-audit.jsonl`（`admin_user_*` / `auth_login*` 事件）
- [ ] **兼容与迁移**：未配置任何用户时保持现状（无鉴权，仅本机使用场景）；
  文档说明从 `QWEN_UI_ADMIN_TOKEN` 迁移到用户体系的方式
- [ ] **测试**：离线单测（用户存储、角色判定、密码哈希）+
  verify_ui 全链路（登录→各角色访问边界→越权 403→审计落盘）

## 2. 会话记忆（刷新不丢聊天记录）（优先级：中）—— ✅ 已完成

实现（均已验证）：

- [x] 刷新后恢复消息：`GET /api/sessions/{sid}/transcript` 透传 daemon 的
  `GET /session/:id/transcript`（0.21.13 有 `session_transcript` 能力，
  分页 direction=backward 逐页拉全）；前端 `selectSession` 在 resume 成功
  且内存无消息时自动恢复
- [x] 退化方案：UI 服务端把管理面会话的 SSE 事件旁路落盘到
  `<QWEN_HOME>/ui-chat-cache/<sid>.jsonl`（chatcache.py，超 4MB 截尾），
  prompt 路由合成 user_message_chunk（SSE 回显的 user chunk 已过滤，
  不会重复）；`GET /api/sessions/{sid}/history` 回放；删会话时清除缓存
- [x] 结构化消息重建：rebuildMessages 统一处理两种来源（envelope /
  transcript replay 帧），还原 user/assistant 文本、思考、工具调用；
  历史权限审批条渲染为只读提示（按钮回调无法重放）
- [x] 测试：test_gateway 加 TestChatCache（4 用例：读写/隔离与路径消毒/
  清除/截尾）；verify_ui 加「缓存落盘+回放+user 去重 + daemon transcript」
  断言；真实浏览器实测「发消息→刷新→点会话→记录完整恢复」

## 3. 工作区浏览 bug：文件夹进不去（优先级：高）—— ✅ 已修复

现象：聊天页「📁 工作区」下拉的浏览文件夹面板里，点文件夹进不去
（无法下钻），只能后退。

根因：`index.html` document 级「点击面板外关闭」监听器用
`picker.contains(ev.target)` 判断，而下钻处理器 `wsBrowse()` 同步
`innerHTML` 清空列表——点击目标在事件冒泡到 document 前已被摘除，
`contains` 误判为「面板外」把面板关闭。修复：改用
`ev.composedPath()`（分发开始时定格，包含已摘下祖先）判断。

已用真实浏览器全流程验证：盘符列表 → C:\ → Users → Administrator
多级下钻正常、「选择当前目录」后胶囊更新正确。（注：建会话时若选
了与 daemon 既有工作区祖先/嵌套的目录，daemon 会拒 `Workspace
path nests/conflicts`，属 daemon 侧策略，非本 bug。）

## 4. 会话级工具配置 + 工作区选择报错友好化（优先级：高）—— ✅ 已完成

需求：新建会话失败（Workspace path conflicts / scratch root）；每个会话
可单独设置工具开关、单独选工作目录。

实现（均已验证）：

- [x] 会话级工具覆盖：daemon 无会话级工具开关，但工具注册表只在会话
  创建（spawn）时读一次 disabled 集——`POST /api/sessions` 支持
  `toolDisable`/`toolEnable` 字段，服务端建会话前临时改工作区级开关、
  建完 finally 逆序恢复（`_create_session_with_tools`），效果等同按会话
  配置；响应附 `toolOverrides` 并落审计 `session_tool_overrides`
- [x] `GET /api/tools/state?cwd=`：读指定工作区全局禁用集（面板基线）
- [x] 前端「🔧 工具」胶囊面板：勾选 = 新会话可用，跟随全局/自定义，
  localStorage 持久，会话列表带 🔧 角标
- [x] 工作区冲突预检：选目录前先查 `/api/workspaces`，祖孙目录（双向
  嵌套）直接红 toast 拒绝并说明原因，不再等 daemon 409
- [x] 报错中文化：`friendlySessionError` 把 scratch_root_conflict /
  workspace_nested 等 daemon 报错翻成人话
- [x] 修复「冲突拒绝后仍弹成功 toast」：`wsSelect` 返回是否选中，
  成功才弹「新会话工作区：…」
- [x] 测试：verify_ui 加「会话级工具覆盖（临时禁用/启用 + 建后全局
  恢复）」断言（PASS，74 单测 OK）；真实浏览器实测：工具面板勾选、
  C:\Temp 选中成功、C:\（与已注册工作区祖孙）被红 toast 拒绝且
  不弹假成功提示
- 注：调试/验证实例的 daemon 现在以独立临时目录为 cwd
  （verify_ui `cwd=tmp_ws`），工具开关不再污染仓库 `.qwen/settings.json`

## 5. UI 界面美化 / 重构（优先级：低）

- [ ] 现状为单文件 `index.html`（原生 JS + 内联 CSS），功能堆叠后维护
  成本上升；可重构为前端工程（如 Vite + Vue/React），或保守做样式美化：
  统一设计变量、卡片间距、表格斑马纹、暗色主题完善、移动端适配
- [ ] 登录页（随功能 1 落地）与整体风格统一
- [ ] 聊天页体验优化：Markdown 渲染、代码块高亮、消息分组
- [ ] 前提：功能 1 完成后再动，避免两个大改交织
