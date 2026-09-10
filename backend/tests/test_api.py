"""API 层：认证、权限边界、模型配置、上传校验。

这些用例不需要 Redis —— 涉及入队的路径由 worker 集成测试覆盖。
"""
from __future__ import annotations

import json

import pytest
from httpx import AsyncClient


@pytest.mark.asyncio
async def test_auth_info_exposes_enabled_methods(client: AsyncClient):
    data = (await client.get("/api/auth/info")).json()
    assert data["local_auth_enabled"] is True
    assert data["oidc_enabled"] is False


@pytest.mark.asyncio
async def test_login_rejects_wrong_password(client: AsyncClient):
    resp = await client.post("/api/auth/login", json={"username": "admin", "password": "wrong"})
    assert resp.status_code == 401
    assert "用户名或密码错误" in resp.json()["detail"]


@pytest.mark.asyncio
async def test_protected_endpoints_require_login(client: AsyncClient):
    for path in ("/api/auth/me", "/api/jobs", "/api/models", "/api/admin/models"):
        assert (await client.get(path)).status_code == 401, path


@pytest.mark.asyncio
async def test_login_sets_session_cookie(admin_client: AsyncClient):
    me = (await admin_client.get("/api/auth/me")).json()
    assert me["username"] == "admin" and me["role"] == "admin"


@pytest.mark.asyncio
async def test_model_crud_and_key_never_returned_in_clear(admin_client: AsyncClient):
    payload = {
        "name": "t-model", "display_name": "测试模型",
        "base_url": "http://127.0.0.1:9/v1", "model_name": "gpt-test",
        "api_key": "sk-super-secret-value", "max_concurrency": 4,
    }
    created = (await admin_client.post("/api/admin/models", json=payload)).json()
    assert created["api_key_masked"] == "sk-s******alue"
    assert "sk-super-secret-value" not in json.dumps(created)

    # 唯一标识重复应报 409 而不是 500
    assert (await admin_client.post("/api/admin/models", json=payload)).status_code == 409

    updated = (await admin_client.patch(
        f"/api/admin/models/{created['id']}", json={"display_name": "改名了", "max_concurrency": 16}
    )).json()
    assert updated["display_name"] == "改名了" and updated["max_concurrency"] == 16
    assert updated["api_key_masked"] == "sk-s******alue"  # 未传 api_key 时保持原值

    cleared = (await admin_client.patch(
        f"/api/admin/models/{created['id']}", json={"api_key": ""}
    )).json()
    assert cleared["api_key_masked"] is None

    assert (await admin_client.delete(f"/api/admin/models/{created['id']}")).status_code == 204


@pytest.mark.asyncio
async def test_base_url_must_be_http(admin_client: AsyncClient):
    resp = await admin_client.post("/api/admin/models", json={
        "name": "bad-url", "display_name": "x", "base_url": "127.0.0.1/v1", "model_name": "m",
    })
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_admin_only_model_hidden_from_normal_user(admin_client: AsyncClient):
    await admin_client.post("/api/admin/models", json={
        "name": "secret-model", "display_name": "内部模型", "base_url": "http://127.0.0.1:9/v1",
        "model_name": "m", "admin_only": True,
    })
    await admin_client.post("/api/admin/models", json={
        "name": "public-model", "display_name": "公开模型", "base_url": "http://127.0.0.1:9/v1",
        "model_name": "m",
    })
    await admin_client.post("/api/admin/users", json={
        "username": "normaluser", "password": "UserPass123!", "role": "user",
    })

    async with AsyncClient(
        transport=admin_client._transport, base_url="http://test"
    ) as user_client:
        await user_client.post("/api/auth/login",
                               json={"username": "normaluser", "password": "UserPass123!"})
        names = {m["name"] for m in (await user_client.get("/api/models")).json()}
        assert "public-model" in names and "secret-model" not in names

        # 普通用户不得访问任何 /admin 路由
        for path in ("/api/admin/models", "/api/admin/users", "/api/admin/dashboard"):
            assert (await user_client.get(path)).status_code == 403, path


@pytest.mark.asyncio
async def test_cannot_demote_last_admin(admin_client: AsyncClient):
    me = (await admin_client.get("/api/auth/me")).json()
    resp = await admin_client.patch(f"/api/admin/users/{me['id']}", json={"role": "user"})
    assert resp.status_code == 400
    assert "至少一个启用状态的管理员" in resp.json()["detail"]


@pytest.mark.asyncio
async def test_upload_validates_and_reports_bad_lines(admin_client: AsyncClient):
    content = (
        json.dumps({"custom_id": "ok", "prompt": "你好"}) + "\n"
        + "{ 坏行\n"
        + json.dumps({"custom_id": "ok2", "messages": [{"role": "user", "content": "hi"}]}) + "\n"
    )
    resp = await admin_client.post(
        "/api/jobs/upload", files={"file": ("in.jsonl", content.encode("utf-8"), "application/json")}
    )
    data = resp.json()
    assert data["total_items"] == 3
    assert len(data["errors"]) == 1 and "第 2 行" in data["errors"][0]
    assert len(data["preview"]) == 2


@pytest.mark.asyncio
async def test_upload_rejects_wrong_extension(admin_client: AsyncClient):
    resp = await admin_client.post(
        "/api/jobs/upload", files={"file": ("data.csv", b"a,b\n1,2\n", "text/csv")}
    )
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_create_job_rejects_invalid_input(admin_client: AsyncClient):
    upload = (await admin_client.post(
        "/api/jobs/upload", files={"file": ("bad.jsonl", b"{ nope\n", "application/json")}
    )).json()
    models = (await admin_client.get("/api/models")).json()
    resp = await admin_client.post("/api/jobs", json={
        "name": "应该被拒绝", "upload_id": upload["upload_id"], "model_config_id": models[0]["id"],
    })
    assert resp.status_code == 400
    assert "输入文件不合法" in resp.json()["detail"]


@pytest.mark.asyncio
async def test_create_job_rejects_stale_upload_id(admin_client: AsyncClient):
    models = (await admin_client.get("/api/models")).json()
    resp = await admin_client.post("/api/jobs", json={
        "name": "x", "upload_id": "0" * 32, "model_config_id": models[0]["id"],
    })
    assert resp.status_code == 400
    assert "上传已失效" in resp.json()["detail"]


@pytest.mark.asyncio
async def test_missing_job_returns_404(admin_client: AsyncClient):
    assert (await admin_client.get("/api/jobs/" + "0" * 32)).status_code == 404


@pytest.mark.asyncio
async def test_settings_roundtrip(admin_client: AsyncClient):
    updated = (await admin_client.patch(
        "/api/admin/settings", json={"allow_new_jobs": False, "announcement": "维护中"}
    )).json()
    assert updated["allow_new_jobs"] is False and updated["announcement"] == "维护中"

    # 关闭新任务后普通用户不能再上传
    async with AsyncClient(transport=admin_client._transport, base_url="http://test") as user_client:
        await user_client.post("/api/auth/login",
                               json={"username": "normaluser", "password": "UserPass123!"})
        resp = await user_client.post(
            "/api/jobs/upload", files={"file": ("a.jsonl", b'{"prompt":"x"}\n', "application/json")}
        )
        assert resp.status_code == 503

    # 管理员不受该开关限制
    assert (await admin_client.post(
        "/api/jobs/upload", files={"file": ("a.jsonl", b'{"prompt":"x"}\n', "application/json")}
    )).status_code == 200

    await admin_client.patch("/api/admin/settings", json={"allow_new_jobs": True, "announcement": ""})


@pytest.mark.asyncio
async def test_change_password_flow(admin_client: AsyncClient):
    bad = await admin_client.post("/api/auth/change-password",
                                  json={"old_password": "nope", "new_password": "NewPass123!"})
    assert bad.status_code == 400

    ok = await admin_client.post("/api/auth/change-password",
                                 json={"old_password": "TestAdmin123!", "new_password": "NewPass123!"})
    assert ok.status_code == 200
    # 改回去，避免影响其他用例
    await admin_client.post("/api/auth/change-password",
                            json={"old_password": "NewPass123!", "new_password": "TestAdmin123!"})


@pytest.mark.asyncio
async def test_oidc_endpoints_disabled_by_config(client: AsyncClient):
    assert (await client.get("/api/auth/oidc/login")).status_code == 400


# --------------------------------------------------------------------------- #
# OIDC 用户映射（直接测 _upsert_oidc_user，不需要真的连 IdP）
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_oidc_creates_user_and_syncs_admin_group(monkeypatch):
    from app.api.auth import _upsert_oidc_user
    from app.config import settings as cfg
    from app.db import session_scope
    from app.models import AuthSource, UserRole

    monkeypatch.setattr(cfg, "oidc_admin_group", "platform-admins")
    monkeypatch.setattr(cfg, "oidc_groups_claim", "groups")
    monkeypatch.setattr(cfg, "oidc_auto_create_user", True)

    claims = {
        "sub": "oidc-sub-1", "iss": "https://idp.example.com",
        "preferred_username": "alice", "email": "alice@example.com", "name": "Alice",
        "groups": ["platform-admins", "dev"],
    }

    async with session_scope() as db:
        user = await _upsert_oidc_user(db, claims)
        assert user.username == "alice"
        assert user.auth_source == AuthSource.oidc
        assert user.role == UserRole.admin      # 在管理员组里
        user_id = user.id

    # 移出管理员组后再次登录应被降权（此时 bootstrap 的 admin 仍在，不会把系统降空）
    async with session_scope() as db:
        user = await _upsert_oidc_user(db, {**claims, "groups": ["dev"]})
        assert user.id == user_id               # 靠 sub 认人，不会建重复账号
        assert user.role == UserRole.user


@pytest.mark.asyncio
async def test_oidc_binds_to_existing_local_account(admin_client, monkeypatch):
    from app.api.auth import _upsert_oidc_user
    from app.config import settings as cfg
    from app.db import session_scope

    monkeypatch.setattr(cfg, "oidc_admin_group", "")

    await admin_client.post("/api/admin/users", json={
        "username": "bob", "password": "BobPass123!", "role": "user",
    })

    async with session_scope() as db:
        user = await _upsert_oidc_user(db, {
            "sub": "oidc-sub-bob", "iss": "https://idp.example.com", "preferred_username": "bob",
        })
        # 绑定到已有本地账号，而不是建一个重名的新账号
        assert user.oidc_subject == "oidc-sub-bob"
        assert user.password_hash is not None


@pytest.mark.asyncio
async def test_oidc_respects_auto_create_switch(monkeypatch):
    from app.api.auth import _upsert_oidc_user
    from app.config import settings as cfg
    from app.db import session_scope

    monkeypatch.setattr(cfg, "oidc_auto_create_user", False)
    monkeypatch.setattr(cfg, "oidc_admin_group", "")

    with pytest.raises(PermissionError, match="尚未在平台注册"):
        async with session_scope() as db:
            await _upsert_oidc_user(db, {
                "sub": "stranger", "iss": "https://idp.example.com", "preferred_username": "eve",
            })


@pytest.mark.asyncio
async def test_oidc_never_demotes_the_last_admin(monkeypatch):
    """IdP 说你不是管理员，但你是最后一个管理员时不能降 —— 否则没人能进后台。"""
    from sqlalchemy import select, update

    from app.api.auth import _upsert_oidc_user
    from app.config import settings as cfg
    from app.db import session_scope
    from app.models import User, UserRole

    monkeypatch.setattr(cfg, "oidc_admin_group", "platform-admins")
    monkeypatch.setattr(cfg, "oidc_auto_create_user", True)

    claims = {"sub": "solo-admin", "iss": "https://idp.example.com",
              "preferred_username": "solo", "groups": ["platform-admins"]}

    async with session_scope() as db:
        solo = await _upsert_oidc_user(db, claims)
        assert solo.role == UserRole.admin
        solo_id = solo.id

    # 把其他管理员全部停用，让 solo 成为唯一启用的管理员
    async with session_scope() as db:
        await db.execute(
            update(User).where(User.role == UserRole.admin, User.id != solo_id).values(is_active=False)
        )

    async with session_scope() as db:
        solo = await _upsert_oidc_user(db, {**claims, "groups": []})
        assert solo.role == UserRole.admin, "最后一个管理员不应被 IdP 降权"

    # 复原，避免影响其他用例
    async with session_scope() as db:
        await db.execute(update(User).values(is_active=True))
        await db.execute(
            update(User).where(User.id == solo_id).values(role=UserRole.user, is_active=False)
        )
        assert (await db.execute(select(User).where(User.id == solo_id))).scalar_one() is not None


# --------------------------------------------------------------------------- #
# 个人网关 token 与模型选择
# --------------------------------------------------------------------------- #
async def _enable_gateway(client: AsyncClient, base_url: str = "https://gw.example.com/v1") -> None:
    """个人网关默认关闭（要管理员先填地址），用例里显式打开。"""
    await client.patch("/api/admin/settings", json={
        "user_gateway_enabled": True, "user_gateway_base_url": base_url,
    })


def _fake_gateway(monkeypatch, models=("gw-model-a", "gw-model-b"), error=None):
    """替换掉两个 api 模块里按名字导入的 fetch_models。"""
    from app.services.gateway import GatewayError

    async def fake(base_url, token):
        if error:
            raise GatewayError(error)
        return list(models)

    import app.api.jobs as jobs_mod
    import app.api.model_configs as mc_mod

    monkeypatch.setattr(mc_mod, "fetch_models", fake)
    monkeypatch.setattr(jobs_mod, "fetch_models", fake)


@pytest.mark.asyncio
async def test_model_options_lists_shared_and_gateway_info(admin_client: AsyncClient):
    await _enable_gateway(admin_client)
    data = (await admin_client.get("/api/models/options")).json()
    assert any(m["name"] == "public-model" for m in data["shared"])
    assert data["gateway_enabled"] is True
    assert data["gateway_base_url"].startswith("http")
    assert data["has_saved_token"] is False
    assert data["personal"] == []


@pytest.mark.asyncio
async def test_personal_models_requires_valid_token(admin_client: AsyncClient, monkeypatch):
    await _enable_gateway(admin_client)
    _fake_gateway(monkeypatch, error="Invalid token (request id: abc)")
    resp = await admin_client.post("/api/models/personal", json={"token": "sk-bad", "remember": False})
    assert resp.status_code == 400
    assert "Invalid token" in resp.json()["detail"]


@pytest.mark.asyncio
async def test_personal_models_saved_token_never_returned_in_clear(
    admin_client: AsyncClient, monkeypatch
):
    await _enable_gateway(admin_client)
    _fake_gateway(monkeypatch)
    resp = (await admin_client.post(
        "/api/models/personal", json={"token": "sk-my-secret-token-value", "remember": True}
    )).json()
    assert resp["models"] == ["gw-model-a", "gw-model-b"]
    assert resp["saved"] is True

    me = (await admin_client.get("/api/auth/me")).json()
    assert me["llm_token_masked"] == "sk-m******alue"
    assert "sk-my-secret-token-value" not in json.dumps(me)

    # 保存之后再进新建任务页，个人模型应自动带出来
    options = (await admin_client.get("/api/models/options")).json()
    assert options["has_saved_token"] is True
    assert options["personal"] == ["gw-model-a", "gw-model-b"]


@pytest.mark.asyncio
async def test_personal_reasoning_caps_follow_model_name_rules(
    admin_client: AsyncClient, monkeypatch
):
    """个人模型不入库，推理能力得由后端按规则现算了给前端。"""
    await _enable_gateway(admin_client)
    _fake_gateway(monkeypatch, models=("qwen3-32b", "gpt-oss-120b", "legacy-instruct"))
    await admin_client.patch("/api/admin/settings", json={
        "user_gateway_reasoning_rules": [
            {"pattern": "qwen3-*",
             "payload": {"chat_template_kwargs": {"enable_thinking": True}}},
            {"pattern": "gpt-oss-*", "payload": {"reasoning_effort": "$effort"},
             "effort_options": ["low", "medium", "high"], "default_effort": "medium"},
            {"pattern": "*-instruct", "enabled": False},
        ],
    })

    resp = (await admin_client.post(
        "/api/models/personal", json={"token": "sk-token", "remember": True}
    )).json()
    caps = resp["reasoning"]
    assert caps["qwen3-32b"] == {
        "reasoning_mode": "optional", "effort_options": [], "default_effort": "",
    }
    assert caps["gpt-oss-120b"]["effort_options"] == ["low", "medium", "high"]
    assert caps["gpt-oss-120b"]["default_effort"] == "medium"
    # 规则里关掉的模型，前端连推理开关都不显示
    assert caps["legacy-instruct"]["reasoning_mode"] == "off"

    # 新建任务页那条接口也要带上同一份能力
    options = (await admin_client.get("/api/models/options")).json()
    assert options["personal_reasoning"]["gpt-oss-120b"]["default_effort"] == "medium"

    # 收尾：规则是全站配置，别影响后面的用例
    await admin_client.patch("/api/admin/settings", json={"user_gateway_reasoning_rules": []})


@pytest.mark.asyncio
async def test_model_options_reports_gateway_failure_without_500(
    admin_client: AsyncClient, monkeypatch
):
    """token 失效不该让整个新建任务页打不开，公用模型仍要能选。"""
    await _enable_gateway(admin_client)
    _fake_gateway(monkeypatch, error="token 已过期")
    data = (await admin_client.get("/api/models/options")).json()
    assert data["personal"] == []
    assert data["personal_error"] == "token 已过期"
    assert len(data["shared"]) > 0


@pytest.mark.asyncio
async def test_create_job_with_personal_model(admin_client: AsyncClient, monkeypatch):
    await _enable_gateway(admin_client)
    _fake_gateway(monkeypatch)
    upload = (await admin_client.post(
        "/api/jobs/upload", files={"file": ("in.jsonl", b'{"prompt":"hi"}\n', "application/json")}
    )).json()

    # Redis 不可用时入队会失败，这里只验证到入队之前的逻辑
    resp = await admin_client.post("/api/jobs", json={
        "name": "个人模型任务", "upload_id": upload["upload_id"],
        "model_source": "personal", "personal_model": "gw-model-a",
    })
    assert resp.status_code in (201, 503), resp.text
    if resp.status_code == 201:
        job = resp.json()
        assert job["model_source"] == "personal"
        assert job["model_display_name"] == "gw-model-a"
        assert job["model_config_id"] is None


@pytest.mark.asyncio
async def test_create_job_rejects_model_outside_token_permission(
    admin_client: AsyncClient, monkeypatch
):
    await _enable_gateway(admin_client)
    _fake_gateway(monkeypatch, models=("gw-model-a",))
    upload = (await admin_client.post(
        "/api/jobs/upload", files={"file": ("in.jsonl", b'{"prompt":"hi"}\n', "application/json")}
    )).json()
    resp = await admin_client.post("/api/jobs", json={
        "name": "越权", "upload_id": upload["upload_id"],
        "model_source": "personal", "personal_model": "someone-elses-model",
    })
    assert resp.status_code == 400
    assert "没有模型 someone-elses-model 的权限" in resp.json()["detail"]


@pytest.mark.asyncio
async def test_create_job_validates_model_selection_shape(admin_client: AsyncClient):
    upload = (await admin_client.post(
        "/api/jobs/upload", files={"file": ("in.jsonl", b'{"prompt":"hi"}\n', "application/json")}
    )).json()
    # personal 但没给模型名
    resp = await admin_client.post("/api/jobs", json={
        "name": "x", "upload_id": upload["upload_id"], "model_source": "personal",
    })
    assert resp.status_code == 422
    # shared 但没给 model_config_id
    resp = await admin_client.post("/api/jobs", json={
        "name": "x", "upload_id": upload["upload_id"], "model_source": "shared",
    })
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_clear_personal_token(admin_client: AsyncClient, monkeypatch):
    _fake_gateway(monkeypatch)
    await admin_client.post("/api/models/personal", json={"token": "sk-to-be-cleared", "remember": True})
    assert (await admin_client.delete("/api/models/personal/token")).status_code == 204

    me = (await admin_client.get("/api/auth/me")).json()
    assert me["llm_token_masked"] is None
    assert (await admin_client.get("/api/models/options")).json()["has_saved_token"] is False


@pytest.mark.asyncio
async def test_gateway_can_be_disabled_by_admin(admin_client: AsyncClient, monkeypatch):
    _fake_gateway(monkeypatch)
    await admin_client.patch("/api/admin/settings", json={"user_gateway_enabled": False})

    options = (await admin_client.get("/api/models/options")).json()
    assert options["gateway_enabled"] is False
    assert options["personal"] == []

    resp = await admin_client.post("/api/models/personal", json={"token": "sk-x", "remember": False})
    assert resp.status_code == 403

    await admin_client.patch("/api/admin/settings", json={"user_gateway_enabled": True})


@pytest.mark.asyncio
async def test_gateway_settings_roundtrip_and_url_validation(admin_client: AsyncClient):
    updated = (await admin_client.patch("/api/admin/settings", json={
        "user_gateway_base_url": "https://llm.example.com/v1/",
        "user_gateway_label": "公司网关",
        "user_gateway_max_concurrency": 8,
    })).json()
    assert updated["user_gateway_base_url"] == "https://llm.example.com/v1"  # 结尾斜杠被去掉
    assert updated["user_gateway_label"] == "公司网关"
    assert updated["user_gateway_max_concurrency"] == 8

    bad = await admin_client.patch("/api/admin/settings", json={"user_gateway_base_url": "llm.example.com"})
    assert bad.status_code == 422


# --------------------------------------------------------------------------- #
# 下载只在任务结束后开放
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_download_blocked_while_job_not_finished(admin_client: AsyncClient):
    """结果文件在跑动时一直被追加写入，中途下载会拿到不完整的内容。"""
    from sqlalchemy import select

    from app.db import session_scope
    from app.models import Job, JobStatus, User
    from app.services import results as results_svc

    async with session_scope() as db:
        user = (await db.execute(select(User).where(User.username == "admin"))).scalar_one()
        job = Job(
            name="下载状态测试", user_id=user.id, status=JobStatus.running,
            input_filename="a.jsonl", input_path="/tmp/none.jsonl",
            input_size=1, total_items=10, completed_items=5,
        )
        db.add(job)
        await db.flush()
        job_id = job.id

    results_svc.output_path(job_id).write_text(
        '{"_index":0,"custom_id":"a","response":{"status_code":200,"body":{}}}\n', encoding="utf-8"
    )

    for blocked in (JobStatus.running, JobStatus.queued, JobStatus.paused, JobStatus.failed):
        async with session_scope() as db:
            j = await db.get(Job, job_id)
            j.status = blocked
        resp = await admin_client.get(f"/api/jobs/{job_id}/download", params={"fmt": "raw"})
        assert resp.status_code == 409, f"{blocked.value} 不该允许下载"
        assert "尚未结束" in resp.json()["detail"]

    for allowed in (JobStatus.succeeded, JobStatus.completed, JobStatus.canceled):
        async with session_scope() as db:
            j = await db.get(Job, job_id)
            j.status = allowed
        resp = await admin_client.get(f"/api/jobs/{job_id}/download", params={"fmt": "raw"})
        assert resp.status_code == 200, f"{allowed.value} 应该允许下载"

    # 跑动中仍然可以看「结果预览」—— 只读已落盘的行，不影响正确性
    async with session_scope() as db:
        j = await db.get(Job, job_id)
        j.status = JobStatus.running
    preview = await admin_client.get(f"/api/jobs/{job_id}/results")
    assert preview.status_code == 200
    assert preview.json()["total"] == 1

    # 直接从库里清掉，不走删除接口 —— 那条路径要连 Redis，测试环境没有
    async with session_scope() as db:
        await db.delete(await db.get(Job, job_id))
    results_svc.delete_job_files(job_id)


# --------------------------------------------------------------------------- #
# 邮件通知：设置、偏好、测试发信
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_smtp_password_is_encrypted_and_masked(admin_client: AsyncClient):
    out = (await admin_client.patch("/api/admin/settings", json={
        "smtp_enabled": True, "smtp_host": "smtp.example.com", "smtp_port": 465,
        "smtp_security": "ssl", "smtp_from": "bot@example.com",
        "smtp_username": "bot@example.com", "smtp_password": "super-secret-pass",
    })).json()
    assert out["smtp_host"] == "smtp.example.com"
    assert out["smtp_password_masked"] == "supe******pass"
    assert "super-secret-pass" not in json.dumps(out)

    # 不传 password 时保持不变
    again = (await admin_client.patch("/api/admin/settings", json={"smtp_port": 587})).json()
    assert again["smtp_password_masked"] == "supe******pass"

    # 传空串 = 清除
    cleared = (await admin_client.patch("/api/admin/settings", json={"smtp_password": ""})).json()
    assert cleared["smtp_password_masked"] is None

    await admin_client.patch("/api/admin/settings", json={"smtp_enabled": False, "smtp_host": ""})


@pytest.mark.asyncio
async def test_user_can_toggle_notification(admin_client: AsyncClient):
    me = (await admin_client.get("/api/auth/me")).json()
    assert me["notify_email"] is True   # 默认开启

    off = (await admin_client.patch("/api/auth/preferences", json={"notify_email": False})).json()
    assert off["notify_email"] is False

    changed = (await admin_client.patch(
        "/api/auth/preferences", json={"notify_email": True, "email": "new@example.com"}
    )).json()
    assert changed["notify_email"] is True
    assert changed["email"] == "new@example.com"


@pytest.mark.asyncio
async def test_preferences_rejects_bad_email(admin_client: AsyncClient):
    resp = await admin_client.patch("/api/auth/preferences", json={"email": "not-an-email"})
    assert resp.status_code == 400
    assert "邮箱格式" in resp.json()["detail"]


@pytest.mark.asyncio
async def test_test_mail_requires_host_and_reports_failure(admin_client: AsyncClient):
    await admin_client.patch("/api/admin/settings", json={"smtp_host": ""})
    out = (await admin_client.post("/api/admin/notifications/test", json={"to": "a@b.c"})).json()
    assert out["ok"] is False
    assert "SMTP 服务器地址" in out["detail"]

    # 配了个连不上的地址：要回传失败原因，同时把渲染好的模板给管理员看
    await admin_client.patch("/api/admin/settings", json={
        "smtp_host": "127.0.0.1", "smtp_port": 1, "smtp_security": "none",
        "smtp_from": "bot@example.com",
    })
    out = (await admin_client.post("/api/admin/notifications/test", json={"to": "a@b.c"})).json()
    assert out["ok"] is False
    assert "无法连接" in out["detail"]
    assert "示例任务" in out["subject"]
    assert "985" in out["body"]

    await admin_client.patch("/api/admin/settings", json={"smtp_host": ""})


@pytest.mark.asyncio
async def test_admin_can_set_user_notification(admin_client: AsyncClient):
    created = (await admin_client.post("/api/admin/users", json={
        "username": "notify-user", "password": "NotifyUser123!", "role": "user",
    })).json()
    assert created["notify_email"] is True
    updated = (await admin_client.patch(
        f"/api/admin/users/{created['id']}", json={"notify_email": False}
    )).json()
    assert updated["notify_email"] is False
    await admin_client.delete(f"/api/admin/users/{created['id']}")


# --------------------------------------------------------------------------- #
# 代码生成接口
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_split_script_endpoint(admin_client: AsyncClient):
    payload = {
        "source_path": "/data/raw", "source_format": "csv",
        "variables": [{"name": "data", "field": "content"}],
        "prompt_template": "总结：{{data}}",
        "custom_id_mode": "uuid",
    }
    out = (await admin_client.post("/api/tools/split-script", json=payload)).json()
    assert out["script_name"].endswith(".py")
    assert "CONFIG = json.loads(" in out["script"]
    assert out["detected_variables"] == ["data"]
    assert out["problems"] == []
    assert out["sample_record"]["body"]["messages"][0]["content"].startswith("总结：")
    assert json.loads(out["sample_line"])["method"] == "POST"


@pytest.mark.asyncio
async def test_split_script_reports_problems_but_still_generates(admin_client: AsyncClient):
    out = (await admin_client.post("/api/tools/split-script", json={
        "source_path": "/x", "prompt_template": "{{a}} {{b}}",
        "variables": [{"name": "a", "field": "f"}],
    })).json()
    assert any("b" in p for p in out["problems"])
    assert out["script"]          # 有问题也照样给脚本，让用户先看再改


@pytest.mark.asyncio
async def test_split_script_download(admin_client: AsyncClient):
    resp = await admin_client.post("/api/tools/split-script/download", json={
        "source_path": "/x", "prompt_template": "{{a}}",
        "variables": [{"name": "a", "field": "f"}],
    })
    assert resp.status_code == 200
    assert "attachment" in resp.headers["content-disposition"]
    compile(resp.text, "downloaded", "exec")   # 下载到的必须是能跑的 Python


@pytest.mark.asyncio
async def test_split_script_requires_login(client: AsyncClient):
    assert (await client.post("/api/tools/split-script", json={
        "source_path": "/x", "prompt_template": "t"
    })).status_code == 401


# --------------------------------------------------------------------------- #
# 我的 Prompt
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_prompt_crud(admin_client: AsyncClient):
    payload = {
        "name": "  新闻摘要  ", "description": "按段落总结",
        "system_prompt": "你是编辑", "prompt_template": "总结：{{title}}\n{{body}}",
        "variables": [{"name": "title", "field": "t"}, {"name": "body", "field": "content"}],
    }
    resp = await admin_client.post("/api/prompts", json=payload)
    assert resp.status_code == 201, resp.text
    created = resp.json()
    assert created["name"] == "新闻摘要"          # 首尾空格去掉，免得出现"看起来同名"的两条
    assert created["variables"][1] == {"name": "body", "field": "content"}

    # 同名 409，不是 500
    assert (await admin_client.post("/api/prompts", json=payload)).status_code == 409

    listed = (await admin_client.get("/api/prompts")).json()
    assert created["id"] in {p["id"] for p in listed}

    updated = (await admin_client.patch(f"/api/prompts/{created['id']}", json={
        "prompt_template": "只看标题：{{title}}",
        "variables": [{"name": "title", "field": "t"}],
        "system_prompt": None,
    })).json()
    assert updated["prompt_template"] == "只看标题：{{title}}"
    assert updated["variables"] == [{"name": "title", "field": "t"}]
    assert updated["system_prompt"] is None
    assert updated["description"] == "按段落总结"   # 没传的字段保持不变

    # 必填字段传 null 当作不修改，不能把库里的非空列写成空
    same = (await admin_client.patch(f"/api/prompts/{created['id']}", json={"name": None})).json()
    assert same["name"] == "新闻摘要"

    assert (await admin_client.delete(f"/api/prompts/{created['id']}")).status_code == 204
    assert (await admin_client.get(f"/api/prompts/{created['id']}")).status_code == 404


@pytest.mark.asyncio
async def test_prompt_rename_to_existing_name_conflicts(admin_client: AsyncClient):
    base = {"prompt_template": "{{x}}", "variables": [{"name": "x", "field": "x"}]}
    a = (await admin_client.post("/api/prompts", json={"name": "改名A", **base})).json()
    await admin_client.post("/api/prompts", json={"name": "改名B", **base})
    resp = await admin_client.patch(f"/api/prompts/{a['id']}", json={"name": "改名B"})
    assert resp.status_code == 409
    # 回滚后原记录不受影响
    assert (await admin_client.get(f"/api/prompts/{a['id']}")).json()["name"] == "改名A"


@pytest.mark.asyncio
async def test_prompt_rejects_bad_variables(admin_client: AsyncClient):
    for variables in (
        [{"name": "x", "field": "a"}, {"name": "x", "field": "b"}],   # 变量名重复
        [{"name": "1bad", "field": "a"}],                              # 变量名不合法
    ):
        resp = await admin_client.post("/api/prompts", json={
            "name": "坏变量", "prompt_template": "{{x}}", "variables": variables,
        })
        assert resp.status_code == 422, variables
    assert (await admin_client.post("/api/prompts", json={
        "name": "   ", "prompt_template": "t",
    })).status_code == 422


@pytest.mark.asyncio
async def test_prompts_are_private_to_owner(admin_client: AsyncClient):
    mine = (await admin_client.post("/api/prompts", json={
        "name": "管理员私有", "prompt_template": "t",
    })).json()
    await admin_client.post("/api/admin/users", json={
        "username": "promptuser", "password": "UserPass123!", "role": "user",
    })

    async with AsyncClient(transport=admin_client._transport, base_url="http://test") as other:
        await other.post("/api/auth/login", json={"username": "promptuser", "password": "UserPass123!"})
        assert mine["id"] not in {p["id"] for p in (await other.get("/api/prompts")).json()}
        # 别人的 Prompt 读、改、删一律 404，不暴露存在与否
        url = f"/api/prompts/{mine['id']}"
        assert (await other.get(url)).status_code == 404
        assert (await other.patch(url, json={"name": "抢过来"})).status_code == 404
        assert (await other.delete(url)).status_code == 404

        # 不同用户之间可以重名
        assert (await other.post("/api/prompts", json={
            "name": "管理员私有", "prompt_template": "t",
        })).status_code == 201

    assert (await admin_client.get(f"/api/prompts/{mine['id']}")).json()["name"] == "管理员私有"


@pytest.mark.asyncio
async def test_prompts_require_login(client: AsyncClient):
    assert (await client.get("/api/prompts")).status_code == 401


# --------------------------------------------------------------------------- #
# 提交前试跑一条
# --------------------------------------------------------------------------- #
def _fake_inference(monkeypatch, result: dict) -> dict:
    """替换 jobs 模块里的 InferenceClient，返回捕获到的请求体。"""
    captured: dict = {}

    class FakeClient:
        def __init__(self, mc):
            captured["model_name"] = mc.model_name

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return None

        async def try_once(self, body):
            captured["body"] = body
            return result

    import app.api.jobs as jobs_mod
    monkeypatch.setattr(jobs_mod, "InferenceClient", FakeClient)
    return captured


async def _model_and_upload(admin_client: AsyncClient, name: str, content: str) -> tuple[str, str]:
    model = (await admin_client.post("/api/admin/models", json={
        "name": name, "display_name": name, "base_url": "http://x/v1", "model_name": "real-model",
        "reasoning_mode": "optional", "reasoning_payload": {"reasoning_effort": "$effort"},
        "reasoning_effort_options": ["none", "low", "medium", "high"],
        "reasoning_default_effort": "medium",
    })).json()
    upload = (await admin_client.post(
        "/api/jobs/upload",
        files={"file": (f"{name}.jsonl", content.encode("utf-8"), "application/json")},
    )).json()
    return model["id"], upload["upload_id"]


@pytest.mark.asyncio
async def test_dry_run_sends_the_real_body_and_reports_success(
    admin_client: AsyncClient, monkeypatch
):
    """试跑要和正式跑用同一套拼装逻辑，否则试了也白试。"""
    model_id, upload_id = await _model_and_upload(
        admin_client, "dryrun-ok",
        json.dumps({"custom_id": "first", "prompt": "你好"}, ensure_ascii=False) + "\n",
    )
    captured = _fake_inference(monkeypatch, {
        "ok": True, "status_code": 200, "latency_ms": 42,
        "content": "你好呀", "usage": {"prompt_tokens": 3, "completion_tokens": 5},
        "response": {"choices": []},
    })

    resp = await admin_client.post("/api/jobs/dry-run", json={
        "upload_id": upload_id, "model_config_id": model_id,
        "params": {"system_prompt": "你是助手", "reasoning": True, "reasoning_effort": "high"},
    })
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["ok"] and data["custom_id"] == "first" and data["content"] == "你好呀"

    body = captured["body"]
    assert body["model"] == "real-model"
    assert body["messages"][0] == {"role": "system", "content": "你是助手"}
    # 用户选的推理档位要真的出现在试跑请求里
    assert body["reasoning_effort"] == "high"
    # 试跑不写请求体里的控制键
    assert "reasoning" not in body and "_system_prompt" not in body


@pytest.mark.asyncio
async def test_dry_run_returns_failure_reason_instead_of_500(
    admin_client: AsyncClient, monkeypatch
):
    """网关报错要原样带回给用户看，接口本身仍是 200。"""
    model_id, upload_id = await _model_and_upload(
        admin_client, "dryrun-fail",
        json.dumps({"custom_id": "a", "prompt": "hi"}) + "\n",
    )
    _fake_inference(monkeypatch, {
        "ok": False, "status_code": 400, "latency_ms": 12,
        "error": "HTTP 400: model does not support enable_thinking",
        "response": {"error": {"message": "model does not support enable_thinking"}},
    })

    resp = await admin_client.post("/api/jobs/dry-run", json={
        "upload_id": upload_id, "model_config_id": model_id,
    })
    assert resp.status_code == 200
    data = resp.json()
    assert data["ok"] is False and "enable_thinking" in data["error"]
    # 请求体一并回传，用户才知道到底发出去了什么
    assert data["request_body"]["model"] == "real-model"


@pytest.mark.asyncio
async def test_dry_run_rejects_bad_line_and_stale_upload(admin_client: AsyncClient, monkeypatch):
    _fake_inference(monkeypatch, {"ok": True})
    model_id, upload_id = await _model_and_upload(admin_client, "dryrun-bad", "{ 坏行\n")

    resp = await admin_client.post("/api/jobs/dry-run", json={
        "upload_id": upload_id, "model_config_id": model_id,
    })
    assert resp.status_code == 400 and "第 1 行" in resp.json()["detail"]

    stale = await admin_client.post("/api/jobs/dry-run", json={
        "upload_id": "0" * 32, "model_config_id": model_id,
    })
    assert stale.status_code == 400 and "上传已失效" in stale.json()["detail"]


@pytest.mark.asyncio
async def test_dry_run_does_not_create_a_job(admin_client: AsyncClient, monkeypatch):
    """试跑不落盘、不建任务 —— 上传还在暂存区，后面照样能正常提交。"""
    before = (await admin_client.get("/api/jobs")).json()["total"]
    model_id, upload_id = await _model_and_upload(
        admin_client, "dryrun-nojob", json.dumps({"prompt": "hi"}) + "\n",
    )
    _fake_inference(monkeypatch, {"ok": True, "status_code": 200, "latency_ms": 1})
    await admin_client.post("/api/jobs/dry-run", json={
        "upload_id": upload_id, "model_config_id": model_id,
    })
    assert (await admin_client.get("/api/jobs")).json()["total"] == before

    # 同一个 upload_id 试跑完还能继续用
    again = await admin_client.post("/api/jobs/dry-run", json={
        "upload_id": upload_id, "model_config_id": model_id,
    })
    assert again.status_code == 200


# --------------------------------------------------------------------------- #
# 停下来的任务换模型
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_change_model_only_on_stopped_jobs_and_reresolves_params(
    admin_client: AsyncClient, monkeypatch
):
    from sqlalchemy import select

    from app.db import session_scope
    from app.models import Job, JobStatus, User

    old = (await admin_client.post("/api/admin/models", json={
        "name": "swap-old", "display_name": "旧模型", "base_url": "http://x/v1",
        "model_name": "old-m", "max_concurrency": 16,
    })).json()
    new = (await admin_client.post("/api/admin/models", json={
        "name": "swap-new", "display_name": "新模型", "base_url": "http://x/v1",
        "model_name": "new-m", "max_concurrency": 4, "forced_params": {"temperature": 0},
        "reasoning_mode": "optional", "reasoning_payload": {"reasoning_effort": "$effort"},
        "reasoning_effort_options": ["low", "high"], "reasoning_default_effort": "low",
    })).json()

    async with session_scope() as db:
        admin = (await db.execute(select(User).where(User.username == "admin"))).scalar_one()
        job = Job(
            name="换模型", user_id=admin.id, model_config_id=old["id"], status=JobStatus.running,
            params={"temperature": 0.7, "max_tokens": 256, "reasoning": True, "reasoning_effort": "high"},
            resolved_params={"temperature": 0.7, "max_tokens": 256}, concurrency=12,
            input_filename="a.jsonl", input_path="/tmp/none.jsonl",
            input_size=1, total_items=10, completed_items=3,
        )
        db.add(job)
        await db.flush()
        job_id = job.id

    url = f"/api/jobs/{job_id}/model"
    # 运行中的 worker 握着旧配置，这时换了也不生效
    for blocked in (JobStatus.running, JobStatus.queued, JobStatus.succeeded):
        async with session_scope() as db:
            (await db.get(Job, job_id)).status = blocked
        resp = await admin_client.post(url, json={"model_config_id": new["id"]})
        assert resp.status_code == 400, blocked.value

    for allowed in (JobStatus.paused, JobStatus.canceled, JobStatus.failed):
        async with session_scope() as db:
            (await db.get(Job, job_id)).status = allowed
        resp = await admin_client.post(url, json={"model_config_id": new["id"]})
        assert resp.status_code == 200, resp.text
        out = resp.json()
        assert out["status"] == allowed.value          # 只换模型，不自动入队
        assert out["model_config_id"] == new["id"]
        assert out["model_display_name"] == "新模型"
        assert out["concurrency"] == 4                 # 按新模型的上限重新截
        assert out["completed_items"] == 3             # 进度原样保留

    async with session_scope() as db:
        resolved = (await db.get(Job, job_id)).resolved_params
    assert resolved["temperature"] == 0                # 新模型的强制值生效
    assert resolved["max_tokens"] == 256               # 原任务的参数沿用
    assert resolved["reasoning_effort"] == "high"      # 推理档位按新模型重新拼

    # 不存在的模型
    bad = await admin_client.post(url, json={"model_config_id": "0" * 32})
    assert bad.status_code == 400 and "模型不可用" in bad.json()["detail"]

    # 别人的任务不能换成自己网关里的模型：执行时用的是提交者本人的 token
    await _enable_gateway(admin_client)
    _fake_gateway(monkeypatch)
    await admin_client.post("/api/admin/users", json={
        "username": "swapuser", "password": "UserPass123!", "role": "user",
    })
    async with session_scope() as db:
        other = (await db.execute(select(User).where(User.username == "swapuser"))).scalar_one()
        (await db.get(Job, job_id)).user_id = other.id
    resp = await admin_client.post(url, json={
        "model_source": "personal", "personal_model": "gw-model-a", "personal_token": "sk-x",
        "remember_token": False,
    })
    assert resp.status_code == 400 and "提交者本人" in resp.json()["detail"]

    # 自己的任务可以换成个人网关模型
    async with session_scope() as db:
        (await db.get(Job, job_id)).user_id = admin.id
    resp = await admin_client.post(url, json={
        "model_source": "personal", "personal_model": "gw-model-a", "personal_token": "sk-x",
        "remember_token": False,
    })
    assert resp.status_code == 200, resp.text
    out = resp.json()
    assert out["model_source"] == "personal"
    assert out["model_config_id"] is None
    assert out["model_display_name"] == "gw-model-a"

    async with session_scope() as db:
        await db.delete(await db.get(Job, job_id))


@pytest.mark.asyncio
async def test_change_params_on_stopped_job(admin_client: AsyncClient, monkeypatch):
    """模型不动只改参数也走同一个接口；参数整份替换，再按模型配置重新合并。"""
    from sqlalchemy import select

    from app.db import session_scope
    from app.models import Job, JobStatus, ModelSource, User

    model = (await admin_client.post("/api/admin/models", json={
        "name": "params-model", "display_name": "改参数模型", "base_url": "http://x/v1",
        "model_name": "p-m", "max_tokens_cap": 100,
    })).json()

    async with session_scope() as db:
        admin = (await db.execute(select(User).where(User.username == "admin"))).scalar_one()
        job = Job(
            name="改参数", user_id=admin.id, model_config_id=model["id"], status=JobStatus.paused,
            params={"temperature": 0.5, "max_tokens": 50, "stop": ["END"]},
            resolved_params={"temperature": 0.5, "max_tokens": 50},
            input_filename="a.jsonl", input_path="/tmp/none.jsonl", input_size=1, total_items=10,
        )
        db.add(job)
        await db.flush()
        job_id = job.id

    url = f"/api/jobs/{job_id}/model"
    out = (await admin_client.post(url, json={
        "model_config_id": model["id"],
        "params": {"temperature": 1.2, "max_tokens": 500, "system_prompt": "新提示"},
    })).json()
    assert out["status"] == "paused"
    # 整份替换：没带上的 stop 就没了
    assert out["params"]["temperature"] == 1.2 and "stop" not in out["params"]
    async with session_scope() as db:
        resolved = (await db.get(Job, job_id)).resolved_params
    assert resolved["temperature"] == 1.2
    assert resolved["max_tokens"] == 100          # 仍受模型的 max_tokens 上限约束
    assert resolved["_system_prompt"] == "新提示"

    # 不传 params：沿用刚才那份
    out = (await admin_client.post(url, json={"model_config_id": model["id"]})).json()
    assert out["params"]["system_prompt"] == "新提示"

    # 管理员改别人个人模型任务的参数：模型没动就放行，换模型仍然不行
    await _enable_gateway(admin_client)
    _fake_gateway(monkeypatch)
    await admin_client.post("/api/admin/users", json={
        "username": "paramuser", "password": "UserPass123!", "role": "user",
    })
    async with session_scope() as db:
        other = (await db.execute(select(User).where(User.username == "paramuser"))).scalar_one()
        j = await db.get(Job, job_id)
        j.user_id = other.id
        j.model_source = ModelSource.personal
        j.model_config_id = None
        j.personal_model_name = "their-model"
    same = await admin_client.post(url, json={
        "model_source": "personal", "personal_model": "their-model", "params": {"max_tokens": 64},
    })
    assert same.status_code == 200, same.text
    assert same.json()["params"]["max_tokens"] == 64
    assert same.json()["model_display_name"] == "their-model"
    swap = await admin_client.post(url, json={"model_source": "personal", "personal_model": "gw-model-a"})
    assert swap.status_code == 400 and "提交者本人" in swap.json()["detail"]

    async with session_scope() as db:
        await db.delete(await db.get(Job, job_id))


# --------------------------------------------------------------------------- #
# worker 被强杀后，暂停/取消要能直接落状态
# --------------------------------------------------------------------------- #
class _FakeControlQueue:
    """只实现暂停/取消会用到的方法。workers: worker_id → 最近心跳时间。"""

    def __init__(self, workers: dict[str, float]):
        self.workers = workers
        self.signals: dict[str, str] = {}

    async def worker_last_seen(self, worker_id):
        return self.workers.get(worker_id)

    async def signal(self, job_id, action):
        self.signals[job_id] = action

    async def clear_signal(self, job_id):
        self.signals.pop(job_id, None)

    async def remove(self, job_id):
        pass

    async def get_progress_many(self, job_ids):
        # Redis 里的实时进度比库里新
        return {j: {"completed": "8", "failed": "1"} for j in job_ids}

    async def queued_position(self, job_id):
        return None


async def _running_job(worker_id: str | None) -> str:
    from sqlalchemy import select

    from app.db import session_scope
    from app.models import Job, JobStatus, User

    async with session_scope() as db:
        user = (await db.execute(select(User).where(User.username == "admin"))).scalar_one()
        job = Job(
            name="卡住的任务", user_id=user.id, status=JobStatus.running, worker_id=worker_id,
            input_filename="a.jsonl", input_path="/tmp/none.jsonl", total_items=20, completed_items=5,
        )
        db.add(job)
        await db.flush()
        return job.id


@pytest.mark.asyncio
async def test_pause_and_cancel_settle_jobs_whose_worker_is_gone(
    admin_client: AsyncClient, monkeypatch
):
    """重建镜像时 worker 在收尾前被强杀，没人响应信号，任务不能永远卡在「运行中」。"""
    import time

    import app.api.jobs as jobs_mod
    from app.db import session_scope
    from app.models import Job

    fake = _FakeControlQueue({"alive": time.time() - 5, "stale": time.time() - 120})
    monkeypatch.setattr(jobs_mod, "queue", fake)
    created: list[str] = []

    # worker 还活着：照旧只发信号，由 worker 自己收尾
    alive = await _running_job("alive")
    created.append(alive)
    out = (await admin_client.post(f"/api/jobs/{alive}/pause")).json()
    assert out["status"] == "running" and fake.signals[alive] == "pause"

    # worker 已经不在了：直接落暂停，计数取 Redis 里更新的那份
    gone = await _running_job("killed-worker")
    created.append(gone)
    out = (await admin_client.post(f"/api/jobs/{gone}/pause")).json()
    assert out["status"] == "paused" and out["worker_id"] is None
    assert out["completed_items"] == 8 and out["failed_items"] == 1
    # 信号留着：万一 worker 其实还活着，看到信号仍会停下
    assert fake.signals[gone] == "pause"
    # 落成暂停之后，取消走的就是「已暂停」那条正常路径
    out = (await admin_client.post(f"/api/jobs/{gone}/cancel")).json()
    assert out["status"] == "canceled" and gone not in fake.signals

    # 心跳超时的 worker：取消直接落终态
    stale = await _running_job("stale")
    created.append(stale)
    out = (await admin_client.post(f"/api/jobs/{stale}/cancel")).json()
    assert out["status"] == "canceled" and out["finished_at"]

    # 读不到心跳（Redis 抖动）时宁可不动，免得把正在跑的任务误改掉
    async def boom(worker_id):
        raise ConnectionError("redis down")

    monkeypatch.setattr(fake, "worker_last_seen", boom)
    flaky = await _running_job("whatever")
    created.append(flaky)
    out = (await admin_client.post(f"/api/jobs/{flaky}/cancel")).json()
    assert out["status"] == "running"

    async with session_scope() as db:
        for jid in created:
            await db.delete(await db.get(Job, jid))
