#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""jevnovel 0.8B 打分服务（单文件，不需要站点那套）。

只做一件事：POST /v1/score_units 给一批已经切好的句子打「烂」概率。

用法（python 环境要有 torch + transformers）：
  python tools/serve_jev.py --model-dir /path/to/jev-0.8B-1007-bf16 --port 8885

模型包从 https://huggingface.co/aikexue170/jev-novel-3-0.8b-fp8 下载（bf16 版同理），
目录里带 infer.py 就是能跑的学生包。
"""
import argparse
import json
import sys

for _stream in (sys.stdout, sys.stderr):          # Windows 控制台统一 UTF-8
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

STATE = {
    "predictor": None,
    "lock": threading.Lock(),
    "max_chars": 300,
    "threshold": 0.08,
    "model_dir": "",
    "started": 0.0,
}


def _score_unit(text: str):
    """给一个句子打分。过长按 max_chars 切块取最烂；切不出片返回 None。"""
    predictor = STATE["predictor"]
    max_chars = STATE["max_chars"]
    if len(text) <= max_chars:
        try:
            return float(predictor.scan(text)["p_bad"])
        except Exception:  # noqa: BLE001
            return None
    values = []
    for i in range(0, len(text), max_chars):
        try:
            values.append(float(predictor.scan(text[i:i + max_chars])["p_bad"]))
        except Exception:  # noqa: BLE001
            continue
    return max(values) if values else None


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):  # 安静一点
        pass

    def _send(self, code: int, obj: dict) -> None:
        raw = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def _read_json(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if not length:
            return {}
        return json.loads(self.rfile.read(length).decode("utf-8"))

    def do_GET(self):  # noqa: N802
        if self.path == "/health":
            self._send(200, {
                "ok": STATE["predictor"] is not None,
                "status": "ready" if STATE["predictor"] is not None else "loading",
                "model_id": Path(STATE["model_dir"]).name,
                "uptime_s": round(time.time() - STATE["started"], 1),
                "max_chars": STATE["max_chars"],
                "threshold": STATE["threshold"],
            })
        elif self.path == "/v1/models":
            self._send(200, {"data": [{"id": "jevnovel-3-0.8b", "object": "model"}]})
        else:
            self._send(404, {"error": "not found"})

    def do_POST(self):  # noqa: N802
        if self.path not in ("/v1/score_units", "/v1/score"):
            self._send(404, {"error": "not found"})
            return
        try:
            body = self._read_json()
        except Exception as exc:  # noqa: BLE001
            self._send(400, {"error": f"bad json: {exc}"})
            return
        units = body.get("units") or body.get("sentences") or []
        if not isinstance(units, list) or not units:
            self._send(400, {"error": "units 不能为空（本服务只打分，不负责切句）"})
            return
        items = []
        t0 = time.time()
        with STATE["lock"]:                      # 单卡串行前向，避免抢显存
            for i, unit in enumerate(units):
                text = str(unit)
                p_bad = _score_unit(text)
                if p_bad is None:
                    items.append({"i": i, "t": text, "p": {}, "bad": None,
                                  "pick": "跳过", "heat": None})
                    continue
                p = round(min(1.0, max(0.0, p_bad)), 4)
                items.append({
                    "i": i, "t": text,
                    "p": {"可以保留": round(1.0 - p, 4), "需要修改": p},
                    "bad": p,
                    "pick": "需要修改" if p >= STATE["threshold"] else "可以保留",
                    "heat": p,
                })
        self._send(200, {
            "model_id": Path(STATE["model_dir"]).name,
            "sentences": items,
            "items": items,
            "seconds": round(time.time() - t0, 2),
            "decision_threshold": STATE["threshold"],
        })


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--model-dir", required=True, help="jev 0.8B 学生包目录（含 infer.py）")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8885)
    parser.add_argument("--device", default="cuda:0")
    parser.add_argument("--max-chars", type=int, default=300)
    parser.add_argument("--threshold", type=float, default=0.08)
    args = parser.parse_args()

    model_dir = Path(args.model_dir).resolve()
    if not (model_dir / "infer.py").exists():
        raise SystemExit(f"{model_dir} 里没有 infer.py，不像 jev 学生包目录")
    sys.path.insert(0, str(model_dir))

    print(f"[jev] 加载模型 {model_dir}（device={args.device}）……", flush=True)
    t0 = time.time()
    from infer import Predictor  # noqa: E402  模型包自带
    STATE["predictor"] = Predictor(str(model_dir), device=args.device)
    STATE["model_dir"] = str(model_dir)
    STATE["max_chars"] = args.max_chars
    STATE["threshold"] = args.threshold
    STATE["started"] = time.time()
    print(f"[jev] 就绪，用时 {time.time() - t0:.1f}s | http://{args.host}:{args.port}", flush=True)
    print(f"[jev] POST /v1/score_units  GET /health", flush=True)

    ThreadingHTTPServer((args.host, args.port), Handler).serve_forever()


if __name__ == "__main__":
    main()
