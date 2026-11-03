#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""无 GPU 自测：起三个 mock 上游 + 真实的薯饼路由器，验证整条链路。

跑法： .venv/bin/python tests/selftest.py
验证：① 思考区顺序 = 云端 CoT → 〔rewrite3思维链〕 → 改文 CoT
      ② 正文 = 改文正文，云端原文没有漏出来
      ③ 改文请求里带上了 INSTRUCTION 和【分数】标记
      ④ jev 挂了 → 全部按【NA】继续改文
      ⑤ 改文模型挂了 → 降级直接输出云端原文
"""

import sys
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))  # 直接 import app.*

CLOUD_PORT, JEV_PORT, REWRITE_PORT, ROUTER_PORT = 18001, 18002, 18003, 18011

CLOUD_COT = "云端的原生思考：这段要写雨夜，人物站在门口，情绪克制。"
CLOUD_BODY = "夜里下起了雨。\n\n他站在门口，没有说话。"
STRUCTURED_BODY = (
    "结构化原文第一段，这句话应该被改掉。\n\n"
    "<StatusBlock>\n<status>\n时间：22:41\n好感度：12\n</status>\n</StatusBlock>\n"
    "<UpdateVariable>\n{\"op\":\"replace\",\"path\":\"/好感度\",\"value\":12}\n</UpdateVariable>"
)
HTML_BODY = ("<div class=\"ui-panel\">\n"
             "<style>.ui-panel{color:#fff}</style>\n"
             "<p>这是一整块前端面板，不是正文。</p>\n"
             "</div>\n"
             "<UpdateVariable>\n{\"op\":\"delta\",\"path\":\"/hp\",\"value\":-5}\n</UpdateVariable>")
REWRITE_COT = "先通读一遍。第一句 jevnovel 给了分，八股味有点重，压掉多余的修饰。"
REWRITE_BODY = "夜雨。\n\n他立在门口，没吭声。"

LAST = {"rewrite_prompt": "", "cloud_messages": None, "cloud_params": None}


class MockHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.0"

    def log_message(self, *args):
        pass

    def _json(self, code: int, obj: dict) -> None:
        raw = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def _read_body(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if not length:
            return {}
        return json.loads(self.rfile.read(length).decode("utf-8"))

    def _sse(self, chunks: list[dict]) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        for chunk in chunks:
            self.wfile.write(("data: " + json.dumps(chunk, ensure_ascii=False) + "\n\n").encode("utf-8"))
            self.wfile.flush()
            time.sleep(0.003)
        self.wfile.write(b"data: [DONE]\n\n")
        self.wfile.flush()


class CloudMock(MockHandler):
    def do_GET(self):  # noqa: N802
        self._json(200, {"object": "list", "data": [
            {"id": "mock-cloud", "object": "model", "owned_by": "test"},
            {"id": "deepseek-flash", "object": "model", "owned_by": "test"},
        ]})

    def do_POST(self):  # noqa: N802
        body = self._read_body()
        assert body.get("stream") is True
        assert body.get("messages"), "云端没收到 messages"
        LAST["cloud_messages"] = body.get("messages")
        LAST["cloud_params"] = body
        user_text = "".join(m.get("content") or "" for m in body["messages"] if m.get("role") == "user")
        if "结构化测试" in user_text:
            reply = STRUCTURED_BODY
        elif "前端跳过测试" in user_text:
            reply = HTML_BODY
        else:
            reply = CLOUD_BODY
        chunks = [{"choices": [{"index": 0, "delta": {"reasoning_content": CLOUD_COT}}]}]
        for i in range(0, len(reply), 12):
            chunks.append({"choices": [{"index": 0, "delta": {"content": reply[i:i + 12]}}]})
        chunks.append({"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]})
        self._sse(chunks)


class JevMock(MockHandler):
    def do_GET(self):  # noqa: N802
        if self.path == "/health":
            self._json(200, {"ok": True, "status": "ready"})
        else:
            self._json(404, {"error": "not found"})

    def do_POST(self):  # noqa: N802
        body = self._read_body()
        units = body.get("units") or []
        items = [
            {"i": i, "t": unit,
             "p": {"可以保留": 0.6, "需要修改": 0.4},
             "bad": 0.4, "pick": "需要修改", "heat": 0.4}
            for i, unit in enumerate(units)
        ]
        self._json(200, {"items": items, "sentences": items})


class RewriteMock(MockHandler):
    def do_GET(self):  # noqa: N802
        self._json(200, {"object": "list", "data": [{"id": "rewrite3-shorter-4B", "object": "model"}]})

    def do_POST(self):  # noqa: N802
        body = self._read_body()
        LAST["rewrite_prompt"] = body["messages"][0]["content"]
        full = f"<think>\n{REWRITE_COT}\n</think>\n\n{REWRITE_BODY}"
        chunks = [
            {"choices": [{"index": 0, "delta": {"content": full[i:i + 5]}}]}
            for i in range(0, len(full), 5)   # 故意切碎，压测 </think> 边界
        ]
        chunks.append({"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]})
        self._sse(chunks)


def _serve(handler_cls, port: int) -> ThreadingHTTPServer:
    server = ThreadingHTTPServer(("127.0.0.1", port), handler_cls)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def _sse_request(port: int, path: str, payload: dict) -> list[dict]:
    """请求自定义 SSE 接口（测试前端用），收集所有 data 事件。"""

    events: list[dict] = []
    with httpx.Client(timeout=60) as client:
        with client.stream("POST", f"http://127.0.0.1:{port}{path}", json=payload) as resp:
            assert resp.status_code == 200, resp.text
            for line in resp.iter_lines():
                if not line.startswith("data:"):
                    continue
                raw = line[5:].strip()
                if raw:
                    events.append(json.loads(raw))
    return events


def _wait_port(port: int, timeout: float = 20.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.5):
                return
        except OSError:
            time.sleep(0.1)
    raise RuntimeError(f"端口 {port} 没起来")


def _request(port: int, user_text: str = "写一段雨夜相遇。",
             model: str = "whatever") -> tuple[str, str]:
    reasoning, content = [], []
    with httpx.Client(timeout=60) as client:
        with client.stream(
            "POST",
            f"http://127.0.0.1:{port}/v1/chat/completions",
            json={
                "model": model,
                "stream": True,
                "temperature": 0.7,
                "top_k": 42,
                "frequency_penalty": 0.1,
                "messages": [
                    {"role": "system", "content": "你是角色扮演作者。"},
                    {"role": "user", "content": user_text},
                ],
            },
        ) as resp:
            assert resp.status_code == 200, resp.text
            for line in resp.iter_lines():
                if not line.startswith("data:"):
                    continue
                data = line[5:].strip()
                if data == "[DONE]":
                    break
                obj = json.loads(data)
                delta = (obj.get("choices") or [{}])[0].get("delta") or {}
                if delta.get("reasoning_content"):
                    reasoning.append(delta["reasoning_content"])
                if delta.get("content"):
                    content.append(delta["content"])
    return "".join(reasoning), "".join(content)


def main() -> None:
    servers = [_serve(CloudMock, CLOUD_PORT), _serve(JevMock, JEV_PORT), _serve(RewriteMock, REWRITE_PORT)]
    cfg_path = Path(tempfile.gettempdir()) / "rewrite_router_selftest_config.json"
    cfg_path.write_text(json.dumps({
        "host": "127.0.0.1",
        "port": ROUTER_PORT,
        "cloud": {"base_url": f"http://127.0.0.1:{CLOUD_PORT}/v1", "api_key": "sk-test",
                  "model": "mock-cloud"},
        "rewrite": {"base_url": f"http://127.0.0.1:{REWRITE_PORT}/v1", "model": "rewrite3-shorter-4B",
                    "instruction_file": "INSTRUCTION.txt", "max_tokens": 2048},
        "jev": {"base_url": f"http://127.0.0.1:{JEV_PORT}"},
        "protection": {"enabled": True, "min_prose_ratio": 0.0, "max_segments": 4,
                       "min_rewrite_chars": 0, "note_in_reasoning": True},
    }, ensure_ascii=False), encoding="utf-8")

    log_path = ROOT / "tests" / "_router.log"
    log_file = log_path.open("w", encoding="utf-8")
    env = {**os.environ, "REWRITE_ROUTER_CONFIG": str(cfg_path)}
    router = subprocess.Popen([sys.executable, "main.py"], cwd=ROOT, env=env,
                              stdout=log_file, stderr=subprocess.STDOUT)
    try:
        _wait_port(ROUTER_PORT)
        time.sleep(0.5)

        # ---- ① 正常链路 ----
        reasoning, content = _request(ROUTER_PORT)
        assert CLOUD_COT in reasoning, f"思考区缺少云端 CoT：{reasoning[:200]!r}"
        assert "〔rewrite3思维链〕" in reasoning, "思考区缺少 〔rewrite3思维链〕 标记"
        assert REWRITE_COT in reasoning, "思考区缺少改文 CoT"
        idx_cloud = reasoning.index(CLOUD_COT)
        idx_header = reasoning.index("〔rewrite3思维链〕")
        idx_rewrite = reasoning.index(REWRITE_COT)
        assert idx_cloud < idx_header < idx_rewrite, "思考区顺序不对"
        assert "夜里下起了雨" not in content, f"云端正文漏出来了：{content!r}"
        assert content.strip() == REWRITE_BODY, f"正文不是改文结果：{content!r}"
        prompt = LAST["rewrite_prompt"]
        assert "你是一位小说编辑" in prompt, "改文请求里没有 INSTRUCTION"
        assert "【" in prompt and "】" in prompt, "改文请求里没有【分数】标记"
        # 纯透传校验：云端收到的 messages 必须和 ST 发的一字不差
        expected = [
            {"role": "system", "content": "你是角色扮演作者。"},
            {"role": "user", "content": "写一段雨夜相遇。"},
        ]
        assert LAST["cloud_messages"] == expected, f"messages 被改动了：{LAST['cloud_messages']!r}"
        assert LAST["cloud_params"].get("top_k") == 42, "top_k 没有透传"
        assert LAST["cloud_params"].get("frequency_penalty") == 0.1, "frequency_penalty 没有透传"
        assert LAST["cloud_params"].get("model") == "whatever", \
            f"请求里的 model 没有透传：{LAST['cloud_params'].get('model')}"
        print("① 正常链路通过：云端CoT → 〔rewrite3思维链〕 → 改文CoT → 改写正文")
        print("   请求侧纯透传通过：预设/正文原样转发，top_k 等参数原样带过去")

        # ---- ⑤ 测试前端：直接改文接口 ----
        events = _sse_request(ROUTER_PORT, "/api/rewrite", {"text": CLOUD_BODY})
        kinds = [e["kind"] for e in events]
        assert "marked" in kinds, f"没有带分数输入事件：{kinds}"
        assert "content" in kinds and "done" in kinds, f"事件不全：{kinds}"
        reasoning_text = "".join(e.get("text", "") for e in events if e["kind"] == "reasoning")
        content_text = "".join(e.get("text", "") for e in events if e["kind"] == "content")
        marked_text = "".join(e.get("text", "") for e in events if e["kind"] == "marked")
        assert REWRITE_COT in reasoning_text, "直接改文没有回改文思维链"
        assert content_text.strip() == REWRITE_BODY, f"直接改文结果不对：{content_text!r}"
        assert "【" in marked_text and "】" in marked_text, "带分数输入里没有标记"
        done = [e for e in events if e["kind"] == "done"][0]
        assert done["stats"]["chars_in"] > 0 and done["stats"]["chars_out"] > 0
        stages = {(e.get("name"), e.get("status")) for e in events if e["kind"] == "stage"}
        assert ("jev", "start") in stages and ("rewrite", "start") in stages, f"阶段事件不全：{stages}"

        # 开头的状态栏保留段必须"压到正文开始前才吐"，不能抢在思维链前面出现
        structured = ("[2024|3|1|周五|22:41|便利店门口]\n[当前状态:良好|学力:B]\n\n"
                      + CLOUD_BODY)
        ev2 = _sse_request(ROUTER_PORT, "/api/rewrite", {"text": structured})
        kinds2 = [e["kind"] for e in ev2]
        first_content = kinds2.index("content")
        first_rewrite_stage = next(i for i, e in enumerate(ev2)
                                   if e["kind"] == "stage" and e.get("name") == "rewrite")
        assert first_content > first_rewrite_stage, "保留段抢在改文思维链之前当正文吐出来了"
        content2 = "".join(e.get("text", "") for e in ev2 if e["kind"] == "content")
        assert content2.startswith("[2024|3|1|周五|22:41|便利店门口]"), f"开头状态栏没保留：{content2[:60]!r}"
        assert "当前状态:良好|学力:B" in content2
        print("⑤ 测试前端「直接改文」接口通过（含保留段缓冲顺序）")

        # ---- ⑥ 测试前端：云端生成 → 改文接口 ----
        events = _sse_request(ROUTER_PORT, "/api/generate", {"prompt": "写一段雨夜相遇。"})
        kinds = [e["kind"] for e in events]
        assert "done" in kinds and "content" in kinds, f"事件不全：{kinds}"
        reasoning_text = "".join(e.get("text", "") for e in events if e["kind"] == "reasoning")
        content_text = "".join(e.get("text", "") for e in events if e["kind"] == "content")
        assert CLOUD_COT in reasoning_text and "〔rewrite3思维链〕" in reasoning_text
        assert content_text.strip() == REWRITE_BODY
        kinds = [e["kind"] for e in events]
        stages = {(e.get("name"), e.get("status")) for e in events if e["kind"] == "stage"}
        assert ("cloud", "start") in stages and ("cloud", "done") in stages, f"云端阶段事件缺失：{stages}"
        assert "cloud_input" in kinds and "cloud_output" in kinds, f"云端输入/输出事件缺失：{kinds}"
        print("⑥ 测试前端「云端生成 → 改文」接口通过")

        # ---- ② jev 挂了：全标【NA】，改文继续 ----
        servers[1].shutdown()
        servers[1].server_close()
        reasoning, content = _request(ROUTER_PORT)
        assert content.strip() == REWRITE_BODY, f"jev 挂了之后正文不对：{content!r}"
        assert "【NA】" in LAST["rewrite_prompt"], "jev 挂了之后没有降级成【NA】"
        # ---- ①b 前端结构保护：只改正文，结构块逐字节保留 ----
        reasoning, content = _request(ROUTER_PORT, "结构化测试")
        assert "<StatusBlock>" in content and "</StatusBlock>" in content, f"状态栏被改坏：{content!r}"
        assert '"op":"replace"' in content, f"变量更新 JSON 被改坏：{content!r}"
        assert REWRITE_BODY.strip() in content, f"正文没有被改写：{content!r}"
        assert "结构化原文第一段" not in content, "正文原文漏出来了"
        assert "〔rewrite3思维链〕" in reasoning
        # 模型列表透传 + 请求 model 原样透传
        with httpx.Client(timeout=10) as client:
            ids = {m["id"] for m in client.get(f"http://127.0.0.1:{ROUTER_PORT}/v1/models").json()["data"]}
        assert "mock-cloud" in ids and "deepseek-flash" in ids, f"模型列表没有透传云端：{ids}"
        print("①a 模型列表透传通过（/v1/models 原样返回云端列表）")

        print("①b 前端结构保护通过：状态栏/变量块逐字节保留，只改纯正文")

        # ---- ①c 整块前端面板：整条跳过改文 ----
        reasoning, content = _request(ROUTER_PORT, "前端跳过测试")
        assert HTML_BODY in content, f"前端面板没有原样保留：{content!r}"
        assert "跳过改文" in reasoning, "跳过提示没有出现在思维链里"
        print("①c 整块前端面板通过：检测为前端结构，整条跳过改文")

        print("② jev 挂掉通过：全部【NA】，改文照常")

        # ---- ③ 改文模型挂了：降级输出云端原文 ----
        servers[2].shutdown()
        servers[2].server_close()
        reasoning, content = _request(ROUTER_PORT)
        assert content.strip() == CLOUD_BODY.strip(), f"降级输出不对：{content!r}"
        assert CLOUD_COT in reasoning, "降级时云端 CoT 应该还在"
        print("③ 改文模型挂掉通过：降级输出云端原文，聊天不中断")

        # ---- ④ 长正文分段工具 ----
        from app.splitter import split_body_chunks
        long_text = "\n".join(["这是一段用来测试切块的正文，" * 30] * 20)
        chunks = split_body_chunks(long_text, 3000)
        assert len(chunks) > 1, "长正文没有切块"
        assert all(len(c) <= 3000 for c in chunks), "切块超长"
        assert "".join(chunks).replace("\n", "") == long_text.replace("\n", ""), "切块丢内容"
        print("④ 长正文分段通过")

        print("\nSELFTEST PASS ✅")
    finally:
        router.terminate()
        try:
            router.wait(timeout=5)
        except subprocess.TimeoutExpired:
            router.kill()
        for server in servers:
            try:
                server.shutdown()
                server.server_close()
            except Exception:  # noqa: BLE001
                pass
        log_file.close()


if __name__ == "__main__":
    main()
