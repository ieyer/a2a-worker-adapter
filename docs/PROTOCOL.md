# 07\_A2A实时通信与Agent接入协议｜V1.0

状态：APPROVED  
Owner：A2A Fleet Maintainers  
适用：所有 APPROVED/ACTIVE Worker，以及未来新增 Worker

# 一、目标与原则

本协议把多 Worker 系统从“机器人定时轮询 Google Sheet”升级为“A2A 主动推送 \+ 任务台账记录 \+ 文件交付”的实时协同架构。  
一级实时通道：A2A。  
二级控制与工具通道：MCP / Drive / Sheets / Docs。  
兜底通道：零 Token deterministic Poller，仅用于 A2A 不可用或 Worker 不具备实时唤醒能力时。  
Google Sheets 是 System of Record，不再作为高频消息队列。  
任何空闲监听不得调用 LLM；只有收到有效任务并成功领取后才允许唤醒 Agent。

# 二、角色模型

Commander：负责任务拆解、Worker 选择、A2A 路由、验收和最终汇总。  
A2A Gateway：负责发现 Agent Card、协议版本协商、发送任务、接收状态/Artifact、重试、鉴权和审计。  
Worker A2A Adapter：每个 Worker 的本地适配层，负责把 A2A Task 转换为该 Worker 的真实 wake/invoke 调用。  
Worker Agent：真正执行研究、浏览器、代码、文档、自动化等工作。  
Task Dashboard：权威任务与 Worker 状态台账。  
Google Drive：Context、Working、Deliverables、Evidence、Worker Memory 的权威文件存储。  
EVENTS：全链路事件日志。

# 三、A2A 接入最低要求

每个 Worker 必须登记：  
A2A\_ENABLED  
AGENT\_CARD\_URL  
A2A\_ENDPOINT  
A2A\_BINDING  
A2A\_VERSION  
WAKE\_MODE  
A2A\_STATUS  
Agent Card 必须至少声明 identity、supportedInterfaces、capabilities、skills、version。  
生产接口优先 HTTPS。  
协议基线使用 A2A 1.0；客户端请求必须携带 A2A-Version: 1.0。  
生产环境禁止无鉴权公开执行接口；Demo 可以无鉴权，但不得承载敏感信息和真实任务。

# 四、统一任务分发流程

1\. Commander 在 TASKS 创建 authoritative task row，写 TASK\_ID、ASSIGNED\_WORKER、STATUS=READY、Context/输入链接和 DONE\_CRITERIA。  
2\. Router 读取 WORKERS 的 A2A 状态。  
3\. 若 A2A\_STATUS=READY/PRODUCTION，Router 通过 Agent Card 确认 endpoint、binding、version、skills。  
4\. Router 发送 A2A Send Message 操作。messageId 必须唯一，并携带 TASK\_ID、CONTEXT\_VERSION、CLASSIFICATION、INPUT\_LINK、CONTEXT\_LINK、DONE\_CRITERIA 等最小必要 metadata。  
5\. Worker Adapter 收到请求后必须重新读取 TASKS 对应行，确认任务仍为 READY 且 ASSIGNED\_WORKER=自己。  
6\. Worker 成功 claim 后更新 TASKS：READY→RUNNING，ATTEMPTS+1，HEARTBEAT=now，CHECKPOINT=STARTED，并写 EVENTS:A2A\_ACK / TASK\_STARTED。  
7\. Adapter 才允许调用真实 Worker runtime，禁止仅由 A2A Server 返回“收到”却不唤醒真实 Agent。  
8\. Worker 执行过程中使用 streaming 或 push notification 返回状态；关键状态同时写入 EVENTS。  
9\. 结果应作为 Artifact/文件交付，OUTPUT\_LINK/EVIDENCE\_LINK 回写 TASKS；完成执行后 STATUS=VERIFYING。  
10\. Commander 验收通过后 STATUS=DONE。

# 五、A2A 状态与 TASKS 状态映射

A2A request accepted：TASKS 仍为 READY，直到 Worker 完成 claim。  
A2A task working：TASKS=RUNNING。  
A2A input/auth required：TASKS=BLOCKED，CHECKPOINT=WAITING\_EXTERNAL。  
A2A completed：Worker 写 OUTPUT/EVIDENCE 后 TASKS=VERIFYING，不得自行写 DONE。  
A2A failed/rejected/canceled：映射到 FAILED/BLOCKED/CANCELLED，并保留 Error、Context、Evidence。  
A2A Artifact 是结果载体；普通 Message 不作为唯一正式交付。

# 六、实时更新与可靠性

短任务优先同步 Send Message。  
长任务优先异步 Task \+ streaming 或 push notification。  
所有请求使用唯一 messageId，Worker 应做幂等去重，避免不可逆动作重复执行。  
网络中断后不得假设消息一定到达；Router 必须检查 TASKS/EVENTS/A2A task state 再决定是否重发。  
关键结果必须落入 TASKS/Drive/EVENTS，不能只存在于临时 stream 或私有会话。  
A2A 通道异常时才进入 FALLBACK\_POLLER，不得同时由 A2A 和 Poller 抢占同一 READY Task。

# 七、Fallback Poller

Fallback Poller 必须为 deterministic script，不得调用 LLM。  
默认频率由 Worker 自身基础设施能力决定，不再强制全体 5 分钟。  
若 Worker 平台的定时器会直接唤醒 LLM，则该定时器不属于 Zero-Token Poller。  
推荐：系统级 cron/systemd/hook/sidecar 可 1–5 分钟；只能通过厂商 Agent Timer 的 Worker 可放宽到 15 分钟或更低频。  
一旦 A2A 实时 wake 验证通过，该 Worker 的 Poller 降级为 health-check / disaster fallback，不参与正常任务抢占。

# 八、安全与信息分级

P0/P1 可经已授权 A2A 通道发送最小必要 Context。  
P2 必须显式授权，并优先发送引用/短期凭证而非复制完整内容。  
P3 永不通过共享 A2A、Sheet、Drive 或公共 endpoint。  
生产 Agent Card 不应暴露 secret。  
A2A endpoint 必须实现认证、授权、请求来源校验、限流、审计和防重放。  
公开 Demo endpoint 只能用于连通性测试。

# 九、Worker 上线验收

一个 Worker 的 A2A\_STATUS 只有同时满足以下条件才可设为 PRODUCTION：  
1\. Agent Card 可从控制平面访问。  
2\. 协议版本与 binding 可协商。  
3\. 外部 Send Message 能命中该 Worker。  
4\. Adapter 能真实 wake/invoke Worker runtime。  
5\. 能完成一条 Canary Task：READY→RUNNING→VERIFYING。  
6\. 能返回 Artifact 或 OUTPUT\_LINK。  
7\. EVENTS 中存在 A2A\_DISPATCHED、A2A\_ACK、TASK\_STARTED、OUTPUT\_READY。  
8\. 已启用认证；公开无鉴权 Demo 不得标记 PRODUCTION。  
9\. 重复 messageId 不会重复执行副作用。  
10\. A2A 不可用时 fallback 行为明确。

# 十、当前迁移策略

阶段 A：首个 Worker（worker-a）Echo Demo 仅作为 A2A connectivity proof，状态标记 DEMO\_OK / REAL\_WAKE\_PENDING。  
阶段 B：worker-a Adapter 接入真实 runtime，完成第一条真实 Canary。  
阶段 C：其余 Worker 分别建立 Adapter 并注册 Agent Card。  
阶段 D：Commander Router 优先 A2A Dispatch；Poller 降级为 fallback。  
阶段 E：非 ACTIVE Worker 在转正时必须补齐 A2A 自报字段或明确 A2A\_UNAVAILABLE。

# 十一、统一执行指令

所有 ACTIVE Worker 立即执行各自的 A2A onboarding task。  
不得自行修改其他 Worker 的 Agent Card、Endpoint 或身份。  
不得宣称“已接入 A2A”而只有 Echo；必须区分 DISCOVERY\_OK、DEMO\_OK、REAL\_WAKE\_OK、PRODUCTION。  
若无法直接 wake 自己的 Agent runtime，必须把限制写入 A2A\_STATUS 和 Evidence，并提出可执行 Adapter 方案。  
所有上线证据进入对应 Task Folder / 30\_Evidence。

# 十二、完成标准

最终系统应满足：任务创建后由 Commander 主动 A2A push；正常路径不依赖 5/15 分钟 LLM Timer；Worker 秒级收到任务；真实 Agent 被唤醒执行；状态与 Artifact 自动回写；Sheet/Drive 保持权威记录和审计；A2A 故障时才启用零 Token fallback。  
