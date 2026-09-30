"""Testler için sahte `imsg rpc`: senaryo dosyasına göre JSON-RPC yanıtları ve bildirimleri üretir.

Kullanım: python fake_imsg.py <senaryo.json>. Gelen her istek senaryodaki log_path dosyasına bir satır
olarak eklenir; testler istemcinin ne gönderdiğini buradan doğrular. Listeler istek sırasıyla tüketilir;
beklenmeyen istek fazlalığı IndexError ile süreci düşürür (test yüksek sesle başarısız olur).
"""
import json
import sys
from pathlib import Path


def emit(payload: dict) -> None:
    sys.stdout.write(json.dumps(payload, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def main() -> None:
    scenario: dict = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
    log = Path(scenario["log_path"])
    after_pages: list = list(scenario.get("after_pages", []))
    send_errors: list = list(scenario.get("send_errors", []))
    batches: list = list(scenario.get("subscribe_batches", []))
    exit_after = scenario.get("exit_after_requests")
    handled = 0
    subscription = 0
    while True:
        line = sys.stdin.readline()
        if not line:
            return
        request: dict = json.loads(line)
        with log.open("a", encoding="utf-8") as target:
            target.write(json.dumps(request, ensure_ascii=False) + "\n")
        handled += 1
        if exit_after is not None and handled > exit_after:
            sys.exit(3)
        method = request["method"]
        if method == "initialize":
            emit({"jsonrpc": "2.0", "id": request["id"], "result": scenario["status"]})
        elif method == "messages.after":
            emit({"jsonrpc": "2.0", "id": request["id"], "result": after_pages.pop(0)})
        elif method == "watch.subscribe":
            subscription += 1
            emit({"jsonrpc": "2.0", "id": request["id"], "result": {"subscription": subscription, "buffer_limit": 256}})
            for note in (batches.pop(0) if batches else []):
                emit({"jsonrpc": "2.0", "method": note["method"],
                      "params": {"subscription": subscription, **note["params"]}})
        elif method == "send":
            error = send_errors.pop(0) if send_errors else None
            if error is None:
                emit({"jsonrpc": "2.0", "id": request["id"],
                      "result": {"ok": True, "id": 900 + handled, "guid": f"sent-{handled}"}})
            else:
                emit({"jsonrpc": "2.0", "id": request["id"], "error": {"code": error, "message": "sahte hata"}})
        else:
            emit({"jsonrpc": "2.0", "id": request["id"], "error": {"code": -32601, "message": "bilinmeyen yöntem"}})


if __name__ == "__main__":
    main()
