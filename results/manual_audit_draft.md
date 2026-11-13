# Day 06 Manual Audit Draft — Coherence, Metadata & Grounding (A / B / C)

> **Minh bạch phương pháp chấm (`GUIDE.md` & `RULES.md`)**: Bảng chấm nháp này đối chiếu trực tiếp từng đoạn text trong [manual_audit_worksheet.md](manual_audit_worksheet.md) (do AI hỗ trợ tổng hợp theo đúng dữ liệu đã chạy; người chấm cuối cùng là tác giả bài làm cần rà lại toàn bộ nhãn để tránh thiên lệch xác nhận trước khi chốt vào báo cáo CP6).
> **Thang điểm**: Coherence `0|1|2` (`0` = cắt giữa ý/thiếu chủ thể không tự hiểu được, `1` = hiểu một phần/thiếu ngữ cảnh hoặc rách ranh giới trang, `2` = trọn ý tự hiểu được); Grounding `0|1|2` (`0` = không có bằng chứng, `1` = có nhưng thiếu điều kiện/ngữ cảnh hoặc bị cắt vụn phải ghép thủ công, `2` = đủ trả lời và truy nguồn rõ ràng).

---

## 1. Chunk Coherence (10 vị trí cố định `pos_01`–`pos_10`, xác định trước khi xem điểm)

| Position | Tài liệu & Trang | Điểm A | Lý do chi tiết Strategy A (`fixed 500/50`) | Điểm B / C | Lý do chi tiết Strategy B & C (`by_paragraph <=900`) |
|---|---|---:|---|---:|---|
| `pos_01` | `nist-ai-100-1` p.6 (`executive_summary`) | 1 | `c01` mở đầu tốt nhưng bị cắt cụt `"environment, a"`; `c02` mở đầu bằng mảnh chữ `"izations..."` và cắt ngang `"AI s"` giữa định nghĩa AI system. | 2 | `B:p006:c01` (720 chars) giữ trọn 4 câu mở đầu Executive Summary từ đầu đoạn tới hết phân loại rủi ro, không đứt câu/từ. |
| `pos_02` | `nist-ai-100-1` p.7 (`executive_summary`) | 1 | `c01` cắt cụt `"resul"` giữa câu Responsible AI; `c02` bắt đầu bằng `"EC TR 24368:2022)"` và cắt cụt `"As"` ở cuối chunk. | 2 | `B:p007:c01` (556 chars) giữ đủ 3 khái niệm (Social responsibility, Sustainability, Responsible AI) trọn vẹn hoàn chỉnh. |
| `pos_03` | `nist-ai-100-1` p.8 (`executive_summary`) | 0 | Cả `c01` lẫn `c02` mất chủ ngữ đầu câu do ngắt trang từ p.7 (`"valid and reliable..."`), đồng thời `c01` cắt từ `"managem"` và `c02` bắt đầu bằng `"es to all stages..."` rồi cắt `"co"`. | 1 | `B:p008:c01` (847 chars) giữ trọn toàn bộ đoạn Core 4 functions + GOVERN + Playbook URL nhưng vẫn mở đầu thiếu chủ ngữ trang trước (`"valid and reliable..."`) vì chunking theo từng trang PDF. |
| `pos_04` | `nist-ai-100-1` p.9 (`framing_risk`) | 1 | `c01` cắt ngang định nghĩa rủi ro `"composite mea"`; `c02` cắt giữa danh sách thành phần rủi ro `"circumstance or event occ"`. | 2 | `B:p009:c02` (789 chars) giữ đầy đủ heading `1.1`, định nghĩa risk + cả 2 thành phần `(1)` và `(2)` + đối tượng chịu tác hại. |
| `pos_05` | `nist-ai-100-1` p.10 (`framing_risk`) | 1 | `c01` cắt giữa từ `"quantita"`; `c02` mở bằng `"ately understood..."` và cắt cụt `"softw"` giữa ví dụ rủi ro bên thứ ba. | 2 | `B:p010:c01` (696 chars) giữ trọn chú thích Fig. 1, heading `1.2`/`1.2.1` và nguyên đoạn định nghĩa thách thức đo lường rủi ro. |
| `pos_06` | `nist-ai-600-1` p.5 (`introduction`) | 1 | `c01` cắt giữa cụm `"AI products,"`; `c02` bắt đầu từ `", development, use..."` và cắt `"requirements and"` giữa câu. | 2 | `B:p005:c01` (524 chars) giữ trọn đoạn mở đầu `1. Introduction` về mục đích của Generative AI Profile. |
| `pos_07` | `nist-ai-600-1` p.6 (`introduction`) | 1 | `c01` cắt giữa `"Incident D"`; `c02` mất chủ ngữ câu liệt kê bốn trọng tâm của GAI PWG và cắt `"NIST’s Tru"`. | 2 | `B:p006:c01` (633 chars) giữ đầy đủ cả 4 câu giới thiệu GAI PWG và 4 trọng tâm của hồ sơ GAI. |
| `pos_08` | `nist-ai-600-1` p.7 (`gai_risks_overview`) | 0 | `c01` mở bằng đoạn dở dang từ p.6 (`"the abuse, misuse..."`) thiếu mở bullet và cắt `"fall a"`; `c02` mở bằng `"tions."` và cắt `"based on th"`. | 1 | `B:p007:c01` (878 chars) vẫn mang đoạn nối trang từ p.6 ở dòng đầu, nhưng bảo toàn nguyên vẹn mục `Time scale` và toàn bộ đoạn yếu tố biến thiên rủi ro. |
| `pos_09` | `nist-ai-600-1` p.8 (`gai_risks_overview`) | 1 | Mục 1–2 đọc được, nhưng mục 3 cắt `"access"` và mục 5 bị cắt cụt tiêu đề `"Environmental Im"`. | 2 | `B:p008:c01` (741 chars) gom gọn 3 mục đầu (`CBRN`, `Confabulation`, `Dangerous, Violent, or Hateful Content`) trọn vẹn từ đầu tới cuối. |
| `pos_10` | `nist-ai-600-1` p.9 (`gai_risks_overview`) | 0 | `c01` mất đầu câu từ p.8 (`"operations..."`) và cắt ngang tên mục 11 `"Obscene, Degrading"`; `c02` mở bằng `"ism or illegal replication."` và cắt cuối mục 12 `"increased"`. | 1 | `B:p009:c01` (734 chars) vẫn bị khuyết phần mở mục 9 từ p.8 (`"operations..."`), nhưng giữ nguyên vẹn mục 10 (`Intellectual Property`) và mục 11 (`Obscene, Degrading, and/or Abusive Content`). |

**Tổng hợp Coherence (10 vị trí):**
- **Strategy A**: `0` điểm = 3, `1` điểm = 7, `2` điểm = 0 → **Trung bình = `0.70 / 2`** (`7/10`).
- **Strategy B & C**: `0` điểm = 0, `1` điểm = 3 (`pos_03`, `pos_08`, `pos_10` do ngắt trang PDF vật lý), `2` điểm = 7 → **Trung bình = `1.70 / 2`** (`17/10`).

---

## 2. Metadata Usefulness (5 câu test × top-3 = 15 bản ghi mỗi chiến lược)

| Chiến lược | Bản ghi metadata đủ & đúng | Tỷ lệ tuân thủ filter (`test`) | Lỗi cụ thể phát hiện khi đối chiếu text nguồn |
|---|---:|---:|---|
| **Strategy A** | **14 / 15** | **N/A** (2 câu có filter không áp dụng lọc) | **1 lỗi phân vùng section**: trong `q19` Rank 2 (`A:nist-ai-100-1:p036:c03`), nội dung chunk là mục `"5.4 Manage The MANAGE function entails allocating risk resources..."` nhưng metadata vẫn ghi `section="measure"` do `fixed_size()` kế thừa nhãn đầu trang 36. |
| **Strategy B** | **15 / 15** | **N/A** (không lọc) | Không phát hiện sai lệch `source`, `page`, `year`, `document_type` hay `section` trong 15 kết quả top-3 của tập `test` (`by_paragraph` cập nhật đúng `section="manage"` tại ranh giới `5.4 Manage`). |
| **Strategy C** | **15 / 15** | **2 / 2** (`q21`, `q22`) | Không phát hiện sai lệch; cả 3 chunk top-3 của `q21` (`year=2024, section=measure`) và `q22` (`document_type=generative_ai_profile, section=manage`) đều tuân thủ chặt bộ lọc SQL. |

- **Một lợi ích đã chứng minh**: Cặp `year`/`document_type` + `section` giúp chiến lược C chặn hoàn toàn kết quả nhiễu từ mục khác hoặc tài liệu khác (ví dụ trên `q22`, chiến lược B không lọc đưa `B:nist-ai-600-1:p018:c02` thuộc `section="govern"` lên Rank 1 với score `0.7774`, còn chiến lược C lọc đúng `section="manage"` đẩy đoạn trả lời `MG-1.3-001` ở p.44 lên Rank 1).
- **Một hạn chế quan trọng**: Filter `section` phụ thuộc regex nhận diện heading và danh mục trang manifest; với `fixed_size` không tách theo heading (`A`), đoạn chuyển mục giữa trang bị gán nhầm nhãn section đầu trang (`A:nist-ai-100-1:p036:c03`), và nếu người dùng nhập sai hoặc thiếu filter `section`, SQL `WHERE` sẽ thu hẹp quá mức.

---

## 3. Grounding Quality (5 câu test `q18`–`q22`, chấm top-3 mỗi chiến lược)

| Câu test | A (Score & Lý do) | B (Score & Lý do) | C (Score & Lý do) |
|---|---|---|---|
| `q18` (GOVERN cross-cutting, `100-1` p.27) | **2** — Rank 1 (`p.27:c01`) chứa trọn câu evidence giải thích GOVERN là cross-cutting và lý do tích hợp vào các hàm khác; truy được nguồn/trang rõ. | **2** — Rank 1 (`p.27:c01`) giữ trọn vẹn đoạn giải thích GOVERN và bối cảnh lifecycle; không có tạp chất. | **2** — Không có filter, kết quả giống B (`Rank 1 p.27:c01`), đủ hoàn toàn để trả lời và truy nguồn. |
| `q19` (MAP foundation for MEASURE & MANAGE, `100-1` p.30) | **2** — Rank 1 (`p.30:c02`) chứa đủ cả 2 câu gold evidence dù đầu chunk bị cắt chữ `"l decision..."`. | **2** — Rank 1 (`p.30:c01`) chứa đầy đủ đoạn văn bối cảnh trước và trọn 2 câu evidence. | **2** — Giống B (`Rank 1 p.30:c01`), đầy đủ thông tin và truy vết nguồn chính xác. |
| `q20` (Anecdotal capability tests fail, `600-1` p.53) | **1** — Bằng chứng bị cắt đôi ngay từ `"reliabi"` cuối Rank 1 (`p.53:c02`) sang `"may not systematically..."` đầu Rank 2 (`p.53:c03`); thiếu nguyên câu điều kiện mở đầu `"Currently available pre-deployment..."`. | **2** — Rank 1 (`p.53:c02`) giữ trọn toàn bộ đoạn mở đầu lẫn cả hai ví dụ (video games/exams và jailbreaking) trong một chunk liền mạch. | **2** — Giống B (`Rank 1 p.53:c02`), đủ bằng chứng và điều kiện trước triển khai. |
| `q21` (MS-2.2-003 human subjects rights, `600-1` p.34, filter) | **1** — Hành động `MS-2.2-003` bị xẻ đôi giữa Rank 3 (cắt tại `"withdraw participation or"`) và Rank 1 (bắt đầu bằng mảnh `"ubjects with options to..."`), làm đứt liên kết giữa ID hành động và quyền rút lại đồng thuận. | **2** — Rank 2 (`p.34:c02`) chứa nguyên vẹn `MS-2.2-003` cùng nhãn rủi ro đi kèm và ngữ cảnh `MEASURE 2.2` ở Rank 1. | **2** — Lọc `year=2024, section=measure`, cả top-3 đúng phạm vi và Rank 2 (`p.34:c02`) giữ nguyên vẹn bằng chứng `MS-2.2-003`. |
| `q22` (MG-1.3-001 tolerance & staged release, `600-1` p.44, filter) | **1** — Phần ghi nhận trade-offs nằm cuối Rank 2 (đứt tại `"approaches for model"`), phần `"staged release approach"` nhảy sang Rank 1 (mở bằng `"l release:"`), buộc người đọc tự ráp 2 chunk đảo thứ tự. | **2** — Rank 2 (`p.44:c01`) giữ trọn vẹn toàn bộ `MG-1.3-001` + điều kiện bối cảnh `MANAGE 1.3`, nhưng Rank 1 bị đoạn `GOVERN 1.3` (p.18) chiếm chỗ. | **2** — Bộ lọc `document_type=generative_ai_profile, section=manage` loại đoạn `GOVERN 1.3`, đưa trọn vẹn `MG-1.3-001` (`p.44:c01`) lên **Rank 1**. |

**Tổng hợp Grounding Quality (5 câu `test`):**
- **Strategy A**: Mức `0` = 0, Mức `1` = 3, Mức `2` = 2 → **Trung bình = `1.40 / 2`** (`7/5`).
- **Strategy B**: Mức `0` = 0, Mức `1` = 0, Mức `2` = 5 → **Trung bình = `2.00 / 2`** (`10/5`).
- **Strategy C**: Mức `0` = 0, Mức `1` = 0, Mức `2` = 5 → **Trung bình = `2.00 / 2`** (`10/5`, vượt trội B ở thứ hạng Rank 1 `4/5` so với `3/5` trên `test`).
