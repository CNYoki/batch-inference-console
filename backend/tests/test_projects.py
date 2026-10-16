"""项目：增删改查、只对本人可见，以及任务 / Prompt 归类。不需要 Redis。"""
from __future__ import annotations

import uuid

import pytest
from httpx import AsyncClient


async def _job(name: str, username: str = "admin", **kw) -> str:
    from sqlalchemy import select

    from app.db import session_scope
    from app.models import Job, JobStatus, User

    async with session_scope() as db:
        user = (await db.execute(select(User).where(User.username == username))).scalar_one()
        job = Job(name=name, user_id=user.id, status=kw.pop("status", JobStatus.succeeded),
                  input_filename="a.jsonl", input_path="/tmp/none.jsonl", **kw)
        db.add(job)
        await db.flush()
        return job.id


async def _drop_jobs(ids: list[str]) -> None:
    from app.db import session_scope
    from app.models import Job

    async with session_scope() as db:
        for jid in ids:
            job = await db.get(Job, jid)
            if job:
                await db.delete(job)


async def _user_client(admin_client: AsyncClient, username: str) -> AsyncClient:
    await admin_client.post("/api/admin/users", json={
        "username": username, "password": "UserPass123!", "role": "user",
    })
    c = AsyncClient(transport=admin_client._transport, base_url="http://test")
    resp = await c.post("/api/auth/login", json={"username": username, "password": "UserPass123!"})
    assert resp.status_code == 200, resp.text
    return c


@pytest.mark.asyncio
async def test_project_crud_and_counts(admin_client: AsyncClient):
    name = f"项目-{uuid.uuid4().hex[:6]}"
    resp = await admin_client.post("/api/projects", json={"name": f"  {name}  ", "description": "说明"})
    assert resp.status_code == 201, resp.text
    project = resp.json()
    assert project["name"] == name and project["job_count"] == 0

    # 同名 409
    assert (await admin_client.post("/api/projects", json={"name": name})).status_code == 409
    assert (await admin_client.post("/api/projects", json={"name": "   "})).status_code == 422

    from app.models import JobStatus

    done = await _job("项目里的任务", project_id=project["id"])
    running = await _job("项目里在跑的", project_id=project["id"], status=JobStatus.running)
    await admin_client.post("/api/prompts", json={
        "name": f"p-{name}", "prompt_template": "{{x}}", "variables": [{"name": "x", "field": "x"}],
        "project_id": project["id"],
    })

    got = (await admin_client.get(f"/api/projects/{project['id']}")).json()
    assert (got["job_count"], got["active_job_count"], got["prompt_count"]) == (2, 1, 1)
    assert project["id"] in {p["id"] for p in (await admin_client.get("/api/projects")).json()}

    updated = (await admin_client.patch(f"/api/projects/{project['id']}",
                                        json={"name": f"{name}-改", "description": None})).json()
    assert updated["name"] == f"{name}-改" and updated["description"] is None

    # 删项目不删内容：任务回到默认项目，Prompt 变成未归类
    assert (await admin_client.delete(f"/api/projects/{project['id']}")).status_code == 204
    assert (await admin_client.get(f"/api/projects/{project['id']}")).status_code == 404
    default = (await admin_client.get("/api/projects")).json()[0]
    assert default["is_default"] and default["name"] == "默认项目"
    job = (await admin_client.get(f"/api/jobs/{done}")).json()
    assert job["project_id"] == default["id"] and job["name"] == "项目里的任务"
    prompts = (await admin_client.get("/api/prompts", params={"project": "none"})).json()
    assert f"p-{name}" in {p["name"] for p in prompts}

    await _drop_jobs([done, running])


@pytest.mark.asyncio
async def test_jobs_filter_and_move_between_projects(admin_client: AsyncClient):
    a = (await admin_client.post("/api/projects", json={"name": f"A-{uuid.uuid4().hex[:6]}"})).json()
    b = (await admin_client.post("/api/projects", json={"name": f"B-{uuid.uuid4().hex[:6]}"})).json()
    j1 = await _job("j1", project_id=a["id"])
    j2 = await _job("j2")
    j3 = await _job("j3")

    listed = (await admin_client.get("/api/jobs", params={"project": a["id"]})).json()
    assert [j["id"] for j in listed["items"]] == [j1]
    assert listed["items"][0]["project_name"] == a["name"]
    unassigned = {j["id"] for j in (await admin_client.get(
        "/api/jobs", params={"project": "none", "page_size": 100})).json()["items"]}
    assert {j2, j3} <= unassigned and j1 not in unassigned

    # 单个挪动；显式传 null 是挪回默认项目，不传就不改
    out = (await admin_client.patch(f"/api/jobs/{j2}", json={"project_id": b["id"]})).json()
    assert out["project_id"] == b["id"] and out["project_name"] == b["name"]
    out = (await admin_client.patch(f"/api/jobs/{j2}", json={"name": "j2-改"})).json()
    assert out["project_id"] == b["id"]
    out = (await admin_client.patch(f"/api/jobs/{j2}", json={"project_id": None})).json()
    assert out["project_name"] == "默认项目"
    assert (await admin_client.patch(f"/api/jobs/{j2}", json={"project_id": "nope"})).status_code == 400

    # 批量
    resp = await admin_client.post("/api/jobs/batch-project",
                                   json={"job_ids": [j1, j2, j3], "project_id": b["id"]})
    assert resp.json() == {"updated": 3}
    listed = (await admin_client.get("/api/jobs", params={"project": b["id"]})).json()
    assert {j["id"] for j in listed["items"]} == {j1, j2, j3}

    await _drop_jobs([j1, j2, j3])
    for p in (a, b):
        await admin_client.delete(f"/api/projects/{p['id']}")


@pytest.mark.asyncio
async def test_projects_are_private(admin_client: AsyncClient):
    mine = (await admin_client.post("/api/projects", json={"name": f"私有-{uuid.uuid4().hex[:6]}"})).json()
    other = await _user_client(admin_client, "projuser")
    try:
        assert (await other.get(f"/api/projects/{mine['id']}")).status_code == 404
        assert mine["id"] not in {p["id"] for p in (await other.get("/api/projects")).json()}
        assert (await other.delete(f"/api/projects/{mine['id']}")).status_code == 404

        # 不能把自己的东西放进别人的项目
        resp = await other.post("/api/prompts", json={
            "name": "x", "prompt_template": "{{x}}", "variables": [{"name": "x", "field": "x"}],
            "project_id": mine["id"],
        })
        assert resp.status_code == 400
        their_job = await _job("别人的任务", username="projuser")
        resp = await other.post("/api/jobs/batch-project",
                                json={"job_ids": [their_job], "project_id": mine["id"]})
        assert resp.status_code == 400
        # 批量接口只动自己的任务
        my_job = await _job("我的任务")
        theirs = (await other.post("/api/projects", json={"name": "他的项目"})).json()
        resp = await other.post("/api/jobs/batch-project",
                                json={"job_ids": [my_job, their_job], "project_id": theirs["id"]})
        assert resp.json() == {"updated": 1}
        # 管理员替别人归类，只能用对方自己的项目
        assert (await admin_client.patch(f"/api/jobs/{their_job}",
                                          json={"project_id": mine["id"]})).status_code == 400
        out = (await admin_client.patch(f"/api/jobs/{their_job}", json={"project_id": theirs["id"]})).json()
        assert out["project_name"] == "他的项目"
        await _drop_jobs([their_job, my_job])
    finally:
        await other.aclose()
        await admin_client.delete(f"/api/projects/{mine['id']}")


@pytest.mark.asyncio
async def test_prompt_project_assignment(admin_client: AsyncClient):
    project = (await admin_client.post("/api/projects", json={"name": f"P-{uuid.uuid4().hex[:6]}"})).json()
    body = {"name": f"pp-{uuid.uuid4().hex[:6]}", "prompt_template": "{{x}}",
            "variables": [{"name": "x", "field": "x"}], "project_id": project["id"]}
    created = (await admin_client.post("/api/prompts", json=body)).json()
    assert created["project_id"] == project["id"]
    listed = (await admin_client.get("/api/prompts", params={"project": project["id"]})).json()
    assert [p["id"] for p in listed] == [created["id"]]

    # 改别的字段不动项目；显式 null 才移出
    out = (await admin_client.patch(f"/api/prompts/{created['id']}", json={"description": "d"})).json()
    assert out["project_id"] == project["id"]
    out = (await admin_client.patch(f"/api/prompts/{created['id']}", json={"project_id": None})).json()
    assert out["project_id"] is None

    await admin_client.delete(f"/api/prompts/{created['id']}")
    await admin_client.delete(f"/api/projects/{project['id']}")


@pytest.mark.asyncio
async def test_every_user_gets_one_undeletable_default_project(admin_client: AsyncClient):
    other = await _user_client(admin_client, f"defuser-{uuid.uuid4().hex[:6]}")
    try:
        first = (await other.get("/api/projects")).json()
        assert [(p["name"], p["is_default"]) for p in first] == [("默认项目", True)]
        # 重复取不会多建
        mine = (await other.post("/api/projects", json={"name": "某科研任务"})).json()
        listed = (await other.get("/api/projects")).json()
        assert [p["name"] for p in listed] == ["默认项目", "某科研任务"]
        assert listed[0]["id"] == first[0]["id"]

        assert (await other.delete(f"/api/projects/{first[0]['id']}")).status_code == 400
        # 默认项目可以改名，仍是默认项目
        renamed = (await other.patch(f"/api/projects/{first[0]['id']}", json={"name": "日常"})).json()
        assert renamed["is_default"]
        await other.delete(f"/api/projects/{mine['id']}")
    finally:
        await other.aclose()


@pytest.mark.asyncio
async def test_job_without_project_goes_to_default(admin_client: AsyncClient):
    """前端建任务必须选项目；命令行等不传项目的老客户端落到默认项目。"""
    from app.api.jobs import _check_project
    from app.db import session_scope
    from app.models import User

    async with session_scope() as db:
        from sqlalchemy import select

        admin = (await db.execute(select(User).where(User.username == "admin"))).scalar_one()
        project_id = await _check_project(db, admin.id, None)
    default = (await admin_client.get("/api/projects")).json()[0]
    assert default["is_default"] and project_id == default["id"]
