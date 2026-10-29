# 问卷系统 API 说明（第二阶段）

所有受保护接口使用 `Authorization: Bearer <access_token>`。注册和登录不需要 token。时间采用 ISO 8601 格式，截止时间应包含时区。

| 方法 | 路径 | 用途 |
|---|---|---|
| POST | `/api/register` | 注册账号 |
| POST | `/api/login` | 登录并取得 token |
| GET | `/api/me` | 查看当前用户 |
| POST | `/api/questions` | 保存一道可复用题目（v1） |
| GET | `/api/questions` | 列出本人及共享给本人的题目 |
| GET | `/api/questions/{id}` | 查看有权访问的题目版本 |
| POST | `/api/questions/{id}/versions` | 创建不可变的新版本 |
| GET | `/api/questions/{id}/history` | 查看题目版本历史 |
| POST | `/api/questions/{id}/restore/{version_id}` | 将历史内容恢复为新版本 |
| PUT | `/api/questions/{id}/share` | 按用户名共享题目 |
| GET | `/api/questions/{id}/usage` | 查看所有版本被哪些问卷使用 |
| GET | `/api/questions/{id}/statistics` | 查看跨问卷统计 |
| POST | `/api/question-banks` | 创建个人题库 |
| GET | `/api/question-banks` | 列出个人题库 |
| PUT | `/api/question-banks/{bank_id}/questions` | 设置题库中的题目版本 ID |
| GET | `/api/question-banks/{bank_id}/questions` | 列出题库题目 |
| POST | `/api/question-banks/{bank_id}/questions/{question_id}/update` | 将题库中的题目更新为最新版本 |
| POST | `/api/surveys` | 创建问卷及题目 |
| GET | `/api/surveys` | 列出本人问卷 |
| GET | `/api/surveys/{id}` | 查看本人问卷 |
| PATCH | `/api/surveys/{id}` | 发布或关闭问卷 |
| GET | `/survey/{id}` | 登录后读取开放问卷 |
| POST | `/survey/{id}/responses` | 提交答卷 |
| GET | `/api/surveys/{id}/statistics` | 查看整卷统计 |
| GET | `/api/surveys/{id}/questions/{qid}/statistics` | 查看单题统计 |

题型为 `single_choice`、`multiple_choice`、`text`、`number`。每题包含唯一 `id`、`title`、`type`、`required`，选择题还需提供 `options`。`validation` 可配置多选数量、文本长度或数字范围；`jump_logic` 的键为选项文本或数字比较表达式，值为目标题目 ID 或 `end`。

答卷请求示例：`{"answers":{"q1":"苹果"}}`。服务端按跳转路径验证并保存实际到达题目的答案。匿名问卷不会保存答题者 ID，但填写者仍需登录。


## 第二阶段：复用题目

调用 `POST /api/questions` 并提交题目字段即可保存题目，响应中包含 `question_id`。创建问卷时，可将已保存题目的 ID 放入 `question_ids`，也可与直接填写的 `questions` 混用。问卷保存题目快照及来源题目 ID、版本号，后续新版本不会改变已有问卷。

调用 `POST /api/questions/{id}/versions` 创建新版本；版本不可变并记录父版本。历史接口返回版本链；恢复操作会把旧版内容复制为新的最新版本。共享请求格式为 `{"usernames":["teammate"]}`。接收者可查看和复用题目，只有所有者可以创建版本或继续共享。

题库是用户私有的题目版本 ID 集合。更新接口会把题库中的一个版本替换为该版本链的最新版本。跨问卷统计会汇总该版本链在所有问卷中的已提交答卷；数字题返回值列表、均值和按数值汇总的频数分布。
