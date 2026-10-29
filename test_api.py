"""API regression tests using an isolated in-memory MongoDB substitute."""
import mongomock
import pytest
from fastapi.testclient import TestClient

import main


@pytest.fixture
def client(monkeypatch):
    db = mongomock.MongoClient().questionnaire_db
    monkeypatch.setattr(main, "users_collection", db.users)
    monkeypatch.setattr(main, "surveys_collection", db.surveys)
    monkeypatch.setattr(main, "responses_collection", db.responses)
    return TestClient(main.app)


def account(client, username="alice"):
    assert client.post("/api/register", json={"username": username, "password": "password123"}).status_code == 201
    response = client.post("/api/login", json={"username": username, "password": "password123"})
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


def sample_survey():
    return {"title": "测试问卷", "description": "", "is_anonymous": False, "questions": [
        {"id": "q1", "title": "年龄", "type": "number", "validation": {"integer": True, "min": 0, "max": 120}, "jump_logic": {"<18": "q3", ">=18": "q2"}},
        {"id": "q2", "title": "水果", "type": "multiple_choice", "options": ["苹果", "香蕉"], "validation": {"min_selections": 1, "max_selections": 2}},
        {"id": "q3", "title": "说明", "type": "text", "required": False, "validation": {"max_length": 30}}
    ]}


def create(client, headers):
    response = client.post("/api/surveys", headers=headers, json=sample_survey())
    assert response.status_code == 201
    return response.json()["survey_id"]


def test_register_login_and_duplicate(client):
    headers = account(client)
    assert "Bearer" in headers["Authorization"]
    assert client.post("/api/register", json={"username": "alice", "password": "password123"}).status_code == 400


def test_create_and_owner_isolation(client):
    owner = account(client)
    survey_id = create(client, owner)
    assert len(client.get("/api/surveys", headers=owner).json()) == 1
    other = account(client, "bob")
    assert client.get(f"/api/surveys/{survey_id}", headers=other).status_code == 404


def test_required_and_number_validation(client):
    headers = account(client)
    survey_id = create(client, headers)
    assert client.post(f"/survey/{survey_id}/responses", headers=headers, json={"answers": {}}).status_code == 422
    assert client.post(f"/survey/{survey_id}/responses", headers=headers, json={"answers": {"q1": 150}}).status_code == 422
    assert client.post(f"/survey/{survey_id}/responses", headers=headers, json={"answers": {"q1": 16, "q3": "x"}}).status_code == 201


def test_jump_path_and_choice_validation(client):
    headers = account(client)
    survey_id = create(client, headers)
    # q1=20 routes to q2; q2 is required and must have a selection.
    assert client.post(f"/survey/{survey_id}/responses", headers=headers, json={"answers": {"q1": 20}}).status_code == 422
    assert client.post(f"/survey/{survey_id}/responses", headers=headers, json={"answers": {"q1": 20, "q2": ["bad"]}}).status_code == 422
    # q1=16 skips q2; q2 answer is excluded from the saved response.
    assert client.post(f"/survey/{survey_id}/responses", headers=headers, json={"answers": {"q1": 16, "q3": "ok", "q2": ["苹果"]}}).status_code == 201


def test_submission_and_statistics(client):
    headers = account(client)
    survey_id = create(client, headers)
    response = client.post(f"/survey/{survey_id}/responses", headers=headers, json={"answers": {"q1": 20, "q2": ["苹果"]}})
    assert response.status_code == 201
    stats = client.get(f"/api/surveys/{survey_id}/statistics", headers=headers).json()
    assert stats["total_submissions"] == 1
    assert stats["questions"]["q2"]["options"]["苹果"] == 1
    assert stats["questions"]["q1"]["average"] == 20
    assert client.get(f"/api/surveys/{survey_id}/questions/q2/statistics", headers=headers).json()["answered"] == 1


def test_close_survey_prevents_submission(client):
    headers = account(client)
    survey_id = create(client, headers)
    assert client.patch(f"/api/surveys/{survey_id}", headers=headers, json={"status": "closed"}).status_code == 200
    assert client.post(f"/survey/{survey_id}/responses", headers=headers, json={"answers": {"q1": 16}}).status_code == 410


def test_reusable_question_version_sharing_snapshot_and_analytics(client):
    owner = account(client)
    respondent = account(client, "bob")
    definition = {"id": "age", "title": "年龄", "type": "number", "required": True,
                  "validation": {"integer": True, "min": 0, "max": 120}}
    created = client.post("/api/questions", headers=owner, json=definition)
    assert created.status_code == 201
    first_id = created.json()["question_id"]

    survey = client.post("/api/surveys", headers=owner, json={"title": "旧版问卷", "question_ids": [first_id]})
    assert survey.status_code == 201
    survey_id = survey.json()["survey_id"]

    changed = {**definition, "title": "你的年龄"}
    second = client.post(f"/api/questions/{first_id}/versions", headers=owner, json=changed)
    assert second.status_code == 201
    second_id = second.json()["question_id"]
    assert second.json()["version"] == 2
    assert second.json()["parent_question_id"] == first_id

    snapshot = client.get(f"/api/surveys/{survey_id}", headers=owner).json()["questions"][0]
    assert snapshot["title"] == "年龄"
    assert snapshot["source_question_id"] == first_id
    assert snapshot["source_question_version"] == 1

    shared = client.put(f"/api/questions/{first_id}/share", headers=owner, json={"usernames": ["bob"]})
    assert shared.status_code == 200
    assert client.get(f"/api/questions/{second_id}", headers=respondent).status_code == 200
    assert client.post(f"/api/questions/{second_id}/versions", headers=respondent, json=changed).status_code == 403

    submission = client.post(f"/survey/{survey_id}/responses", headers=respondent, json={"answers": {"age": 20}})
    assert submission.status_code == 201
    second_survey = client.post("/api/surveys", headers=owner,
                                 json={"title": "新版本问卷", "question_ids": [second_id]})
    assert second_survey.status_code == 201
    second_survey_id = second_survey.json()["survey_id"]
    second_submission = client.post(f"/survey/{second_survey_id}/responses", headers=respondent,
                                    json={"answers": {"age": 30}})
    assert second_submission.status_code == 201
    stats = client.get(f"/api/questions/{second_id}/statistics", headers=owner)
    assert stats.status_code == 200
    assert stats.json()["answered"] == 2
    assert stats.json()["survey_count"] == 2
    assert stats.json()["average"] == 25
    assert stats.json()["distribution"] == {"20": 1, "30": 1}
    usage = client.get(f"/api/questions/{second_id}/usage", headers=owner)
    assert usage.status_code == 200
    assert {item["survey_id"] for item in usage.json()} == {survey_id, second_survey_id}

    history = client.get(f"/api/questions/{second_id}/history", headers=owner)
    assert history.status_code == 200
    assert [item["version"] for item in history.json()] == [1, 2]
    restored = client.post(f"/api/questions/{second_id}/restore/{first_id}", headers=owner)
    assert restored.status_code == 201
    assert restored.json()["version"] == 3
    assert restored.json()["title"] == "年龄"


def test_question_bank_add_and_update_to_latest_version(client):
    owner = account(client)
    definition = {"id": "q1", "title": "题目 v1", "type": "text"}
    first = client.post("/api/questions", headers=owner, json=definition)
    assert first.status_code == 201
    first_id = first.json()["question_id"]
    second = client.post(f"/api/questions/{first_id}/versions", headers=owner,
                         json={**definition, "title": "题目 v2"})
    assert second.status_code == 201

    bank = client.post("/api/question-banks", headers=owner, json={"name": "常用题目"})
    assert bank.status_code == 201
    bank_id = bank.json()["bank_id"]
    added = client.put(f"/api/question-banks/{bank_id}/questions", headers=owner,
                       json={"question_ids": [first_id]})
    assert added.status_code == 200
    updated = client.post(f"/api/question-banks/{bank_id}/questions/{first_id}/update", headers=owner)
    assert updated.status_code == 200
    assert updated.json()["question_id"] == second.json()["question_id"]
    items = client.get(f"/api/question-banks/{bank_id}/questions", headers=owner)
    assert items.status_code == 200
    assert [item["question_id"] for item in items.json()] == [second.json()["question_id"]]



