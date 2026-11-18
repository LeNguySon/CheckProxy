# Proxy Checker SDK

Tài liệu đặc tả SDK kiểm tra proxy. SDK nhận thông tin proxy, kiểm tra khả năng
kết nối, độ trễ và tốc độ, xác định quốc gia và loại proxy, rồi trả về đánh giá
chất lượng có thể dùng trong ứng dụng.

## Mục tiêu

- Phân biệt proxy hoạt động (`alive`) và không hoạt động (`dead`), kèm lý do.
- Đo độ trễ và tốc độ tải qua proxy.
- Xác định quốc gia dựa trên địa chỉ IP công khai nhìn thấy từ phía máy chủ kiểm tra.
- Nhận diện loại proxy: HTTP, HTTPS (HTTP CONNECT), SOCKS4 hoặc SOCKS5.
- Tạo điểm chất lượng có giải thích, không chỉ trả về một nhãn chung chung.
- Hỗ trợ thời gian chờ, hủy tác vụ và giới hạn số lần kiểm tra đồng thời.

## Phạm vi và lưu ý

Chỉ kiểm tra proxy mà người dùng có quyền sử dụng. Việc kiểm tra cần tuân thủ
điều khoản của các endpoint kiểm tra, dịch vụ định vị IP và mạng đang sử dụng.
Không dùng SDK để quét dải IP, né kiểm soát truy cập hoặc gửi lưu lượng tới đích
không được phép.

Địa chỉ IP và quốc gia được báo cáo là thông tin quan sát tại thời điểm kiểm tra;
VPN, proxy nối tiếp, định tuyến mạng và dữ liệu định vị có thể làm kết quả khác
với vị trí thực tế của máy chủ proxy.

## API đề xuất

API hướng ngôn ngữ; tên kiểu dữ liệu có thể được điều chỉnh theo ngôn ngữ triển
khai. SDK Python cung cấp thêm `CheckHistory` tùy chọn để theo dõi các lần kiểm
tra kết nối gần đây trong bộ nhớ của tiến trình.

```text
checkProxy(proxy, options?) -> CheckResult
checkProxies(proxies, options?) -> CheckResult[]
```

### Đầu vào

```text
ProxyConfig {
  host: string
  port: integer
  type: "http" | "https" | "socks4" | "socks5" | "auto"
  username?: string
  password?: string
}

CheckOptions {
  timeoutMs?: integer          // mặc định: 10000
  speedTest?: boolean          // mặc định: true
  speedTestBytes?: integer     // kích thước tải tối đa; mặc định: 1 MiB
  concurrency?: integer        // áp dụng cho checkProxies
  signal?: AbortSignal         // nếu ngôn ngữ hỗ trợ
  testUrl?: string             // endpoint do người dùng cấu hình/cho phép
  geoIpProvider?: string       // dịch vụ định vị IP do ứng dụng cấu hình
}
```

Không ghi mật khẩu proxy vào log hoặc kết quả. Khi tự động nhận diện loại proxy,
chỉ thử các giao thức được bật trong cấu hình và dừng khi đạt giới hạn thời gian.

### Kết quả

```text
CheckResult {
  status: "alive" | "dead" | "unknown"
  checkedAt: string            // thời điểm ISO 8601
  proxy: {
    host: string
    port: integer
    type: "http" | "https" | "socks4" | "socks5" | "unknown"
  }
  exitIp?: string
  country?: {
    code?: string              // ISO 3166-1 alpha-2, ví dụ "VN"
    name?: string
  }
  latencyMs?: number           // thời gian round-trip tới endpoint kiểm tra
  downloadMbps?: number        // thông lượng tải đo được
  quality?: {
    score: number | null       // 0..100; null nếu thiếu phép đo bắt buộc
    grade: "excellent" | "good" | "fair" | "poor" | "unavailable"
    factors: string[]          // lý do ảnh hưởng điểm
  }
  error?: {
    code: string
    message: string
  }
}
```

`dead` biểu thị đã kiểm tra và kết nối thất bại; `unknown` biểu thị không thể kết
luận về kết nối, ví dụ tác vụ bị hủy hoặc endpoint kiểm tra không khả dụng. Lỗi
khi xác định quốc gia không làm thay đổi trạng thái kết nối.

## Quy trình kiểm tra

1. Kiểm tra đầu vào (host, port, giao thức và thông tin xác thực) trước khi tạo
   kết nối.
2. Kết nối tới endpoint kiểm tra đã cấu hình qua proxy. Kết quả thành công xác
   nhận proxy hoạt động; lỗi DNS, xác thực, timeout và lỗi giao thức được trả về
   bằng mã lỗi riêng.
3. Đo độ trễ bằng thời gian từ lúc gửi yêu cầu đến khi nhận phản hồi hợp lệ.
4. Nếu bật kiểm tra tốc độ, tải một payload giới hạn kích thước qua proxy và
   tính `downloadMbps = bytes * 8 / elapsedSeconds / 1_000_000`. Không tải nội
   dung không giới hạn.
5. Lấy IP thoát từ phản hồi endpoint kiểm tra, sau đó tra cứu quốc gia qua
   provider định vị đã cấu hình.
6. Tính điểm chất lượng từ các phép đo thành công và trả kèm các yếu tố tác
   động. Nếu proxy không hoạt động, điểm là 0 và hạng là `unavailable`.

## Đánh giá chất lượng

Điểm tham khảo (0–100), có thể tinh chỉnh theo ứng dụng:

- Độ trễ: tối đa 40 điểm. Điểm thành phần là
  `40 * clamp((2000 - latencyMs) / 1900, 0, 1)`; do đó độ trễ không quá 100 ms
  đạt 40 điểm và từ 2000 ms trở lên đạt 0.
- Tốc độ tải: tối đa 40 điểm, tính bằng `40 * min(downloadMbps / 20, 1)`.
- Độ tin cậy: tối đa 20 điểm, tính bằng
  `20 * (số lần kết nối thành công / tổng số lần kiểm tra gần nhất)`. Nếu chưa
  có lịch sử thì thành phần này chưa có dữ liệu và không được cộng điểm.

SDK Python chấp nhận `history=CheckHistory()` trên `check_proxy` và
`check_proxies`. Lịch sử chỉ lưu trong bộ nhớ, giới hạn số proxy và số mẫu gần
nhất (tối đa 10.000 proxy và 100 mẫu mỗi proxy), không lưu mật khẩu dạng rõ, và
không tồn tại sau khi tiến trình kết thúc. Khóa nhận diện dùng host, port và
fingerprint credential; chọn `auto` hay một giao thức cụ thể không tạo lịch sử
riêng cho cùng endpoint.
Chỉ kết quả kết nối xác định (`alive`/`dead`) được ghi nhận; `unknown` không làm
thay đổi độ tin cậy. Không truyền history thì hành vi chấm điểm vẫn giữ như
trước. CLI không chia sẻ hoặc lưu lịch sử giữa các lần chạy.

Tổng điểm là tổng các thành phần có dữ liệu. Nếu thiếu độ trễ hoặc tốc độ tải
khi `speedTest` được bật, `score` là `null` và `grade` là `unavailable`; các
thành phần đã đo vẫn có thể được liệt kê trong `factors`. Nếu tắt `speedTest`,
chỉ chấm điểm độ trễ và độ tin cậy, tối đa 60 điểm.
Không tự suy diễn độ tin cậy khi không có lịch sử. Khi bật kiểm tra tốc độ, cả
độ trễ và tốc độ đều phải có mới tạo điểm số dạng số; thiếu một trong hai thì
`score` là `null`.

Hạng: `excellent` từ 85, `good` từ 70, `fair` từ 50, `poor` dưới 50.
Điểm phải được đánh dấu là chưa đầy đủ khi thiếu phép đo bắt buộc; không diễn
giải tốc độ đo từ một lần kiểm tra thành cam kết hiệu năng ổn định.

## Lỗi và vận hành

Các mã lỗi nên phân biệt tối thiểu: `INVALID_PROXY`, `DNS_ERROR`,
`CONNECTION_REFUSED`, `AUTH_FAILED`, `TIMEOUT`, `PROTOCOL_ERROR`,
`TEST_ENDPOINT_ERROR`, `GEOIP_ERROR`, `SPEED_TEST_ERROR`, `ABORTED`.

- Luôn áp dụng timeout cho kết nối và từng bước đo.
- Giới hạn đồng thời và kích thước tải; không retry vô hạn.
- Không ghi thông tin xác thực vào log; che IP nếu chính sách ứng dụng yêu cầu.
- Trả kết quả từng proxy trong `checkProxies`, để lỗi một proxy không làm mất
  kết quả của các proxy còn lại.
- Cho phép thay endpoint kiểm tra để người dùng sử dụng dịch vụ đáng tin cậy
  hoặc hạ tầng của chính họ.
- Trong API Python, hủy tác vụ truyền `asyncio.CancelledError` lên caller,
  không chuyển cancellation thành một kết quả `ABORTED`.

## Tiêu chí nghiệm thu

- Trả đúng trạng thái cho proxy hoạt động, proxy từ chối kết nối, sai xác thực
  và timeout.
- Nhận diện đúng giao thức trong phạm vi giao thức được bật; `auto` không đoán
  loại proxy nếu không có phép bắt tay thành công.
- Chỉ trả độ trễ/tốc độ khi phép đo hợp lệ; tốc độ không âm và bị giới hạn bởi
  payload cấu hình.
- Quốc gia dùng mã ISO hai ký tự khi provider cung cấp; lỗi GeoIP không làm
  hỏng kết quả kiểm tra kết nối.
- Điểm nằm trong khoảng 0–100, có hạng tương ứng và giải thích được các yếu tố.
- Tác vụ hủy và danh sách proxy lớn đều tôn trọng giới hạn tài nguyên.

## Triển khai Python

Mã nguồn SDK và CLI nằm tại `proxy_checker/`; hướng dẫn cài đặt, API, endpoint
mặc định và giới hạn quyền riêng tư ở [README.md](../README.md). Các endpoint
mặc định là `https://example.com/` để kiểm tra kết nối,
`https://ipwho.is/` để lấy IP/quốc gia và
`https://speed.cloudflare.com/__down` để đo tốc độ. Các yêu cầu này được gửi
qua proxy; ứng dụng có thể thay endpoint hoặc tắt GeoIP/tốc độ. Redirect không
được theo và nội dung phản hồi luôn bị giới hạn.
