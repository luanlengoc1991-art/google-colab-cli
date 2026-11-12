# Colab Studio

Ứng dụng Windows có giao diện cho Colab CLI: đăng nhập Google, tạo / tắt máy CPU · GPU · TPU của Colab,
chạy code hoặc tệp `.py` / `.ipynb`, chạy một lượt (tự tạo và tự tắt máy), quản lý tệp, cài thư viện, gắn Drive,
xuất nhật ký, xem đơn vị tính toán. Mọi nút chạy chính các lệnh `colab` thật (gói `colab_cli` trong repo này).

- Chạy từ mã nguồn: `.venv\Scripts\python.exe studio\colab_studio.py`
- Build `Colab Studio.exe` (một tệp, không cần cài Python): `powershell -File studio\build.ps1` → `studio\dist\`
- Dòng lệnh trong bản exe: `"Colab Studio.exe" --cli <lệnh>` giống hệt `colab <lệnh>`.
- Đăng nhập: kiểu dán mã của CLI; token lưu ở Windows Credential Manager (`google-colab-cli`), CLI và app dùng chung.
- Cửa sổ: WebView2 có sẵn trên Windows 10/11 (pywebview), mở phóng to.
