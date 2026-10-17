"""
oci_auto_retry.py
=====================================
Script TỰ ĐỘNG DÒ VÀ TẠO VM Free Tier ARM (VM.Standard.A1.Flex) trên Oracle Cloud.

VÌ SAO CẦN SCRIPT NÀY: shape VM.Standard.A1.Flex (ARM Ampere, 4 OCPU/24GB RAM miễn phí) rất hay
báo lỗi "Out of host capacity" vì có quá nhiều người cùng xin — Oracle chỉ cấp được khi có người
khác trả lại tài nguyên. Cách duy nhất để "bắt" được là liên tục thử lại (retry) — tay không làm
xuể vì phải bấm liên tục 24/7. Script này tự làm việc đó qua OCI Python SDK, gọi thẳng API
LaunchInstance (KHÔNG qua Terraform Apply — nhanh hơn nhiều lần, mỗi lần thử chỉ mất 1-2 giây
thay vì cả phút như chạy lại Resource Manager stack).

CÁCH DÙNG: xem hướng dẫn lấy đủ OCID/secrets ở file README đi kèm. Chạy: python oci_auto_retry.py
--------------------------------------------------------------------------------
"""
import os
import sys
import time
import logging
import tempfile
import requests

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

try:
    import oci
except ImportError:
    print("Thiếu thư viện oci. Chạy: pip install oci")
    sys.exit(1)


# ========================= CẤU HÌNH (đọc từ biến môi trường / GitHub Secrets) =========================
def _require(name: str) -> str:
    val = os.getenv(name, "").strip()
    if not val:
        log.error("Thiếu biến môi trường bắt buộc: %s", name)
        sys.exit(1)
    return val


OCI_USER_OCID = _require("OCI_USER_OCID")
OCI_FINGERPRINT = _require("OCI_FINGERPRINT")
OCI_TENANCY_OCID = _require("OCI_TENANCY_OCID")
OCI_REGION = _require("OCI_REGION")
OCI_PRIVATE_KEY = _require("OCI_PRIVATE_KEY")  # Nội dung file .pem, dán nguyên văn vào secret
OCI_COMPARTMENT_OCID = _require("OCI_COMPARTMENT_OCID")
OCI_SUBNET_OCID = _require("OCI_SUBNET_OCID")
OCI_IMAGE_OCID = _require("OCI_IMAGE_OCID")
OCI_SSH_PUBLIC_KEY = _require("OCI_SSH_PUBLIC_KEY")

INSTANCE_DISPLAY_NAME = os.getenv("OCI_INSTANCE_NAME", "falconthp-vm-bot").strip()
# SỬA 02/10/2026: Oracle đã giảm hạn mức Always Free cho Ampere A1 từ 4 OCPU/24GB xuống còn
# 2 OCPU/12GB (áp dụng từ 15/06/2026, cho MỌI tài khoản).
# SỬA 06/10/2026: sau ~270 lần thử (nhiều ngày) vẫn "Out of host capacity" liên tục ở Singapore
# (chỉ có 1 AD, vốn nổi tiếng khó xin ARM A1) — hạ tiếp xuống 1 OCPU/6GB. Khẩu phần xin càng nhỏ,
# xác suất Oracle "ghép" được vào 1 khe trống càng cao (so với xin nguyên 2 OCPU/12GB liền khối).
# Có thể nâng lại lên 2/12 sau khi đã tạo được VM lần đầu (không bị tính là "yêu cầu mới" nữa).
OCPUS = float(os.getenv("OCI_OCPUS", "1"))
MEMORY_GB = float(os.getenv("OCI_MEMORY_GB", "6"))
BOOT_VOLUME_GB = float(os.getenv("OCI_BOOT_VOLUME_GB", "50"))

# Job GitHub Actions nên đặt timeout-minutes lớn hơn số này 1-2 phút để không bị Actions tự ngắt
# giữa chừng (mất log). SỬA 01/10/2026: tăng từ 280s (~4.7 phút) lên 600s (10 phút) — gộp với
# RETRY_INTERVAL_SEC tăng lên 60s bên dưới, vẫn giữ được ~10 lần thử/lượt chạy nhưng dãn tần suất
# gọi API ra, AN TOÀN HƠN cho tài khoản (tránh bị Oracle coi là gọi API dồn dập/bất thường).
MAX_DURATION_SEC = int(os.getenv("MAX_DURATION_SEC", "600"))
# SỬA 01/10/2026 (lần 2): 60s vẫn dính 429 khá thường xuyên trên thực tế (log thật cho thấy cứ
# cách 1 lần lại bị 429) — tăng tiếp lên 120s/lần cho an toàn hơn, đổi lại mỗi lượt chạy (600s) còn
# ~5 lần thử thay vì ~10, nhưng ít bị Oracle giới hạn tốc độ/gắn cờ hơn.
RETRY_INTERVAL_SEC = int(os.getenv("RETRY_INTERVAL_SEC", "120"))

# SỬA 10/10/2026: sau 444 lần thử (nhiều ngày) vẫn "Out of host capacity" ở Singapore cho A1.Flex
# (ARM) dù đã hạ xuống 1 OCPU/6GB — thêm PHƯƠNG ÁN DỰ PHÒNG: shape E2.1.Micro (x86, CŨNG thuộc
# Always Free, 1/8 OCPU + 1GB RAM) rất ít bị tranh giành nên gần như luôn tạo được ngay. Script sẽ
# thử tạo máy E2 này (tên riêng, KHÔNG đụng tới máy ARM) 1 LẦN DUY NHẤT mỗi lượt chạy — chỉ để có
# ngay 1 máy dùng tạm trong lúc vẫn tiếp tục dò ARM song song. Đặt ENABLE_E2_FALLBACK=false trong
# workflow nếu không muốn máy x86 dự phòng này (ví dụ đã đủ 2 máy E2 Micro miễn phí rồi).
ENABLE_E2_FALLBACK = os.getenv("ENABLE_E2_FALLBACK", "true").strip().lower() == "true"
# SỬA 11/10/2026: log thực tế báo lỗi "Shape VM.Standard.E2.1.Micro is not valid for image ***" —
# OCI_IMAGE_OCID đang dùng là image kiến trúc ARM (aarch64, để tạo máy A1.Flex), KHÔNG tương thích
# với shape x86 (E2.1.Micro) — đây là lỗi cấu hình thật (400), không phải hết chỗ, nên cứ lặp lại
# mãi mãi mỗi lượt chạy nếu không sửa. BẮT BUỘC cần 1 image x86_64 RIÊNG cho máy E2 qua secret
# OCI_E2_IMAGE_OCID (xem hướng dẫn lấy OCID trong README/tin nhắn kèm theo). Nếu chưa cấu hình,
# tự động TẮT fallback E2 (thay vì cứ lỗi 400 vô ích mỗi lượt chạy) và chỉ cảnh báo 1 lần.
OCI_E2_IMAGE_OCID = os.getenv("OCI_E2_IMAGE_OCID", "").strip()
if ENABLE_E2_FALLBACK and not OCI_E2_IMAGE_OCID:
    log.warning("ENABLE_E2_FALLBACK=true nhưng CHƯA có OCI_E2_IMAGE_OCID (image x86_64 riêng cho "
                "E2.1.Micro) — image ARM hiện tại KHÔNG dùng được cho shape x86. Tạm tắt fallback "
                "E2 lượt chạy này; thêm secret OCI_E2_IMAGE_OCID để bật lại.")
    ENABLE_E2_FALLBACK = False
E2_SHAPE = "VM.Standard.E2.1.Micro"
# SỬA 10/10/2026 (lần 2): Oracle Always Free cho phép TỐI ĐA 2 máy E2.1.Micro cùng lúc (tách biệt
# hoàn toàn với suất ARM) — tạo danh sách 2 tên thay vì 1, script sẽ thử tạo ĐỦ CẢ 2 (bỏ qua máy
# nào đã có sẵn). Đặt OCI_E2_INSTANCE_NAMES="ten1,ten2" nếu muốn đổi tên; để trống phần tử nào thì
# dùng tên mặc định tương ứng.
_e2_names_raw = os.getenv("OCI_E2_INSTANCE_NAMES", "").strip()
if _e2_names_raw:
    E2_INSTANCE_NAMES = [n.strip() for n in _e2_names_raw.split(",") if n.strip()]
else:
    # Giữ nguyên tên máy đầu tiên là "-e2micro" (không phải "-e2micro-1") để KHỚP với tên đã dùng ở
    # bản trước — tránh tạo trùng/mồ côi nếu máy đó lỡ đã được tạo ở lượt chạy trước khi có bản này.
    E2_INSTANCE_NAMES = [f"{INSTANCE_DISPLAY_NAME}-e2micro", f"{INSTANCE_DISPLAY_NAME}-e2micro-2"]
E2_BOOT_VOLUME_GB = float(os.getenv("OCI_E2_BOOT_VOLUME_GB", "50"))

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()


def send_telegram(message: str):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        log.info("Chưa cấu hình Telegram — bỏ qua gửi thông báo. Nội dung: %s", message)
        return
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
        resp = requests.post(url, data={"chat_id": TELEGRAM_CHAT_ID, "text": message}, timeout=15)
        if resp.status_code != 200:
            log.error("Gửi Telegram thất bại (%s): %s", resp.status_code, resp.text[:200])
    except Exception as e:
        log.error("Lỗi khi gửi Telegram: %s", e)


def _normalize_pem(raw: str) -> str:
    """SỬA 01/10/2026: gặp lỗi 'InvalidPrivateKey ... MalformedFraming' trên GitHub Actions — do nội
    dung dán vào secret OCI_PRIVATE_KEY bị MẤT XUỐNG DÒNG thật (ví dụ dán qua ô chỉ nhận 1 dòng, hoặc
    bị thay bằng ký tự '\\n' chữ thay vì xuống dòng thật '\n') — PEM BẮT BUỘC phải có xuống dòng thật
    giữa các dòng base64 thì thư viện cryptography mới đọc được. Hàm này tự dò và sửa 2 kiểu lỗi phổ
    biến nhất:
      1. Toàn bộ nội dung dính thành 1 dòng, có chứa ký tự '\\n' (backslash + n) thay vì xuống dòng
         thật — thay literal '\\n' bằng xuống dòng thật.
      2. Xuống dòng kiểu Windows (\r\n) — chuẩn hoá về \n.
    KHÔNG tự "sửa" được nếu bạn dán thiếu hẳn 1 phần nội dung key — trường hợp đó phải tải lại/dán
    lại key cho đủ, script chỉ báo rõ lỗi để bạn biết hướng xử lý."""
    text = raw.strip()
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    if "\n" not in text and "\\n" in text:
        text = text.replace("\\n", "\n")
    return text.strip() + "\n"


def get_oci_config() -> dict:
    """SỬA: GitHub Actions không có sẵn file ~/.oci/config như máy cá nhân — OCI SDK bắt buộc cần
    1 FILE private key thật trên đĩa (không nhận nội dung PEM trực tiếp qua config dict ở 1 số
    phiên bản SDK cũ), nên ghi tạm nội dung OCI_PRIVATE_KEY ra 1 file trong thư mục tạm rồi trỏ
    key_file vào đó."""
    key_content = _normalize_pem(OCI_PRIVATE_KEY)
    lines = key_content.strip().split("\n")
    first_line, last_line = lines[0] if lines else "", lines[-1] if lines else ""
    log.info("Kiểm tra định dạng OCI_PRIVATE_KEY — dòng đầu: '%s' | dòng cuối: '%s' | số dòng: %d",
              first_line, last_line, len(lines))
    if not first_line.startswith("-----BEGIN") or not last_line.startswith("-----END"):
        log.error("OCI_PRIVATE_KEY KHÔNG đúng định dạng PEM (thiếu dòng -----BEGIN...-----/-----END...-----). "
                   "Kiểm tra lại: đã copy ĐỦ TOÀN BỘ nội dung file .pem (kể cả 2 dòng BEGIN/END) chưa, "
                   "dán bằng Notepad/VSCode (KHÔNG dùng Word), không có dấu ngoặc kép bọc ngoài.")
        send_telegram("❌ Script tạo VM Oracle dừng vì OCI_PRIVATE_KEY sai định dạng PEM — kiểm tra lại secret, dán lại đủ nội dung file .pem.")
        sys.exit(1)
    if len(lines) < 3:
        log.error("OCI_PRIVATE_KEY chỉ có %d dòng — PEM hợp lệ thường có nhiều dòng base64 ở giữa "
                   "BEGIN/END. Khả năng cao nội dung bị dồn hết thành 1-2 dòng khi dán vào secret — "
                   "dán lại trực tiếp từ file .pem gốc, KHÔNG qua ô chỉ nhận 1 dòng văn bản.", len(lines))
    key_path = os.path.join(tempfile.gettempdir(), "oci_api_key.pem")
    with open(key_path, "w") as f:
        f.write(key_content)
    os.chmod(key_path, 0o600)
    return {
        "user": OCI_USER_OCID,
        "fingerprint": OCI_FINGERPRINT,
        "tenancy": OCI_TENANCY_OCID,
        "region": OCI_REGION,
        "key_file": key_path,
    }



# ======================= SỬA 14/10/2026: VIẾT LẠI PHẦN DÒ/TẠO VM CHO DỄ "BẮT CHỖ" HƠN =======================
# Dùng API CreateComputeCapacityReport (hỏi "còn chỗ không", không tạo gì) để dò thường xuyên, thấy
# Fault Domain nào báo AVAILABLE thì bắn LaunchInstance NGAY vào đúng FD đó; xoay vòng cả 3 FD.
#
# SỬA 17/10/2026 — log thật 17/10 cho thấy 3 vấn đề, bản này sửa:
#   1. "Bắn thử" LaunchInstance cho 3 máy sát nhau (cách 1-2s) -> gần như lần nào cũng dính 429; mỗi lần
#      429 lại chờ 30s + 60s -> cả lượt 600s chỉ dò được 3 vòng (thay vì ~20 vòng như thiết kế).
#      Nay: (a) giữa 2 lần gọi LaunchInstance bất kỳ luôn cách nhau tối thiểu LAUNCH_GAP_SEC (mặc định
#      40s), chưa đủ thì để vòng sau, KHÔNG ngồi chờ; (b) "bắn thử" khi báo cáo đang nói hết chỗ chỉ là
#      phòng hờ nên gặp 429 thì bỏ qua luôn (không chờ), chỉ dời lịch bắn thử sau; (c) chỉ khi báo cáo nói
#      CÓ CHỖ thì mới chờ ngắn (10s, 20s) và thử lại — vì lúc đó mới đáng giành giật.
#   2. Lượt chạy vượt quá giờ (vòng cuối bắt đầu ở giây 562 nhưng còn ngồi chờ 429 thêm vài phút) -> bị
#      GitHub ngắt ("The operation was canceled", job báo đỏ). Nay mọi lần chờ đều bị chặn bởi hạn chót
#      MAX_DURATION_SEC — hết giờ là dừng gọn, in tổng kết, job báo xanh.
#   3. Báo cáo chỗ trống luôn khớp với kết quả LaunchInstance thật (đều OUT_OF_HOST_CAPACITY) -> khi báo
#      cáo dùng được, giãn nhịp bắn thử ra BLIND_LAUNCH_INTERVAL_SEC (mặc định 300s/máy) để dành "hạn mức
#      gọi API" cho lúc báo cáo báo có chỗ thật.
CHECK_INTERVAL_SEC = int(os.getenv("CHECK_INTERVAL_SEC", "30"))
LAUNCH_GAP_SEC = int(os.getenv("LAUNCH_GAP_SEC", "40"))
BLIND_LAUNCH_INTERVAL_SEC = int(os.getenv("BLIND_LAUNCH_INTERVAL_SEC", "300"))
ARM_SHAPE = "VM.Standard.A1.Flex"

_deadline = None          # thời điểm (time.time()) phải dừng lượt chạy
_last_any_launch = 0.0    # lần gọi LaunchInstance gần nhất (của BẤT KỲ máy nào)


def time_left() -> float:
    return (_deadline - time.time()) if _deadline else 1e9


def sleep_capped(sec: float) -> bool:
    """Ngủ nhưng không vượt hạn chót. Trả về False nếu đã hết giờ (không nên làm gì thêm)."""
    left = time_left() - 3
    if left <= 0:
        return False
    time.sleep(max(0, min(sec, left)))
    return time_left() > 3


class Target:
    """1 máy cần tạo (máy ARM chính hoặc 1 trong 2 máy E2 dự phòng)."""

    def __init__(self, name, shape, image_id, boot_gb, ocpus=None, memory_gb=None, label=""):
        self.name = name
        self.shape = shape
        self.image_id = image_id
        self.boot_gb = boot_gb
        self.ocpus = ocpus
        self.memory_gb = memory_gb
        self.label = label or name
        self.done = False          # đã tạo xong / đã có sẵn
        self.skipped = False       # lỗi cấu hình (image sai kiến trúc, vượt hạn mức...) — bỏ qua lượt này
        self.next_blind = 0.0      # thời điểm được "bắn thử" tiếp theo
        self.fd_index = 0          # xoay vòng Fault Domain khi bắn thử
        self.launch_tries = 0
        self.reports = 0
        self.seen_available = 0

    @property
    def active(self):
        return not self.done and not self.skipped

    @property
    def is_flex(self):
        return self.shape.endswith(".Flex")


def instance_already_exists(compute_client, display_name: str):
    """Trả về ID nếu đã có instance tên display_name (chưa bị terminate) — tránh tạo trùng, tốn hạn mức
    Free Tier. Đây cũng là cách tool TỰ DỪNG: tạo xong rồi thì các lượt cron sau thoát sau vài giây."""
    states_to_ignore = {"TERMINATED", "TERMINATING"}
    try:
        resp = compute_client.list_instances(compartment_id=OCI_COMPARTMENT_OCID, display_name=display_name)
        for inst in resp.data:
            if inst.lifecycle_state not in states_to_ignore:
                return inst.id
    except oci.exceptions.ServiceError as e:
        log.warning("Không kiểm tra được '%s' đã tồn tại chưa (%s) — vẫn tiếp tục thử tạo.", display_name, e.message)
    return None


def list_availability_domains(identity_client) -> list:
    resp = identity_client.list_availability_domains(compartment_id=OCI_TENANCY_OCID)
    ads = [ad.name for ad in resp.data]
    log.info("Các Availability Domain khả dụng ở region %s: %s", OCI_REGION, ads)
    return ads


def list_fault_domains(identity_client, ad: str) -> list:
    """Danh sách Fault Domain trong 1 AD (thường là FAULT-DOMAIN-1/2/3). Lỗi thì trả [None] = để Oracle tự chọn."""
    try:
        resp = identity_client.list_fault_domains(compartment_id=OCI_TENANCY_OCID, availability_domain=ad)
        fds = [fd.name for fd in resp.data]
        if fds:
            return fds
    except oci.exceptions.ServiceError as e:
        log.warning("Không lấy được danh sách Fault Domain của %s (%s) — để Oracle tự chọn FD.", ad, e.message)
    return [None]


def capacity_report(compute_client, target: Target, ad: str, fds: list):
    """Hỏi Oracle: shape này ở từng Fault Domain còn chỗ không. Trả về list FD đang AVAILABLE ([] nếu
    hết chỗ hoặc lỗi tạm thời), hoặc None nếu API này không dùng được (thiếu quyền...) -> bắn thử định kỳ."""
    shape_cfg = None
    if target.is_flex:
        shape_cfg = oci.core.models.CapacityReportInstanceShapeConfig(
            ocpus=target.ocpus, memory_in_gbs=target.memory_gb)
    avail_items = [
        oci.core.models.CreateCapacityReportShapeAvailabilityDetails(
            instance_shape=target.shape, fault_domain=fd, instance_shape_config=shape_cfg)
        for fd in fds
    ]
    details = oci.core.models.CreateComputeCapacityReportDetails(
        compartment_id=OCI_TENANCY_OCID, availability_domain=ad, shape_availabilities=avail_items)
    target.reports += 1
    try:
        resp = compute_client.create_compute_capacity_report(details)
    except oci.exceptions.ServiceError as e:
        if e.status == 429:
            log.warning("  Dò chỗ trống [%s]: bị 429 — bỏ qua vòng này.", target.label)
            return []
        log.warning("  Dò chỗ trống [%s] lỗi (%s %s): %s — chuyển sang chỉ bắn thử định kỳ.",
                    target.label, e.status, e.code, e.message)
        return None
    except Exception as e:  # lỗi mạng tạm thời
        log.warning("  Dò chỗ trống [%s] lỗi mạng: %s", target.label, e)
        return []
    available, summary = [], []
    for item in resp.data.shape_availabilities or []:
        status = item.availability_status
        fd = item.fault_domain or "?"
        summary.append(f"{fd.replace('FAULT-DOMAIN-', 'FD')}={status}")
        if status == "AVAILABLE":
            available.append(item.fault_domain)
    line = ", ".join(summary) or "(không có dữ liệu)"
    # Chỉ in chi tiết khi có chỗ, hoặc 1 lần mỗi ~10 vòng — tránh log quá dài khi hết chỗ liên tục.
    if available or target.reports % 10 == 1:
        log.info("  Dò chỗ trống [%s] (lần %d): %s", target.label, target.reports, line)
    return available


def try_launch(compute_client, target: Target, ad: str, fd, urgent: bool):
    """Gọi LaunchInstance cho target ở AD/FD chỉ định. Trả về instance nếu thành công, None nếu chưa được.
    urgent=True (báo cáo nói CÓ CHỖ): gặp 429 thì chờ ngắn 10s, 20s rồi thử lại.
    urgent=False (bắn thử phòng hờ): gặp 429 thì bỏ qua luôn, dời lịch bắn thử sau."""
    global _last_any_launch
    kwargs = dict(
        availability_domain=ad,
        compartment_id=OCI_COMPARTMENT_OCID,
        shape=target.shape,
        display_name=target.name,
        create_vnic_details=oci.core.models.CreateVnicDetails(subnet_id=OCI_SUBNET_OCID, assign_public_ip=True),
        source_details=oci.core.models.InstanceSourceViaImageDetails(
            image_id=target.image_id, boot_volume_size_in_gbs=target.boot_gb),
        metadata={"ssh_authorized_keys": OCI_SSH_PUBLIC_KEY},
    )
    if fd:
        kwargs["fault_domain"] = fd
    if target.is_flex:
        kwargs["shape_config"] = oci.core.models.LaunchInstanceShapeConfigDetails(
            ocpus=target.ocpus, memory_in_gbs=target.memory_gb)
    details = oci.core.models.LaunchInstanceDetails(**kwargs)
    where = f"[{target.label}] {fd or ad}" + ("" if urgent else " (bắn thử)")

    waits = [10, 20] if urgent else []
    for attempt in range(len(waits) + 1):
        _last_any_launch = time.time()
        target.launch_tries += 1
        try:
            resp = compute_client.launch_instance(details)
            return resp.data
        except oci.exceptions.ServiceError as e:
            msg = e.message or ""
            code = e.code or ""
            if "Out of host capacity" in msg or "OutOfCapacity" in code:
                log.info("  %s: hết chỗ (Out of host capacity).", where)
                return None
            if e.status == 429:
                if attempt < len(waits):
                    log.warning("  %s: bị 429 — chờ %ds rồi thử lại.", where, waits[attempt])
                    if not sleep_capped(waits[attempt]):
                        return None
                    continue
                log.info("  %s: bị 429 — bỏ qua, dời lần bắn thử sau.", where)
                return None
            if e.status in (401, 404):
                log.error("  %s: lỗi cấu hình (%s): %s — KIỂM TRA LẠI OCID/QUYỀN.", where, e.status, msg)
                send_telegram(f"❌ Script tạo VM Oracle dừng vì lỗi cấu hình ({e.status}): {msg}\n"
                              f"Kiểm tra lại OCID/quyền, không phải do hết chỗ.")
                sys.exit(1)
            if "LimitExceeded" in code or "QuotaExceeded" in code or "service limit" in msg.lower():
                log.error("  %s: VƯỢT HẠN MỨC (%s): %s — bỏ qua máy này lượt chạy này.", where, code, msg)
                target.skipped = True
                return None
            if e.status == 400:
                log.error("  %s: lỗi cấu hình 400 (%s): %s — bỏ qua máy này lượt chạy này.", where, code, msg)
                send_telegram(f"⚠️ Máy '{target.name}' lỗi cấu hình 400 ({code}): {msg}")
                target.skipped = True
                return None
            log.warning("  %s: lỗi khác (%s %s): %s", where, e.status, code, msg)
            return None
        except Exception as e:
            log.warning("  %s: lỗi mạng/tạm thời: %s", where, e)
            return None
    return None


def announce_success(target: Target, inst, ad: str, fd):
    if target.shape == ARM_SHAPE:
        msg = (f"🎉 ĐÃ TẠO THÀNH CÔNG VM ARM '{target.name}' ({target.ocpus:g} OCPU / {target.memory_gb:g}GB RAM)!\n"
               f"Instance ID: {inst.id}\nAD: {ad}\nFault Domain: {fd or 'Oracle tự chọn'}\n"
               f"Vào Console > Compute > Instances để xem Public IP (mất khoảng 1 phút để lên Running).")
    else:
        msg = (f"✅ Đã tạo máy DỰ PHÒNG '{target.name}' (x86 E2.1.Micro, 1/8 OCPU + 1GB RAM)!\n"
               f"Instance ID: {inst.id}\nAD: {ad}\nFault Domain: {fd or 'Oracle tự chọn'}\n"
               f"Vào Console > Compute > Instances để xem Public IP.")
    log.info(msg)
    send_telegram(msg)


def main():
    global _deadline
    _deadline = time.time() + MAX_DURATION_SEC

    cfg = get_oci_config()
    identity_client = oci.identity.IdentityClient(cfg)
    compute_client = oci.core.ComputeClient(cfg)

    ads = list_availability_domains(identity_client)
    if not ads:
        log.error("Không lấy được danh sách Availability Domain — kiểm tra lại cấu hình/quyền.")
        sys.exit(1)
    fds_by_ad = {ad: list_fault_domains(identity_client, ad) for ad in ads}
    log.info("Fault Domain theo AD: %s", fds_by_ad)

    targets = [Target(INSTANCE_DISPLAY_NAME, ARM_SHAPE, OCI_IMAGE_OCID, BOOT_VOLUME_GB,
                      ocpus=OCPUS, memory_gb=MEMORY_GB, label=f"ARM {OCPUS:g}/{MEMORY_GB:g}GB")]
    if ENABLE_E2_FALLBACK:
        for i, n in enumerate(E2_INSTANCE_NAMES, 1):
            targets.append(Target(n, E2_SHAPE, OCI_E2_IMAGE_OCID, E2_BOOT_VOLUME_GB, label=f"E2 #{i}"))

    for t in targets:
        existing = instance_already_exists(compute_client, t.name)
        if existing:
            log.info("✅ [%s] '%s' đã có sẵn (ID: %s) — không tạo thêm.", t.label, t.name, existing)
            t.done = True
    if not any(t.active for t in targets):
        log.info("Tất cả máy cần tạo đều đã có — lượt chạy này kết thúc ngay.")
        return 0

    # Lệch lịch bắn thử giữa các máy để không bao giờ bắn 3 máy sát nhau (nguyên nhân 429 trong log 17/10).
    now = time.time()
    for i, t in enumerate(t for t in targets if t.active):
        t.next_blind = now + i * LAUNCH_GAP_SEC

    report_ok = True
    cycle = 0
    while time_left() > 5 and any(t.active for t in targets):
        cycle += 1
        if cycle % 10 == 1:
            pending = ", ".join(t.label for t in targets if t.active)
            log.info("--- Vòng dò #%d (còn %.0fs) — còn chờ: %s ---", cycle, time_left(), pending)

        for t in targets:
            if not t.active or time_left() <= 5:
                continue
            for ad in ads:
                if not t.active or time_left() <= 5:
                    break
                fds = fds_by_ad[ad]
                # Bước 1: báo cáo chỗ trống (rẻ). Có FD AVAILABLE -> bắn NGAY, bỏ qua giãn cách.
                available = None
                if report_ok:
                    available = capacity_report(compute_client, t, ad, fds)
                    if available is None:
                        report_ok = False
                if available:
                    t.seen_available += 1
                    for fd in available:
                        log.info("  >>> [%s] báo cáo CÓ CHỖ ở %s — bắn LaunchInstance NGAY!", t.label, fd)
                        inst = try_launch(compute_client, t, ad, fd, urgent=True)
                        if inst:
                            announce_success(t, inst, ad, fd)
                            t.done = True
                            break
                    continue
                # Bước 2: bắn thử phòng hờ (báo cáo có thể sai), giãn cách để không dính 429.
                if time.time() >= t.next_blind and time.time() - _last_any_launch >= LAUNCH_GAP_SEC:
                    fd = fds[t.fd_index % len(fds)]
                    t.fd_index += 1
                    t.next_blind = time.time() + (BLIND_LAUNCH_INTERVAL_SEC if report_ok else RETRY_INTERVAL_SEC)
                    inst = try_launch(compute_client, t, ad, fd, urgent=False)
                    if inst:
                        announce_success(t, inst, ad, fd)
                        t.done = True

        if not any(t.active for t in targets):
            break
        if not sleep_capped(CHECK_INTERVAL_SEC if report_ok else min(RETRY_INTERVAL_SEC, LAUNCH_GAP_SEC)):
            break

    for t in targets:
        state = "ĐÃ CÓ" if t.done else ("BỎ QUA (lỗi cấu hình/hạn mức)" if t.skipped else "chưa bắt được chỗ")
        log.info("Tổng kết [%s] '%s': %s — dò %d lần (thấy có chỗ %d lần), gọi LaunchInstance %d lần.",
                 t.label, t.name, state, t.reports, t.seen_available, t.launch_tries)
    log.info("Kết thúc lượt chạy sau %d vòng dò — lượt cron kế tiếp sẽ dò tiếp nếu còn máy chưa tạo.", cycle)
    return 0  # KHÔNG để job "Failed" — chưa bắt được chỗ trống không phải lỗi thật


if __name__ == "__main__":
    sys.exit(main())
