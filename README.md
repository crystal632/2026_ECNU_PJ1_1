# 基于 MongoDB 的问卷系统（第二阶段）

本项目按第一阶段初始需求实现简化在线问卷系统，包含用户注册登录、问卷创建和管理、可复用题目与题库、题目共享和版本历史、单选/多选/文本/数字题、题目校验与条件跳转、答卷提交以及问卷内统计。题目直接嵌入问卷文档，答卷独立存储在 MongoDB。

题目支持版本、共享、历史恢复、题库和跨问卷统计，问卷保存选用时的题目快照。匿名问卷仍要求填写者登录；匿名表示答卷不保存填写者 ID。系统由 FastAPI、MongoDB 和 `web/index.html` 简易网页组成。

## 环境与启动

需要 Python 3.10+ 和 MongoDB。PowerShell 中执行：

```powershell
py -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
$env:MONGO_URL = "mongodb://localhost:27017/"
$env:SECRET_KEY = "请设置随机密钥"
uvicorn main:app --reload
```

访问 `http://127.0.0.1:8000/app/` 使用网页，`http://127.0.0.1:8000/docs` 查看 API 文档。数据库名默认为 `questionnaire_db`，可通过 `MONGO_DATABASE` 修改。

## 问卷配置示例

```json
{
  "title": "基本情况",
  "description": "请按实际情况填写",
  "is_anonymous": false,
  "questions": [
    {"id":"q1","title":"年龄","type":"number","required":true,"validation":{"integer":true,"min":0,"max":120},"jump_logic":{"<18":"q3",">=18":"q2"}},
    {"id":"q2","title":"喜欢的水果","type":"multiple_choice","options":["苹果","香蕉"],"required":true,"validation":{"min_selections":1,"max_selections":2}},
    {"id":"q3","title":"补充说明","type":"text","required":false,"validation":{"max_length":300}}
  ]
}
```

运行自动化用例：`python -m pytest -q`。提交前以实际运行结果为准。
