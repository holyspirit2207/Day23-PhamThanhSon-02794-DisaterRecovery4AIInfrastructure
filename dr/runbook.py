"""BƯỚC 3c — SINH VIÊN VIẾT. Tự động hoá runbook §4 "Runbook: Region Chính Down".

7 bước trên slide, mỗi bước 1 dòng log có ts. Log này CHÍNH LÀ timeline của postmortem.
  1 xac_nhan_outage          — probe cả 2 region, đừng tin 1 lần fail (dùng nhiều lần
                              hoặc gọi health_checker.probe nếu đã viết xong 3a)
  2 thong_bao_incident       — ts của dòng này là mốc "operator biết tin", LUÔN LUÔN
                              SAU t_outage trong chaos-events (không thể trùng — operator
                              không thể biết ngay giây outage xảy ra). Ghi cả 2 ts vào
                              log để postmortem tính được "độ trễ thông báo".
  3 scale_gpu_pool           — gọi HÀM `failover.failover(...)` MỘT LẦN DUY NHẤT. Hàm
                              đó tự làm đủ 5 bước con (verify/restore/scale/wait/cutover)
                              và tự ghi log riêng vào reports/failover-events.jsonl.
  4 verify_state_replica     — KHÔNG gọi lại failover — chỉ ĐỌC kết quả (vector count +
                              weights ở region phụ) từ dict mà bước 3 trả về, để log vào
                              runbook-run.jsonl cho postmortem đọc 1 chỗ duy nhất.
  5 dns_cutover              — cũng chỉ đọc lại: kết quả cutover có ok hay không.
  6 verify_golden_signals    — 10 request thật vào region phụ: p95 latency + error rate
  7 post_incident            — elapsed_s + lệnh đo RTO

BÁN TỰ ĐỘNG, KHÔNG FULL-AUTO (§4: "failover đầu tiên nên là bán tự động — alert +
1-click confirm — tránh flapping gây failover 2 chiều liên tục"). Mặc định phải hỏi
người vận hành confirm; --auto chỉ dùng trong CI/khi chấm điểm.

Chạy:  python dr/runbook.py --primary a --target b --backend fs
"""
import argparse
import json
import pathlib
import sys
import time

import httpx

sys.path.insert(0, ".")
from dr import failover as fo  # noqa: E402
from dr.health_checker import probe  # noqa: E402

LOG = pathlib.Path("reports/runbook-run.jsonl")
URL = {"a": "http://127.0.0.1:8001", "b": "http://127.0.0.1:8002"}


def step(n: int, name: str, **kw):
    """Ghi 1 dòng {ts, iso, step, name, ...} vào LOG."""
    LOG.parent.mkdir(parents=True, exist_ok=True)
    rec = {
        "ts": time.time(),
        "iso": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime()),
        "step": n,
        "name": name,
        **kw,
    }
    with LOG.open("a", encoding="utf-8") as f:
        f.write(json.dumps(rec) + "\n")
    print(f"RUNBOOK [{n}. {name}]", json.dumps(rec))
    return rec


def confirm(auto: bool, msg: str) -> bool:
    """Auto=True -> True; ngược lại hỏi y/N. Đừng bỏ hàm này đi."""
    if auto:
        return True
    ans = input(f"{msg} [y/N]: ").strip().lower()
    return ans in ("y", "yes")


def run(primary: str, target: str, backend: str, auto: bool) -> dict:
    """7 bước ở trên."""
    t_start = time.time()

    # Bước 1: xac_nhan_outage — chờ tín hiệu cảnh báo từ health check để đảm bảo t_cutover >= t_detect
    health_log = pathlib.Path("reports/health-events.jsonl")
    chaos_path = pathlib.Path("chaos/chaos-events.jsonl")
    t_outage = None
    if chaos_path.exists():
        kills = [json.loads(l) for l in chaos_path.read_text().splitlines() if l.strip()]
        kills = [k for k in kills if k.get("action") == "kill" and k.get("region") == primary]
        if kills:
            t_outage = kills[-1]["ts"]

    confirmed_by_hc = False
    wait_hc_deadline = time.time() + 30.0
    while time.time() < wait_hc_deadline:
        if health_log.exists():
            lines = [json.loads(l) for l in health_log.read_text().splitlines() if l.strip()]
            for ev in lines:
                if (
                    ev.get("event") == "state_change"
                    and ev.get("to") == "UNHEALTHY"
                    and ev.get("region") == primary
                    and (t_outage is None or ev.get("ts", 0) >= t_outage)
                ):
                    confirmed_by_hc = True
                    break
        if confirmed_by_hc:
            break
        time.sleep(0.5)

    p_ready, p_reason = probe(primary, 1.5)
    t_ready, t_reason = probe(target, 1.5)
    step(
        1,
        "xac_nhan_outage",
        primary=primary,
        primary_ready=p_ready,
        primary_reason=p_reason,
        target=target,
        target_ready=t_ready,
        target_reason=t_reason,
        health_check_alert_received=confirmed_by_hc,
    )

    # Bước 2: thong_bao_incident
    now = time.time()
    notif_lag = round(now - t_outage, 2) if t_outage else None
    step(
        2,
        "thong_bao_incident",
        primary=primary,
        target=target,
        t_outage=t_outage,
        announced_at=now,
        notification_lag_s=notif_lag,
        severity="P1",
    )

    if not confirm(auto, f"Kích hoạt failover từ Region {primary} sang Region {target}?"):
        print("Failover bị hủy bởi người vận hành.")
        return {"ok": False, "aborted": True}

    # Bước 3: scale_gpu_pool (gọi hàm failover.failover DUY NHẤT 1 LẦN)
    fo_res = fo.failover(target=target, backend=backend, wait=60.0)
    step(3, "scale_gpu_pool", target=target, backend=backend, failover_ok=fo_res.get("ok"))
    if not fo_res.get("ok"):
        return {"ok": False, "error": "failover_failed", "detail": fo_res}

    # Bước 4: verify_state_replica (ĐỌC lại kết quả từ fo_res và state_of)
    st = fo.state_of(target)
    step(
        4,
        "verify_state_replica",
        target=target,
        rpo_seconds=fo_res.get("rpo_seconds"),
        docs_lost=fo_res.get("docs_lost"),
        embed_model_version=fo_res.get("embed_model_version"),
        target_state=st,
    )

    # Bước 5: dns_cutover
    act_file = pathlib.Path("edge/active_region")
    curr_active = act_file.read_text().strip() if act_file.exists() else None
    step(5, "dns_cutover", active_region=curr_active, cutover_verified=(curr_active == target))

    # Bước 6: verify_golden_signals (10 request thật vào region phụ)
    latencies = []
    errors = 0
    with httpx.Client(timeout=3.0) as client:
        for i in range(10):
            t_req = time.time()
            try:
                resp = client.get(f"{URL[target]}/v1/infer", params={"q": f"golden signal {i}"})
                if resp.status_code == 200:
                    latencies.append((time.time() - t_req) * 1000)
                else:
                    errors += 1
            except Exception:
                errors += 1
    latencies.sort()
    p95 = latencies[int(len(latencies) * 0.95)] if latencies else None
    step(
        6,
        "verify_golden_signals",
        requests_sent=10,
        errors=errors,
        error_rate=errors / 10.0,
        p95_latency_ms=round(p95, 1) if p95 else None,
    )

    # Bước 7: post_incident
    elapsed = round(time.time() - t_start, 2)
    measure_cmd = "python tools/measure_rto.py --loadgen reports/drill-2-withdr.jsonl --target-rto 300"
    step(7, "post_incident", elapsed_s=elapsed, measure_command=measure_cmd)

    return {
        "ok": True,
        "primary": primary,
        "target": target,
        "failover": fo_res,
        "elapsed_s": elapsed,
        "measure_command": measure_cmd,
    }


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--primary", default="a")
    p.add_argument("--target", default="b")
    p.add_argument("--backend", default="fs", choices=["fs", "minio"])
    p.add_argument("--auto", action="store_true")
    a = p.parse_args()
    print(json.dumps(run(a.primary, a.target, a.backend, a.auto), indent=2))
