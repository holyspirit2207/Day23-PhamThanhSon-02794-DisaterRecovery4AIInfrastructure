# Runbook 1 trang — Region chính down

Runbook tiêu chuẩn cho kỹ sư trực ca (On-call) thực thi khi xảy ra sự cố ngừng hoạt động tại Region chính (Region A). Quy trình bán tự động giúp kiểm soát an toàn và ngăn chặn hiện tượng flapping.

| # | Bước | Lệnh (Copy-pasteable) | Biết là xong khi | Ai làm |
|---|---|---|---|---|
| 1 | Xác nhận outage | `python -c "import httpx; [print(r, httpx.get('http://127.0.0.1:'+p+'/readyz').status_code) for r,p in [('a','8001'),('b','8002')]]"` | Region A trả về ReadTimeout hoặc non-200 liên tiếp 3 lần | On-call SRE |
| 2 | Mở incident + bấm giờ RTO | `python dr/runbook.py --primary a --target b --backend fs --auto` | Thông báo P1 Incident được phát đi, ghi timestamp vào `reports/runbook-run.jsonl` | Incident Commander |
| 3 | Restore state ở region phụ | `python state/snapshot.py get --region b --backend fs` | In ra metadata snapshot, file `state/region-b/vectors.sqlite` và `weights/model.bin` được khôi phục | Automation / SRE |
| 4 | Scale pool warm→full | `python -c "open('state/region-b/pool_state','w').write('full\n')"` | Endpoint `http://localhost:8002/readyz` trả về HTTP 200 OK sau thời gian warm-up | Automation / SRE |
| 5 | DNS/LB cutover | `python -c "open('edge/active_region','w',encoding='utf-8').write('b\n')"` | `curl.exe -s http://localhost:8080/edge/state` trả về `{"active_region":"b", ...}` | Automation / SRE |
| 6 | Verify golden signals | `python -c "import httpx; r=[httpx.get('http://127.0.0.1:8080/v1/infer').status_code for _ in range(10)]; print('200 count:', r.count(200))"` | 10/10 request trả về HTTP 200, error_rate = 0%, p95 latency < 100ms | On-call SRE |
| 7 | Đo RTO + postmortem | `python tools/measure_rto.py --loadgen reports/drill-2-withdr.jsonl --target-rto 300` | Kết quả trả về JSON hợp lệ với `"rto_verdict": "PASS"`, log ghi nhận RTO/RPO cụ thể | SRE Lead / Postmortem Owner |

---

## Điều kiện và Quy trình Rollback (Chuyển ngược về Region A)

> [!WARNING]
> **Tuyệt đối KHÔNG tự động rollback (No full-auto rollback without circuit breaker).**
> Tự động chuyển ngược mà không có kiểm tra kỹ lưỡng sẽ dẫn đến thảm họa flapping (hai region liên tục tranh quyền phục vụ, gây gián đoạn kép).

1. **Điều kiện kích hoạt Rollback:**
   * Region A đã được khôi phục phần cứng/mạng và duy trì trạng thái ổn định liên tục ít nhất **30 phút** (không có bất kỳ dropped packet hay spike latency nào).
   * Dữ liệu vector DB phát sinh mới tại Region B trong thời gian outage đã được đồng bộ ngược (reverse replication) về Region A đầy đủ (`rpo_seconds == 0`).
   * Kiểm tra `/readyz` của Region A đạt HTTP 200 OK (`weights=true`, `pool_state=full`, `count >= Region B count`).
2. **Thẩm quyền quyết định:**
   * Phải có sự đồng thuận và phê duyệt chính thức từ **Incident Commander (IC)** hoặc **Engineering Lead**. Kỹ sư trực ca không tự ý rollback thủ công khi chưa có lệnh.
3. **Thao tác Rollback:**
   ```powershell
   # 1. Reverse sync dữ liệu từ B sang A
   python state/snapshot.py put --region b --backend fs
   python state/snapshot.py get --region a --backend fs

   # 2. Chuyển DNS về Region A
   python -c "open('edge/active_region','w',encoding='utf-8').write('a\n')"

   # 3. Kiểm tra kiểm chứng Edge
   curl.exe -s http://localhost:8080/edge/state
   ```
