# Custom Video Tool

Công cụ desktop (Python + PySide6) chỉnh sửa **hàng loạt** video trong một thư
mục: áp một bố cục dọc **9:16** (hoặc **16:9**) gồm tiêu đề trên cùng, khung
video ở giữa, mô tả ở dưới, với **nền làm mờ** từ chính video — rồi xuất ra
thư mục đích. Bố cục được căn chỉnh trực quan bằng **kéo-thả & resize** trên
khung preview, và preview khớp với video xuất ra.

## Tính năng
- Chọn **thư mục input** chứa video; **output** mặc định = input.
- Nút **Bắt đầu / Dừng**, xử lý toàn bộ video trong thư mục.
- Chọn định dạng **9:16 ↔ 16:9**.
- Preview 3 vùng **kéo-thả + đổi kích cỡ**: Title / Video / Description.
- **Nền làm mờ** video (hoặc màu đặc).
- **Tự động mô tả** bằng Whisper (speech-to-text) → tự điền title/description;
  hoặc lấy theo **tên file**, hoặc **nhập tay**.
- **Styling chữ**: font, cỡ, màu chữ, màu nền, canh lề (riêng cho title & desc).
- **Audio**: tốc độ phát (đồng bộ cả video) + âm lượng.
- **Lưu / tải cấu hình** (JSON); tự lưu phiên gần nhất.

> Ứng dụng có **2 tab**: **Ghép Video** (mô tả ở trên) và **YouTube Finder** (bên dưới).

## Tab "YouTube Finder" — tìm kênh hot/trending
Nhập từ khóa (để trống = **TOP TRENDING** theo quốc gia), chọn khu vực, ngày
đăng, các ngưỡng lọc (sub / lượt xem / tuổi kênh / tổng video) → **Bắt đầu tìm**
→ app gọi **YouTube Data API v3** và liệt kê kênh dạng bảng (sub, tổng view, ngày
tạo, lượt xem/ngày, lượt xem/ngày cao nhất của video gần đây, …). Chuột phải vào
dòng để **Mở kênh** hoặc copy các trường; **Import/Export CSV**; lọc nhanh & sort
theo cột.

### Cần API key (YouTube Data API v3)
1. Vào Google Cloud Console → tạo project → bật **YouTube Data API v3** → tạo
   **API key**.
2. Dán key vào ô **API key** trên tab (lưu vào `config.json`), **hoặc** đặt biến
   môi trường `YOUTUBE_API_KEY` (ô để trống sẽ tự dùng biến này).
3. Quota mặc định 10.000 unit/ngày: chế độ trending gần như miễn phí; tìm theo từ
   khóa tốn ~100 unit/trang (mỗi 50 kết quả).

### Tải video của kênh (yt-dlp)
- Di chuột vào dòng trong bảng → **highlight cả hàng**.
- Đặt **Tải về** (thư mục lưu) + **Số video / kênh** (hoặc tick **Tải tất cả**).
- **Chuột phải** vào kênh (chọn được nhiều dòng) → **⬇ Tải video của kênh này** →
  tải N video mới nhất (hoặc tất cả) vào `<thư mục>/<tên kênh>/`. Bấm **Dừng** để
  hủy.
- Hoặc dán trực tiếp vào ô **URL kênh** rồi bấm **⬇ Tải video** — cũng tải theo
  cùng cấu hình (số lượng / Tải tất cả). Hỗ trợ URL kênh, playlist, hoặc 1 video.
- Cần `yt-dlp` (`python -m pip install yt-dlp`). Tải dạng file đơn `best[ext=mp4]`
  (không cần ghép bằng ffmpeg).

## Cài đặt
```bash
python -m pip install -r requirements.txt
```
- FFmpeg **không cần cài tay** — gói `imageio-ffmpeg` tự tải binary ở lần dùng
  đầu tiên (cần internet một lần).
- `faster-whisper` là **tùy chọn**; thiếu nó app vẫn chạy, chỉ tắt phần tự động
  nhận dạng giọng nói. (Đã kiểm tra cài & chạy được trên Python 3.14.)

## Chạy
```bash
python main.py
```
1. Bấm **Input** → chọn thư mục video. Output tự điền theo input.
2. Chọn **9:16** hoặc **16:9**.
3. Kéo/resize các khung trên preview, hoặc dùng slider X/Y/W/H.
4. Chỉnh chữ, nền, audio, nguồn title/description ở panel bên phải.
5. Bấm **Bắt đầu**. Video xuất ra `output/<tên>_out.mp4`.

## Cấu trúc
```
main.py                     # entry point
app/core/
  layout_model.py           # Layout/Region (toạ độ chuẩn hoá) + serialize
  config.py                 # lưu/tải JSON
  video_probe.py            # OpenCV: metadata + frame preview
  text_render.py            # Pillow: text -> PNG trong suốt
  ffmpeg_runner.py          # định vị ffmpeg, dựng filter_complex, chạy + progress
  transcribe.py             # faster-whisper (lazy/optional)
  batch_processor.py        # QThread: duyệt thư mục, render, progress, stop
app/ui/
  main_window.py            # cửa sổ chính
  preview_canvas.py         # QGraphicsView + nền blur
  resizable_item.py         # item kéo-thả + 8 handle resize
  widgets.py                # slider+spin, folder picker, color button
```

## Ghi chú kỹ thuật
- Toạ độ vùng lưu **chuẩn hoá [0..1]** theo canvas nên preview khớp output ở mọi
  tỉ lệ/độ phân giải. Canvas: 9:16 → 720×1280, 16:9 → 1280×720.
- Chữ được render ra **PNG (Pillow)** rồi overlay (không dùng `drawtext`) để hỗ
  trợ tiếng Việt có dấu + tự xuống dòng. Font mặc định lấy từ Windows (Arial).
- "Tốc độ" áp cho **cả video và audio** (setpts + atempo) để giữ đồng bộ.
