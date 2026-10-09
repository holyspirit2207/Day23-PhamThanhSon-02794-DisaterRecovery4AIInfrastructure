# Postmortem — DR Drill Lab 23

Theo đúng template §4 "Sau Failover: Blameless Postmortem". Blameless: câu hỏi là
"hệ thống/process nào cho phép chuyện này", không phải "ai làm sai".

## 1. Timeline (mọi dòng phải có evidence path:line)

| ISO time | Sự kiện | Evidence |
|---|---|---|
| `2026-10-09T04:47:49` | outage bắt đầu (Region A bị netblock) | `chaos/chaos-events.jsonl:5` |
| `2026-10-09T04:47:49` | user đầu tiên bị ảnh hưởng (ReadTimeout 503) | `reports/drill-2-withdr.jsonl:25` |
| `2026-10-09T04:48:04` | health check alert (Region A sang UNHEALTHY) | `reports/health-events.jsonl:2` |
| `2026-10-09T04:48:06` | operator confirm cutover (Runbook bước 2 thông báo) | `reports/runbook-run.jsonl:2` |
| `2026-10-09T04:48:12` | DNS cutover sang Region B hoàn tất | `reports/failover-events.jsonl:5` |
| `2026-10-09T04:48:17` | resolved (request đầu tiên OK 200 từ Region B) | `reports/drill-2-withdr.jsonl:39` |

## 2. RTO/RPO đo được vs mục tiêu — gap ở bước nào?

- RTO mục tiêu: 300s · đo được: `28.2s` · gap: `-271.8s` (vượt mục tiêu an toàn).
- RPO mục tiêu: 300s · đo được: `4.0s` (`2` doc bị mất) · gap: `-296.0s`.
- **Bước tốn nhiều giây nhất:** `Health-check detection floor (15.0s)` — vì hệ thống cấu hình `interval_s=5.0` và `threshold=3` liên tiếp để chống flapping. Giai đoạn này chiếm tới 53.2% tổng thời gian RTO (15.0s / 28.2s).

## 3. Root cause (5 whys)

1. **Tại sao user nhận lỗi 503?** Do Region A bị netblock (mất kết nối mạng đột ngột), request từ Edge Proxy bị treo đến timeout.
2. **Tại sao Edge Proxy không tự động failover ngay giây đầu tiên?** Vì Edge Proxy không tích hợp active probing trực tiếp, nó chỉ phân giải upstream theo record DNS trong `edge/active_region`.
3. **Tại sao hệ thống cần 15s mới phát hiện outage?** Vì Health Checker cần đợi đủ 3 lần thăm dò liên tiếp thất bại (`threshold=3`, mỗi lần cách nhau 5s) nhằm tránh false alarm do jitter mạng tạm thời.
4. **Tại sao Region B không thể phục vụ ngay khi Region A chết?** Vì Region B là mô hình Warm Standby rỗng (chưa replicate model weights và vector index mới nhất, GPU pool đang ở trạng thái `warm`).
5. **Hệ thống/process nào cho phép chuyện này?** Thiết kế chưa có cơ chế Active-Active hoặc Hot Standby với CDC streaming vector liên tục; việc snapshot/restore dựa trên batch file interval 30s tạo ra độ trễ RPO và RTO nhất định.

## 4. Action items (có owner + deadline)

| # | Action item | Owner | Deadline | Giảm RTO/RPO bao nhiêu giây |
|---|---|---|---|---|
| 1 | Giảm health check interval xuống 3s với adaptive backoff | SRE Team | 2026-11-01 | Giảm RTO 6.0s (từ 15s xuống 9s detect floor) |
| 2 | Duy trì GPU pool ở mức pre-warmed weights trong VRAM thay vì load từ disk | AI Infra Team | 2026-11-15 | Giảm RTO ~5.0s (warm-up pool) |
| 3 | Tăng tần suất continuous snapshot replication xuống 10s hoặc dùng CDC | Data Platform | 2026-12-01 | Giảm RPO tối đa xuống dưới 10s |

## 5. Ba câu hỏi bắt buộc trả lời

1. **`interval × threshold` của bạn là bao nhiêu giây? Nó chiếm bao nhiêu % RTO?**
   `5.0s × 3 = 15.0s`. Chiếm `53.19%` tổng thời gian RTO đo được (15.0s / 28.2s).
2. **Nếu hạ interval xuống 1s, RTO giảm mấy giây — và bạn trả giá gì (§4 flapping)?**
   Nếu hạ interval xuống 1s (với threshold=3), detect floor giảm từ 15s xuống 3s $\rightarrow$ RTO giảm 12s.
   Cái giá phải trả: Tăng đột biến nguy cơ **flapping** (chuyển đổi qua lại liên tục giữa các region) khi chỉ có packet drop nhẹ hoặc mạng lag nhất thời, gây tải đột biến, ngắt kết nối hàng loạt client và làm mất tính nhất quán dữ liệu.
3. **Nếu outage kéo dài 6 giờ và region chính mất dữ liệu vĩnh viễn, `docs_lost` của bạn có nghĩa gì với khách hàng?**
   `docs_lost = 2` tương ứng với 2 yêu cầu/giao dịch của khách hàng phát sinh trong khoảng thời gian 4 giây giữa lần snapshot cuối cùng và thời điểm xảy ra sự cố. Dữ liệu này bị mất vĩnh viễn và không thể khôi phục tự động từ replica; hệ thống cần cơ chế reconcile log hoặc thông báo khách hàng gửi lại giao dịch đó.
