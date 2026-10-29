"""MongoDB-backed questionnaire API with reusable question versioning."""
from datetime import datetime, timedelta, timezone
from contextlib import asynccontextmanager
import os
from pathlib import Path
from typing import Any, Dict, List, Literal, Optional

from bson import ObjectId
from fastapi import Depends, FastAPI, HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jose import JWTError, jwt
from passlib.context import CryptContext
from pydantic import BaseModel, Field
from fastapi.staticfiles import StaticFiles

from database import (
    question_banks_collection,
    questions_collection,
    responses_collection,
    surveys_collection,
    users_collection,
)

@asynccontextmanager
async def lifespan(_app):
    users_collection.create_index("username", unique=True)
    surveys_collection.create_index([("creator_id", 1), ("created_at", -1)])
    responses_collection.create_index([("survey_id", 1), ("submitted_at", -1)])
    questions_collection.create_index([("owner_id", 1), ("lineage_id", 1), ("version", -1)])
    questions_collection.create_index("shared_with")
    question_banks_collection.create_index([("owner_id", 1), ("name", 1)], unique=True)
    yield


app = FastAPI(title="问卷系统 API", version="2.0.0", lifespan=lifespan)
pwd_context = CryptContext(schemes=["pbkdf2_sha256"], deprecated="auto")
bearer = HTTPBearer(auto_error=False)
SECRET_KEY = os.getenv("SECRET_KEY", "dev-only-change-this-secret-before-deployment")
ALGORITHM = "HS256"
TOKEN_HOURS = 12


class UserCredentials(BaseModel):
    username: str = Field(min_length=3, max_length=50)
    password: str = Field(min_length=8, max_length=128)


class Question(BaseModel):
    id: str = Field(min_length=1, max_length=80)
    title: str = Field(min_length=1, max_length=500)
    type: Literal["single_choice", "multiple_choice", "text", "number"]
    options: List[str] = Field(default_factory=list)
    required: bool = True
    validation: Dict[str, Any] = Field(default_factory=dict)
    # Map an answer (option or numeric comparator such as ">=18") to a question id.
    jump_logic: Dict[str, str] = Field(default_factory=dict)


class SurveyCreate(BaseModel):
    title: str = Field(min_length=1, max_length=200)
    description: str = ""
    is_anonymous: bool = False
    end_time: Optional[datetime] = None
    questions: List[Question] = Field(default_factory=list)
    question_ids: List[str] = Field(default_factory=list)


class QuestionCreate(Question):
    pass


class QuestionVersionCreate(Question):
    pass


class QuestionShare(BaseModel):
    usernames: List[str] = Field(min_length=1)


class QuestionBankCreate(BaseModel):
    name: str = Field(min_length=1, max_length=100)


class QuestionBankItems(BaseModel):
    question_ids: List[str] = Field(default_factory=list)


class SurveyUpdate(BaseModel):
    status: Literal["open", "closed"]


class Submission(BaseModel):
    answers: Dict[str, Any]


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _dump(model: BaseModel) -> dict:
    return model.model_dump() if hasattr(model, "model_dump") else model.dict()


def _public_survey(doc: dict) -> dict:
    result = {key: value for key, value in doc.items() if key != "_id"}
    result["survey_id"] = str(doc["_id"])
    return result


def _get_user(credentials: Optional[HTTPAuthorizationCredentials] = Depends(bearer)):
    if credentials is None:
        raise HTTPException(status_code=401, detail="请先登录", headers={"WWW-Authenticate": "Bearer"})
    try:
        payload = jwt.decode(credentials.credentials, SECRET_KEY, algorithms=[ALGORITHM])
        user_id = payload.get("sub")
        if not user_id or not ObjectId.is_valid(user_id):
            raise ValueError("invalid subject")
    except (JWTError, ValueError):
        raise HTTPException(status_code=401, detail="登录已失效，请重新登录", headers={"WWW-Authenticate": "Bearer"})
    user = users_collection.find_one({"_id": ObjectId(user_id)})
    if not user:
        raise HTTPException(status_code=401, detail="用户不存在")
    return user


def _owner_survey(survey_id: str, user: dict) -> dict:
    if not ObjectId.is_valid(survey_id):
        raise HTTPException(status_code=404, detail="问卷不存在")
    survey = surveys_collection.find_one({"_id": ObjectId(survey_id), "creator_id": str(user["_id"])})
    if not survey:
        raise HTTPException(status_code=404, detail="问卷不存在")
    return survey


def _question_accessible(question: dict, user: dict) -> bool:
    return question.get("owner_id") == str(user["_id"]) or str(user["_id"]) in question.get("shared_with", [])


def _question_public(question: dict) -> dict:
    return {**{k: v for k, v in question.items() if k != "_id"}, "question_id": str(question["_id"])}


def _get_accessible_question(question_id: str, user: dict) -> dict:
    if not ObjectId.is_valid(question_id):
        raise HTTPException(status_code=404, detail="题目不存在")
    question = questions_collection.find_one({"_id": ObjectId(question_id)})
    if not question or not _question_accessible(question, user):
        raise HTTPException(status_code=404, detail="题目不存在或无权使用")
    return question


def _check_survey_questions(questions: List[Question]) -> None:
    ids = [q.id for q in questions]
    if len(set(ids)) != len(ids):
        raise HTTPException(status_code=422, detail="题目 id 不能重复")
    for q in questions:
        if q.type in ("single_choice", "multiple_choice") and not q.options:
            raise HTTPException(status_code=422, detail=f"选择题 {q.id} 至少需要一个选项")
        if len(set(q.options)) != len(q.options):
            raise HTTPException(status_code=422, detail=f"题目 {q.id} 的选项不能重复")
        for target in q.jump_logic.values():
            if target not in ids and target != "end":
                raise HTTPException(status_code=422, detail=f"题目 {q.id} 的跳转目标 {target} 不存在")


def _check_question_definition(question: Question) -> None:
    if question.type in ("single_choice", "multiple_choice") and not question.options:
        raise HTTPException(status_code=422, detail=f"选择题 {question.id} 至少需要一个选项")
    if len(set(question.options)) != len(question.options):
        raise HTTPException(status_code=422, detail=f"题目 {question.id} 的选项不能重复")


def _matches_rule(answer: Any, rule: str) -> bool:
    if rule.startswith((">=", "<=", ">", "<", "==", "!=")):
        import re
        match = re.fullmatch(r"(>=|<=|==|!=|>|<)\s*(-?\d+(?:\.\d+)?)", rule)
        if not match:
            return False
        try:
            a, b = float(answer), float(match.group(2))
        except (TypeError, ValueError):
            return False
        return {">=": a >= b, "<=": a <= b, ">": a > b, "<": a < b, "==": a == b, "!=": a != b}[match.group(1)]
    return rule == answer or (isinstance(answer, list) and rule in answer)


def _validate_answers(survey: dict, answers: dict) -> dict:
    questions = survey["questions"]
    by_id = {q["id"]: q for q in questions}
    unknown = set(answers) - set(by_id)
    if unknown:
        raise HTTPException(status_code=422, detail=f"包含未知题目：{', '.join(sorted(unknown))}")

    # Follow configured jumps from the first question; only visible questions are validated/stored.
    visible, current, visited = [], questions[0]["id"], set()
    while current != "end":
        if current in visited:
            raise HTTPException(status_code=422, detail="跳转逻辑形成循环")
        visited.add(current)
        visible.append(current)
        q = by_id[current]
        answer = answers.get(current)
        target = next((dest for rule, dest in q.get("jump_logic", {}).items() if _matches_rule(answer, rule)), None)
        if target is None:
            index = next(i for i, item in enumerate(questions) if item["id"] == current)
            target = questions[index + 1]["id"] if index + 1 < len(questions) else "end"
        current = target

    result = {}
    for qid in visible:
        q, answer = by_id[qid], answers.get(qid)
        if answer is None or answer == "" or (q.get("type") == "multiple_choice" and answer == []):
            if q.get("required", True):
                raise HTTPException(status_code=422, detail=f"必答题未填写：{q['title']}")
            continue
        kind, options, rules = q["type"], q.get("options", []), q.get("validation", {})
        if kind == "single_choice" and (not isinstance(answer, str) or answer not in options):
            raise HTTPException(status_code=422, detail=f"题目 {q['title']} 的选项无效")
        if kind == "multiple_choice":
            if not isinstance(answer, list) or any(not isinstance(a, str) or a not in options for a in answer) or len(set(answer)) != len(answer):
                raise HTTPException(status_code=422, detail=f"题目 {q['title']} 的选择无效")
            count = len(answer)
            if count < rules.get("min_selections", 0) or count > rules.get("max_selections", len(options)):
                raise HTTPException(status_code=422, detail=f"题目 {q['title']} 的选择数量不符合要求")
            if "exact_selections" in rules and count != rules["exact_selections"]:
                raise HTTPException(status_code=422, detail=f"题目 {q['title']} 必须选择 {rules['exact_selections']} 项")
        elif kind == "text":
            if not isinstance(answer, str):
                raise HTTPException(status_code=422, detail=f"题目 {q['title']} 需要填写文字")
            if len(answer) < rules.get("min_length", 0) or len(answer) > rules.get("max_length", 10000):
                raise HTTPException(status_code=422, detail=f"题目 {q['title']} 的字数不符合要求")
        elif kind == "number":
            if isinstance(answer, bool) or not isinstance(answer, (int, float)):
                raise HTTPException(status_code=422, detail=f"题目 {q['title']} 需要填写数字")
            if rules.get("integer", False) and not float(answer).is_integer():
                raise HTTPException(status_code=422, detail=f"题目 {q['title']} 必须是整数")
            if answer < rules.get("min", float("-inf")) or answer > rules.get("max", float("inf")):
                raise HTTPException(status_code=422, detail=f"题目 {q['title']} 超出数字范围")
        result[qid] = answer
    return result


@app.get("/")
def read_root():
    return {"message": "问卷系统后端 API 运行成功！"}


@app.post("/api/register", status_code=201)
def register(user: UserCredentials):
    if users_collection.find_one({"username": user.username}):
        raise HTTPException(status_code=400, detail="用户名已存在")
    users_collection.insert_one({"username": user.username, "password_hash": pwd_context.hash(user.password), "created_at": _now()})
    return {"message": "注册成功", "username": user.username}


@app.post("/api/login")
def login(credentials: UserCredentials):
    user = users_collection.find_one({"username": credentials.username})
    if not user or not pwd_context.verify(credentials.password, user["password_hash"]):
        raise HTTPException(status_code=401, detail="用户名或密码错误")
    token = jwt.encode({"sub": str(user["_id"]), "exp": _now() + timedelta(hours=TOKEN_HOURS)}, SECRET_KEY, algorithm=ALGORITHM)
    return {"access_token": token, "token_type": "bearer", "expires_in": TOKEN_HOURS * 3600}


@app.get("/api/me")
def current_user(user: dict = Depends(_get_user)):
    return {"user_id": str(user["_id"]), "username": user["username"]}


@app.post("/api/questions", status_code=201)
def create_question(question: QuestionCreate, user: dict = Depends(_get_user)):
    data = _dump(question)
    _check_question_definition(question)
    now = _now()
    doc = {**data, "owner_id": str(user["_id"]), "shared_with": [], "version": 1,
           "lineage_id": None, "parent_question_id": None, "created_at": now}
    result = questions_collection.insert_one(doc)
    questions_collection.update_one({"_id": result.inserted_id}, {"$set": {"lineage_id": str(result.inserted_id)}})
    doc["_id"] = result.inserted_id
    doc["lineage_id"] = str(result.inserted_id)
    return _question_public(doc)


@app.get("/api/questions")
def list_questions(user: dict = Depends(_get_user)):
    uid = str(user["_id"])
    docs = questions_collection.find({"$or": [{"owner_id": uid}, {"shared_with": uid}]}).sort([("lineage_id", 1), ("version", -1)])
    return [_question_public(dict(q)) for q in docs]


@app.get("/api/questions/{question_id}")
def get_question(question_id: str, user: dict = Depends(_get_user)):
    return _question_public(_get_accessible_question(question_id, user))


@app.post("/api/questions/{question_id}/versions", status_code=201)
def create_question_version(question_id: str, question: QuestionVersionCreate, user: dict = Depends(_get_user)):
    previous = _get_accessible_question(question_id, user)
    if previous.get("owner_id") != str(user["_id"]):
        raise HTTPException(status_code=403, detail="只有题目所有者可以创建新版本")
    _check_question_definition(question)
    lineage = previous.get("lineage_id") or str(previous["_id"])
    latest = questions_collection.find_one({"lineage_id": lineage}, sort=[("version", -1)])
    doc = {**_dump(question), "owner_id": str(user["_id"]), "shared_with": list(previous.get("shared_with", [])),
           "version": int(latest.get("version", 1)) + 1, "lineage_id": lineage,
           "parent_question_id": str(previous["_id"]), "created_at": _now()}
    result = questions_collection.insert_one(doc)
    doc["_id"] = result.inserted_id
    return _question_public(doc)


@app.get("/api/questions/{question_id}/history")
def question_history(question_id: str, user: dict = Depends(_get_user)):
    question = _get_accessible_question(question_id, user)
    lineage = question.get("lineage_id") or str(question["_id"])
    return [_question_public(dict(q)) for q in questions_collection.find({"lineage_id": lineage}).sort("version", 1)]


@app.post("/api/questions/{question_id}/restore/{version_id}", status_code=201)
def restore_question_version(question_id: str, version_id: str, user: dict = Depends(_get_user)):
    current = _get_accessible_question(question_id, user)
    if current.get("owner_id") != str(user["_id"]):
        raise HTTPException(status_code=403, detail="只有题目所有者可以恢复版本")
    old = _get_accessible_question(version_id, user)
    lineage = current.get("lineage_id") or str(current["_id"])
    if old.get("lineage_id") != lineage:
        raise HTTPException(status_code=400, detail="目标版本不属于该题目历史")
    fields = {k: old[k] for k in ("id", "title", "type", "options", "required", "validation", "jump_logic")}
    return create_question_version(question_id, QuestionVersionCreate(**fields), user)


@app.put("/api/questions/{question_id}/share")
def share_question(question_id: str, share: QuestionShare, user: dict = Depends(_get_user)):
    question = _get_accessible_question(question_id, user)
    if question.get("owner_id") != str(user["_id"]):
        raise HTTPException(status_code=403, detail="只有题目所有者可以共享题目")
    users = list(users_collection.find({"username": {"$in": share.usernames}}))
    found = {u["username"] for u in users}
    missing = sorted(set(share.usernames) - found)
    if missing:
        raise HTTPException(status_code=404, detail=f"用户不存在：{', '.join(missing)}")
    ids = [str(u["_id"]) for u in users if str(u["_id"]) != str(user["_id"])]
    questions_collection.update_many({"lineage_id": question.get("lineage_id", str(question["_id"]))}, {"$addToSet": {"shared_with": {"$each": ids}}})
    return {"question_id": question_id, "shared_with": share.usernames}


@app.get("/api/questions/{question_id}/usage")
def question_usage(question_id: str, user: dict = Depends(_get_user)):
    question = _get_accessible_question(question_id, user)
    lineage = question.get("lineage_id") or str(question["_id"])
    versions = [str(q["_id"]) for q in questions_collection.find({"lineage_id": lineage})]
    surveys = surveys_collection.find({"questions.source_question_id": {"$in": versions}})
    result = []
    for survey in surveys:
        matching = [q for q in survey.get("questions", []) if q.get("source_question_id") in versions]
        result.append({"survey_id": str(survey["_id"]), "title": survey["title"], "status": survey["status"],
                       "question_ids": [q["id"] for q in matching], "versions": [q.get("source_question_version") for q in matching]})
    return result


@app.get("/api/questions/{question_id}/statistics")
def shared_question_statistics(question_id: str, user: dict = Depends(_get_user)):
    question = _get_accessible_question(question_id, user)
    kind = question["type"]
    lineage = question.get("lineage_id") or str(question["_id"])
    versions = list(questions_collection.find({"lineage_id": lineage}))
    ids = {str(q["_id"]) for q in versions}
    values = []
    survey_ids = set()
    for response in responses_collection.find({}):
        survey = surveys_collection.find_one({"_id": ObjectId(response["survey_id"])} if ObjectId.is_valid(response.get("survey_id", "")) else {"_id": None})
        if not survey:
            continue
        for snapshot in survey.get("questions", []):
            if snapshot.get("source_question_id") in ids and snapshot.get("type") == kind and snapshot["id"] in response.get("answers", {}):
                values.append((snapshot, response["answers"][snapshot["id"]]))
                survey_ids.add(response["survey_id"])
    result = {"lineage_id": lineage, "question_title": question["title"], "type": kind,
              "answered": len(values), "survey_count": len(survey_ids), "versions": sorted({x[0].get("source_question_version", 1) for x in values})}
    if kind in ("single_choice", "multiple_choice"):
        options = list(dict.fromkeys(option for q, _ in values for option in q.get("options", [])))
        result["options"] = {option: sum((value == option if isinstance(value, str) else option in value) for _, value in values) for option in options}
    elif kind == "text":
        result["responses"] = [value for _, value in values]
    else:
        numbers = [value for _, value in values]
        result["values"] = numbers
        result["average"] = sum(numbers) / len(numbers) if numbers else None
        result["distribution"] = {str(value): numbers.count(value) for value in sorted(set(numbers))}
    return result


@app.post("/api/question-banks", status_code=201)
def create_question_bank(bank: QuestionBankCreate, user: dict = Depends(_get_user)):
    if question_banks_collection.find_one({"owner_id": str(user["_id"]), "name": bank.name}):
        raise HTTPException(status_code=400, detail="题库名称已存在")
    result = question_banks_collection.insert_one({"owner_id": str(user["_id"]), "name": bank.name, "question_ids": [], "created_at": _now()})
    return {"bank_id": str(result.inserted_id), "name": bank.name, "question_ids": []}


@app.get("/api/question-banks")
def list_question_banks(user: dict = Depends(_get_user)):
    return [{**{k: v for k, v in b.items() if k != "_id"}, "bank_id": str(b["_id"])} for b in question_banks_collection.find({"owner_id": str(user["_id"])})]


@app.put("/api/question-banks/{bank_id}/questions")
def update_question_bank(bank_id: str, items: QuestionBankItems, user: dict = Depends(_get_user)):
    if not ObjectId.is_valid(bank_id):
        raise HTTPException(status_code=404, detail="题库不存在")
    bank = question_banks_collection.find_one({"_id": ObjectId(bank_id), "owner_id": str(user["_id"])})
    if not bank:
        raise HTTPException(status_code=404, detail="题库不存在")
    for qid in items.question_ids:
        _get_accessible_question(qid, user)
    question_banks_collection.update_one({"_id": ObjectId(bank_id)}, {"$set": {"question_ids": list(dict.fromkeys(items.question_ids))}})
    return {"bank_id": bank_id, "question_ids": list(dict.fromkeys(items.question_ids))}


@app.post("/api/question-banks/{bank_id}/questions/{question_id}/update")
def update_question_bank_version(bank_id: str, question_id: str, user: dict = Depends(_get_user)):
    if not ObjectId.is_valid(bank_id):
        raise HTTPException(status_code=404, detail="题库不存在")
    bank = question_banks_collection.find_one({"_id": ObjectId(bank_id), "owner_id": str(user["_id"])})
    if not bank:
        raise HTTPException(status_code=404, detail="题库不存在")
    old = _get_accessible_question(question_id, user)
    lineage = old.get("lineage_id") or str(old["_id"])
    bank_question_ids = bank.get("question_ids", [])
    member_versions = list(questions_collection.find({"_id": {"$in": [ObjectId(qid) for qid in bank_question_ids if ObjectId.is_valid(qid)]}, "lineage_id": lineage}))
    if not member_versions:
        raise HTTPException(status_code=404, detail="该题目版本链不在题库中")
    latest = questions_collection.find_one({"lineage_id": lineage}, sort=[("version", -1)])
    if not latest:
        raise HTTPException(status_code=404, detail="题目版本不存在")
    member_ids = {str(q["_id"]) for q in member_versions}
    ids = [qid for qid in bank_question_ids if qid not in member_ids]
    if str(latest["_id"]) not in ids:
        ids.append(str(latest["_id"]))
    question_banks_collection.update_one({"_id": ObjectId(bank_id)}, {"$set": {"question_ids": ids}})
    return {"bank_id": bank_id, "question_id": str(latest["_id"]), "version": latest.get("version", 1)}


@app.get("/api/question-banks/{bank_id}/questions")
def get_question_bank_items(bank_id: str, user: dict = Depends(_get_user)):
    if not ObjectId.is_valid(bank_id):
        raise HTTPException(status_code=404, detail="题库不存在")
    bank = question_banks_collection.find_one({"_id": ObjectId(bank_id), "owner_id": str(user["_id"])})
    if not bank:
        raise HTTPException(status_code=404, detail="题库不存在")
    return [_question_public(q) for q in questions_collection.find({"_id": {"$in": [ObjectId(qid) for qid in bank["question_ids"] if ObjectId.is_valid(qid)]}})]


@app.post("/api/surveys", status_code=201)
def create_survey(survey: SurveyCreate, user: dict = Depends(_get_user)):
    doc = _dump(survey)
    if not survey.questions and not survey.question_ids:
        raise HTTPException(status_code=422, detail="问卷至少需要一道题目")
    selected = []
    for question_id in survey.question_ids:
        source = _get_accessible_question(question_id, user)
        selected.append({key: source.get(key) for key in ("id", "title", "type", "options", "required", "validation", "jump_logic")} | {
            "source_question_id": str(source["_id"]), "source_question_version": source.get("version", 1)})
    doc["questions"] = [_dump(q) for q in survey.questions] + selected
    _check_survey_questions([Question(**{key: q[key] for key in ("id", "title", "type", "options", "required", "validation", "jump_logic")}) for q in doc["questions"]])
    doc.pop("question_ids", None)
    doc.update(creator_id=str(user["_id"]), status="open", created_at=_now())
    result = surveys_collection.insert_one(doc)
    return {"message": "问卷创建成功", "survey_id": str(result.inserted_id), "access_link": f"/survey/{result.inserted_id}"}


@app.get("/api/surveys")
def list_surveys(user: dict = Depends(_get_user)):
    return [_public_survey(dict(s)) for s in surveys_collection.find({"creator_id": str(user["_id"])}).sort("created_at", -1)]


@app.get("/api/surveys/{survey_id}")
def get_survey(survey_id: str, user: dict = Depends(_get_user)):
    return _public_survey(dict(_owner_survey(survey_id, user)))


@app.patch("/api/surveys/{survey_id}")
def update_survey(survey_id: str, update: SurveyUpdate, user: dict = Depends(_get_user)):
    _owner_survey(survey_id, user)
    surveys_collection.update_one({"_id": ObjectId(survey_id)}, {"$set": {"status": update.status}})
    return {"survey_id": survey_id, "status": update.status}


@app.get("/survey/{survey_id}")
def public_survey(survey_id: str, _user: dict = Depends(_get_user)):
    """A respondent must authenticate; anonymous means identity is not stored, not no login."""
    if not ObjectId.is_valid(survey_id):
        raise HTTPException(status_code=404, detail="问卷不存在")
    doc = surveys_collection.find_one({"_id": ObjectId(survey_id)})
    if not doc:
        raise HTTPException(status_code=404, detail="问卷不存在")
    if doc["status"] != "open" or (doc.get("end_time") and doc["end_time"] <= _now()):
        raise HTTPException(status_code=410, detail="问卷已关闭或已截止")
    return {"survey_id": survey_id, "title": doc["title"], "description": doc["description"], "questions": doc["questions"]}


@app.post("/survey/{survey_id}/responses", status_code=201)
def submit_response(survey_id: str, submission: Submission, user: dict = Depends(_get_user)):
    if not ObjectId.is_valid(survey_id):
        raise HTTPException(status_code=404, detail="问卷不存在")
    survey = surveys_collection.find_one({"_id": ObjectId(survey_id)})
    if not survey:
        raise HTTPException(status_code=404, detail="问卷不存在")
    if survey["status"] != "open" or (survey.get("end_time") and survey["end_time"] <= _now()):
        raise HTTPException(status_code=410, detail="问卷已关闭或已截止")
    answers = _validate_answers(survey, submission.answers)
    doc = {"survey_id": survey_id, "answers": answers, "submitted_at": _now()}
    if not survey.get("is_anonymous", False):
        doc["respondent_id"] = str(user["_id"])
    result = responses_collection.insert_one(doc)
    return {"message": "提交成功", "response_id": str(result.inserted_id)}


@app.get("/api/surveys/{survey_id}/statistics")
def survey_statistics(survey_id: str, user: dict = Depends(_get_user)):
    survey = _owner_survey(survey_id, user)
    responses = list(responses_collection.find({"survey_id": survey_id}))
    question_stats = {}
    for q in survey["questions"]:
        values = [r.get("answers", {}).get(q["id"]) for r in responses if q["id"] in r.get("answers", {})]
        stat = {"answered": len(values), "type": q["type"]}
        if q["type"] in ("single_choice", "multiple_choice"):
            stat["options"] = {option: sum((value == option if isinstance(value, str) else option in value) for value in values) for option in q.get("options", [])}
        elif q["type"] == "text":
            stat["responses"] = values
        else:
            stat["values"] = values
            stat["average"] = sum(values) / len(values) if values else None
        question_stats[q["id"]] = stat
    return {"survey_id": survey_id, "total_submissions": len(responses), "questions": question_stats}


@app.get("/api/surveys/{survey_id}/questions/{question_id}/statistics")
def question_statistics(survey_id: str, question_id: str, user: dict = Depends(_get_user)):
    result = survey_statistics(survey_id, user)
    if question_id not in result["questions"]:
        raise HTTPException(status_code=404, detail="题目不存在")
    return {"survey_id": survey_id, "question_id": question_id, **result["questions"][question_id]}





