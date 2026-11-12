---
log:
  - 2026-11-13: Ô nhập cho lệnh chờ nhập (gắn Drive, cấp quyền GCP), đường dẫn tệp đúng gốc /content, sửa tệp trong app,
    tạo/xoá thư mục, tải lên nhiều tệp, ô lệnh shell, lưu/mở code, tắt máy mồ côi, cài đặt (lối tắt, cập nhật). Có tests/test_studio.py.
---

# Colab Studio

Ứng dụng Windows có giao diện cho Colab CLI. Mọi nút chạy chính các lệnh `colab` thật (gói `colab_cli` trong repo này).

- Chạy từ mã nguồn: `.venv\Scripts\python.exe studio\colab_studio.py`
- Build `Colab Studio.exe` (một tệp, không cần cài Python): `powershell -File studio\build.ps1` → `studio\dist\`
- Dòng lệnh trong bản exe: `"Colab Studio.exe" --cli <lệnh>` giống hệt `colab <lệnh>`.
- Đăng nhập: kiểu dán mã của CLI; token lưu ở Windows Credential Manager (`google-colab-cli`), CLI và app dùng chung.
- Cửa sổ: WebView2 có sẵn trên Windows 10/11 (pywebview), mở phóng to.
- Kiểm thử: `uv run pytest tests/test_studio.py`

## Tính năng ↔ lệnh CLI

| Trong app | Lệnh / cách làm |
|---|---|
| + Máy mới (CPU, T4, L4, A100, H100, G4, TPU v5e/v6e, nhiều RAM) | `new` |
| Tắt máy · Trạng thái · Khởi động lại kernel · Mở trên web | `stop` · `status` · `restart-kernel` · `url --open` |
| Tài nguyên (RAM, ổ đĩa, GPU) | đoạn code nhỏ chạy qua `exec` |
| Chạy code (giữ biến như notebook), Mở/Lưu tệp .py, code tự lưu | `exec` (stdin) |
| Lệnh shell (`nvidia-smi`, `df -h`…) | `exec` với dòng `!lệnh` |
| Chạy tệp .py / .ipynb · Chạy một lượt | `exec -f` · `run` |
| Tệp: duyệt thư mục, tải lên nhiều tệp, tải về, xoá, **sửa**, tạo tệp, tạo/xoá thư mục | `ls` · `upload` · `download` · `rm` · download→sửa→upload (như `edit`) · `exec` (os.makedirs / shutil.rmtree) |
| Thư viện | `install` · `install -r` |
| Gắn Drive · Cấp quyền Google Cloud | `drivemount` · `auth` — lệnh chờ nhập: dùng ô **Gửi / Enter ↵** ở khung Kết quả |
| Nhật ký phiên | `log -n` · `log -o` |
| Tài khoản & gói: số dư, tốc độ tiêu, giờ còn lại, GPU/TPU được dùng, mọi máy đang tính tiền (kể cả **máy mồ côi**) + nút Tắt | `/tun/m/ccu-info`, `sessions`, `client.unassign(endpoint)` |
| Mua thêm · Cài đặt (lối tắt Desktop/Start Menu, kiểm tra cập nhật, hướng dẫn) | `pay` · `update` · `readme` |

## Lưu ý

- Contents API của Colab tính từ gốc `/` của máy ảo; app luôn dùng đường dẫn `content/...` (hàm `content_path`).
- Không có trong app: `repl` và `ssh` tương tác (cần terminal thật — dùng `"Colab Studio.exe" --cli ssh` trong cửa sổ dòng lệnh),
  và `--auth adc` (dùng dòng lệnh).
