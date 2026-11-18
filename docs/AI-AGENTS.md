# Thiết lập AI agent

Dự án tích hợp tài nguyên ECC (Everything Claude Code) và hướng dẫn riêng cho
các coding agent.

## Vị trí hướng dẫn

- [../AGENTS.md](../AGENTS.md) là quy tắc dùng chung cho agent của dự án.
- [../.github/copilot-instructions.md](../.github/copilot-instructions.md)
  dẫn GitHub Copilot tới quy tắc chung và đặc tả SDK.
- [../.github/agents/](../.github/agents/) có hai agent VS Code riêng của dự
  án: **Proxy SDK Engineer** và **Proxy SDK Reviewer**.
- [../.github/skills/](../.github/skills/) và
  [../.github/instructions/ecc/](../.github/instructions/ecc/) chứa các skill
  và hướng dẫn bổ sung được cài từ ECC.

Ưu tiên yêu cầu riêng của dự án. Khuyến nghị chung từ ECC không phải quy định
của dự án nếu xung đột với đặc tả hiện tại hoặc công cụ sẵn có trong repository.

## Nguồn và phạm vi ECC

- Mã nguồn chính thức: [affaan-m/ECC](https://github.com/affaan-m/ECC)
- Thông báo giấy phép MIT: [../LICENSE-ECC](../LICENSE-ECC)
- Commit đã cài: `3f14a103a8694e51c1df9a806217a6d0b5f7a6f7`
- Target/profile: `copilot` / `full`
- Nội dung đã cài: 72 agent, 315 skill và 121 file hướng dẫn ECC. Installer
  theo từng target chỉ chọn một phần danh mục upstream.
- Target Copilot được ECC mô tả dành cho GitHub Copilot CLI và không cấu hình
  ECC hooks. Các agent `.agent.md` riêng của dự án cung cấp vai trò cho VS Code;
  khả năng hỗ trợ agent client khác tùy thuộc quy tắc discovery của từng client.
- Bản npm `ecc-universal@2.2.3` không nhận target `copilot`. Vì vậy, quá trình
  cài dùng commit upstream đã review ở trên, không dùng bản npm đó.

Installer ECC ghi danh sách file được quản lý trong
[`../.github/ecc-install-state.json`](../.github/ecc-install-state.json). Giữ
file ECC tách biệt với hướng dẫn do dự án sở hữu; review kế hoạch trước khi cập
nhật hoặc xóa tài nguyên ECC.

## Cập nhật ECC

Review mã nguồn upstream và ghim commit trước khi cập nhật. Từ thư mục gốc dự
án, chạy installer trong checkout ECC ở chế độ xem trước:

```powershell
node "C:\path\to\ECC\scripts\install-apply.js" --profile full --target copilot --dry-run
```

Kiểm tra đường dẫn dự kiến để phát hiện xung đột hoặc xóa ngoài ý muốn. Chỉ sau
khi review kế hoạch mới chạy lại lệnh và bỏ `--dry-run`. Sau đó kiểm tra hướng
dẫn riêng của dự án và chạy các bước xác thực hiện có.
