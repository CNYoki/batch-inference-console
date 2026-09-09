"""邮件通知：模板渲染、发送判定、SMTP 交互、以及不影响任务的兜底。"""
from __future__ import annotations

import contextlib
import email
import socket
import threading
from datetime import UTC, datetime, timedelta

import pytest

from app.core.security import encrypt_secret
from app.models import Job, JobStatus, User
from app.services import mailer
from app.services.settings_store import DEFAULTS


def make_runtime(**overrides) -> dict:
    runtime = dict(DEFAULTS)
    runtime.update({
        "smtp_enabled": True, "smtp_host": "127.0.0.1", "smtp_port": 2525,
        "smtp_security": "none", "smtp_from": "noreply@example.com",
        "smtp_from_name": "批量推理平台",
    })
    runtime.update(overrides)
    return runtime


def make_job(**overrides) -> Job:
    data = dict(
        id="j" * 32, name="标注任务", user_id="u1", status=JobStatus.completed,
        input_filename="a.jsonl", input_path="", input_size=0,
        total_items=1000, completed_items=985, failed_items=15,
        prompt_tokens=1200, completion_tokens=450,
        started_at=datetime.now(UTC) - timedelta(minutes=42, seconds=10),
        finished_at=datetime.now(UTC),
    )
    data.update(overrides)
    return Job(**data)


def make_user(**overrides) -> User:
    data = dict(id="u1", username="alice", email="alice@example.com",
                display_name="爱丽丝", notify_email=True)
    data.update(overrides)
    return User(**data)


# --------------------------------------------------------------------------- #
# 模板渲染
# --------------------------------------------------------------------------- #
def test_render_replaces_known_and_keeps_unknown():
    out = mailer.render("你好 {{name}}，{{nope}}", {"name": "小明"})
    assert out == "你好 小明，{{nope}}"   # 未知变量原样保留，便于发现拼错


def test_render_escapes_in_html_mode():
    out = mailer.render("{{v}}", {"v": "<b>粗体</b> & 符号"}, as_html=True)
    assert "&lt;b&gt;" in out and "&amp;" in out
    plain = mailer.render("{{v}}", {"v": "<b>粗体</b>"}, as_html=False)
    assert plain == "<b>粗体</b>"


def test_build_values_covers_template_placeholders():
    values = mailer.build_values(make_job(), make_user(), "Qwen3")
    for key in ("job_name", "status_label", "total", "completed", "failed",
                "duration", "tokens", "model", "username", "job_url", "error_block"):
        assert key in values
    assert values["status_label"] == "已完成（含失败条目）"
    assert values["tokens"] == 1650
    assert "42 分" in values["duration"]
    assert values["job_url"].endswith("/jobs/" + "j" * 32)


def test_default_template_renders_without_leftover_placeholders():
    runtime = make_runtime()
    mail = mailer.build_job_mail(make_job(), make_user(), "Qwen3", runtime)
    assert "{{" not in mail.subject
    assert "{{" not in mail.body
    assert "标注任务" in mail.subject
    assert "985" in mail.body


def test_error_block_only_appears_on_failure():
    ok = mailer.build_job_mail(make_job(), make_user(), None, make_runtime())
    assert "错误：" not in ok.body
    bad = mailer.build_job_mail(
        make_job(status=JobStatus.failed, error="模型端点不可达"), make_user(), None, make_runtime()
    )
    assert "模型端点不可达" in bad.body
    assert "异常终止" in bad.subject


# --------------------------------------------------------------------------- #
# 发送判定
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("runtime_kw,user_kw,job_kw,expect,reason_part", [
    ({"smtp_enabled": False}, {}, {}, False, "未启用"),
    ({}, {"notify_email": False}, {}, False, "已关闭"),
    ({}, {"email": None}, {}, False, "没有邮箱"),
    ({"notify_on_success": False}, {}, {}, False, "未开启成功通知"),
    ({"notify_on_failure": False}, {}, {"status": JobStatus.failed}, False, "未开启失败通知"),
    ({}, {}, {"status": JobStatus.canceled}, False, "未开启取消通知"),
    ({"notify_on_canceled": True}, {}, {"status": JobStatus.canceled}, True, ""),
    ({"notify_min_items": 5000}, {}, {}, False, "少于 5000"),
    ({}, {}, {"status": JobStatus.paused}, False, "不触发"),
    ({}, {}, {}, True, ""),
    ({}, {}, {"status": JobStatus.succeeded}, True, ""),
    ({}, {}, {"status": JobStatus.failed}, True, ""),
])
def test_should_notify(runtime_kw, user_kw, job_kw, expect, reason_part):
    ok, reason = mailer.should_notify(
        make_job(**job_kw), make_user(**user_kw), make_runtime(**runtime_kw)
    )
    assert ok is expect
    assert reason_part in reason


# --------------------------------------------------------------------------- #
# 真的起一个 SMTP 服务器收信
# --------------------------------------------------------------------------- #
class TinySMTPServer:
    """够用的 SMTP 实现：能完成握手、认证、收一封信。"""

    def __init__(self, require_auth: bool = False):
        self.sock = socket.socket()
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(1)
        self.port = self.sock.getsockname()[1]
        self.require_auth = require_auth
        self.received: list[str] = []
        self.auth_seen = False
        self.thread = threading.Thread(target=self._serve, daemon=True)

    def start(self) -> None:
        self.thread.start()

    def _serve(self) -> None:
        conn, _ = self.sock.accept()
        with conn:
            fh = conn.makefile("rwb")
            conn.sendall(b"220 tiny ESMTP\r\n")
            data_mode = False
            payload: list[str] = []
            while True:
                line = fh.readline()
                if not line:
                    break
                text = line.decode("utf-8", errors="replace")
                if data_mode:
                    if text.strip() == ".":
                        data_mode = False
                        self.received.append("".join(payload))
                        conn.sendall(b"250 OK\r\n")
                        payload = []
                    else:
                        payload.append(text)
                    continue

                cmd = text.strip().upper()
                if cmd.startswith("EHLO") or cmd.startswith("HELO"):
                    conn.sendall(b"250-tiny\r\n250 AUTH PLAIN LOGIN\r\n")
                elif cmd.startswith("AUTH"):
                    self.auth_seen = True
                    conn.sendall(b"235 authenticated\r\n")
                elif cmd.startswith("MAIL FROM") or cmd.startswith("RCPT TO"):
                    conn.sendall(b"250 OK\r\n")
                elif cmd == "DATA":
                    if self.require_auth and not self.auth_seen:
                        conn.sendall(b"530 auth required\r\n")
                        continue
                    data_mode = True
                    conn.sendall(b"354 send data\r\n")
                elif cmd == "QUIT":
                    conn.sendall(b"221 bye\r\n")
                    break
                else:
                    conn.sendall(b"250 OK\r\n")

    def close(self) -> None:
        with contextlib.suppress(OSError):
            self.sock.close()


def test_send_reaches_a_real_smtp_server():
    server = TinySMTPServer()
    server.start()
    try:
        runtime = make_runtime(smtp_port=server.port, smtp_security="none")
        mail = mailer.build_job_mail(make_job(), make_user(), "Qwen3", runtime)
        mailer.send_sync(mail, runtime)
    finally:
        server.close()
    server.thread.join(timeout=5)

    assert len(server.received) == 1
    msg = email.message_from_string(server.received[0])
    assert msg["To"] == "alice@example.com"
    # 中文主题会被 RFC2047 编码，解回来比对
    subject = str(email.header.make_header(email.header.decode_header(msg["Subject"])))
    assert "标注任务" in subject
    body = msg.get_payload(decode=True).decode("utf-8")
    assert "爱丽丝" in body and "985" in body


def test_send_performs_auth_when_username_set():
    server = TinySMTPServer(require_auth=True)
    server.start()
    try:
        runtime = make_runtime(
            smtp_port=server.port, smtp_security="none",
            smtp_username="user@example.com",
            smtp_password_encrypted=encrypt_secret("s3cret"),
        )
        mail = mailer.Mail(to="a@b.c", subject="hi", body="body")
        mailer.send_sync(mail, runtime)
    finally:
        server.close()
    server.thread.join(timeout=5)
    assert server.auth_seen is True
    assert len(server.received) == 1


def test_send_reports_unreachable_host_clearly():
    runtime = make_runtime(smtp_host="127.0.0.1", smtp_port=1, smtp_security="none")
    with pytest.raises(mailer.MailError, match="无法连接"):
        mailer.send_sync(mailer.Mail(to="a@b.c", subject="s", body="b"), runtime)


def test_send_requires_host_and_sender():
    with pytest.raises(mailer.MailError, match="SMTP 服务器地址"):
        mailer.send_sync(mailer.Mail("a@b.c", "s", "b"), make_runtime(smtp_host=""))
    with pytest.raises(mailer.MailError, match="发件人"):
        mailer.send_sync(
            mailer.Mail("a@b.c", "s", "b"), make_runtime(smtp_from="", smtp_username="")
        )


def test_undecryptable_password_is_reported():
    runtime = make_runtime(smtp_password_encrypted="not-a-valid-fernet-token")
    with pytest.raises(mailer.MailError, match="解密失败"):
        mailer.send_sync(mailer.Mail("a@b.c", "s", "b"), runtime)


def test_html_mail_has_plain_text_fallback():
    server = TinySMTPServer()
    server.start()
    try:
        runtime = make_runtime(
            smtp_port=server.port, smtp_security="none", mail_html=True,
            mail_body_template="<p>任务 <b>{{job_name}}</b> {{status_label}}</p>",
        )
        mailer.send_sync(mailer.build_job_mail(make_job(), make_user(), None, runtime), runtime)
    finally:
        server.close()
    server.thread.join(timeout=5)

    msg = email.message_from_string(server.received[0])
    assert msg.is_multipart()
    types = {part.get_content_type() for part in msg.walk() if part.get_content_type().startswith("text/")}
    assert types == {"text/plain", "text/html"}


# --------------------------------------------------------------------------- #
# notify_job：编排逻辑与「绝不影响任务」的兜底
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_notify_job_sends_for_finished_job(monkeypatch):
    from sqlalchemy import select

    from app.db import session_scope
    from app.models import Job
    from app.models import User as UserModel
    from app.services import notifications
    from app.services.settings_store import write_runtime

    sent: list = []

    async def fake_to_thread(fn, mail, runtime):
        sent.append(mail)

    monkeypatch.setattr(notifications.asyncio, "to_thread", fake_to_thread)

    async with session_scope() as db:
        await write_runtime(db, {
            "smtp_enabled": True, "smtp_host": "127.0.0.1", "smtp_from": "bot@example.com",
            "notify_on_success": True, "notify_on_failure": True,
        })
        user = (await db.execute(select(UserModel).where(UserModel.username == "admin"))).scalar_one()
        user.email = "admin@example.com"
        user.notify_email = True
        job = Job(
            name="通知用例", user_id=user.id, status=JobStatus.succeeded,
            input_filename="a.jsonl", input_path="", input_size=0,
            total_items=10, completed_items=10,
        )
        db.add(job)
        await db.flush()
        job_id = job.id

    assert await notifications.notify_job(job_id) == "已发送"
    assert len(sent) == 1
    assert sent[0].to == "admin@example.com"
    assert "通知用例" in sent[0].subject

    async with session_scope() as db:
        await write_runtime(db, {"smtp_enabled": False})


@pytest.mark.asyncio
async def test_notify_job_never_raises_when_sending_fails(monkeypatch):
    """发信炸了也只能返回错误说明，绝不能把异常抛回给 worker。"""
    from sqlalchemy import select

    from app.db import session_scope
    from app.models import Job
    from app.models import User as UserModel
    from app.services import notifications
    from app.services.settings_store import write_runtime

    async def boom(fn, mail, runtime):
        raise mailer.MailError("模拟的 SMTP 故障")

    monkeypatch.setattr(notifications.asyncio, "to_thread", boom)

    async with session_scope() as db:
        await write_runtime(db, {
            "smtp_enabled": True, "smtp_host": "127.0.0.1", "smtp_from": "bot@example.com",
        })
        user = (await db.execute(select(UserModel).where(UserModel.username == "admin"))).scalar_one()
        user.email = "admin@example.com"
        user.notify_email = True
        job = Job(
            name="失败通知用例", user_id=user.id, status=JobStatus.failed,
            error="模型端点不可达", input_filename="a.jsonl", input_path="", input_size=0,
            total_items=5,
        )
        db.add(job)
        await db.flush()
        job_id = job.id

    result = await notifications.notify_job(job_id)   # 不抛异常
    assert "发送失败" in result and "模拟的 SMTP 故障" in result

    async with session_scope() as db:
        await write_runtime(db, {"smtp_enabled": False})


@pytest.mark.asyncio
async def test_notify_job_handles_missing_job():
    from app.services import notifications
    assert await notifications.notify_job("0" * 32) == "任务不存在"


@pytest.mark.asyncio
async def test_fail_job_path_triggers_notification(monkeypatch):
    """任务级失败走的是 _fail_job 这条独立分支，同样要发通知。"""
    from sqlalchemy import select

    from app.db import session_scope
    from app.models import Job
    from app.models import User as UserModel
    from app.worker import runner as runner_mod

    called: list[str] = []

    async def fake_notify(job_id: str) -> str:
        called.append(job_id)
        return "已发送"

    monkeypatch.setattr(runner_mod, "notify_job", fake_notify)

    async with session_scope() as db:
        user = (await db.execute(select(UserModel).where(UserModel.username == "admin"))).scalar_one()
        job = Job(
            name="fail-path", user_id=user.id, status=JobStatus.running,
            input_filename="a.jsonl", input_path="/nope.jsonl", input_size=0, total_items=1,
        )
        db.add(job)
        await db.flush()
        job_id = job.id

    r = runner_mod.JobRunner(job_id, "test-worker")
    monkeypatch.setattr(runner_mod.queue, "clear_signal", lambda *a, **k: _noop())
    await r._fail_job("输入文件已丢失")

    assert called == [job_id]
    async with session_scope() as db:
        job = await db.get(Job, job_id)
        assert job.status == JobStatus.failed
        assert job.error == "输入文件已丢失"


async def _noop():
    return None
