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
# 2 OCPU/12GB (áp dụng từ 15/06/2026, cho MỌI tài khoản). Tài khoản này chưa từng tạo được VM
# A1 thành công nên rất có khả năng KHÔNG được giữ hạn mức cũ (grandfathered) — xin 4/24 sẽ
# luôn thất bại dù Oracle có trống chỗ. Hạ về đúng 2/12 để khớp hạn mức mới.
OCPUS = float(os.getenv("OCI_OCPUS", "2"))
MEMORY_GB = float(os.getenv("OCI_MEMORY_GB", "12"))
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


def instance_already_exists(compute_client) -> str:
    """SỬA 01/10/2026: kiểm tra TRƯỚC KHI thử tạo — nếu đã có 1 instance tên đúng INSTANCE_DISPLAY_NAME
    và CHƯA bị terminate, nghĩa là lần chạy trước đã tạo thành công rồi (có thể Telegram gửi lỗi/mất
    mạng nên bạn không thấy thông báo) — bỏ qua, KHÔNG thử tạo thêm (tránh tạo trùng, tốn hết hạn mức
    Free Tier OCPU/RAM). Đây cũng là cách TỰ DỪNG của tool: 1 khi đã tạo được VM, các lần chạy cron
    sau tự nhận ra và không gọi API tạo nữa — tuy lịch cron trong workflow vẫn tiếp tục kích hoạt job
    (GitHub không tự tắt schedule), nhưng job sẽ thoát ngay trong vài giây, gần như không tốn phút
    chạy Actions hay gọi thêm API Oracle nữa. Muốn tắt hẳn lịch, vào tab Actions > chọn workflow này >
    nút "..." > Disable workflow."""
    states_to_ignore = {"TERMINATED", "TERMINATING"}
    try:
        resp = compute_client.list_instances(
            compartment_id=OCI_COMPARTMENT_OCID, display_name=INSTANCE_DISPLAY_NAME
        )
        for inst in resp.data:
            if inst.lifecycle_state not in states_to_ignore:
                return inst.id
    except oci.exceptions.ServiceError as e:
        log.warning("Không kiểm tra được instance đã tồn tại chưa (%s) — vẫn tiếp tục thử tạo.", e.message)
    return None


def list_availability_domains(identity_client) -> list:
    resp = identity_client.list_availability_domains(compartment_id=OCI_TENANCY_OCID)
    ads = [ad.name for ad in resp.data]
    log.info("Các Availability Domain khả dụng ở region %s: %s", OCI_REGION, ads)
    return ads


def try_launch_in_ad(compute_client, ad: str):
    """Thử tạo instance ở ĐÚNG 1 AD. Trả về (thành_công: bool, chi_tiết_lỗi_hoặc_None)."""
    details = oci.core.models.LaunchInstanceDetails(
        availability_domain=ad,
        compartment_id=OCI_COMPARTMENT_OCID,
        shape="VM.Standard.A1.Flex",
        shape_config=oci.core.models.LaunchInstanceShapeConfigDetails(
            ocpus=OCPUS, memory_in_gbs=MEMORY_GB
        ),
        display_name=INSTANCE_DISPLAY_NAME,
        create_vnic_details=oci.core.models.CreateVnicDetails(
            subnet_id=OCI_SUBNET_OCID, assign_public_ip=True
        ),
        source_details=oci.core.models.InstanceSourceViaImageDetails(
            image_id=OCI_IMAGE_OCID, boot_volume_size_in_gbs=BOOT_VOLUME_GB
        ),
        metadata={"ssh_authorized_keys": OCI_SSH_PUBLIC_KEY},
    )
    try:
        response = compute_client.launch_instance(details)
        return True, response.data
    except oci.exceptions.ServiceError as e:
        # "Out of host capacity" luôn trả HTTP 500 kèm code "InternalError" hoặc thông điệp chứa
        # "Out of host capacity" — đây là lỗi BÌNH THƯỜNG, KHÔNG phải lỗi cấu hình, cứ thử lại.
        is_capacity_error = "Out of host capacity" in (e.message or "") or "OutOfCapacity" in (e.code or "")
        if is_capacity_error:
            log.info("  AD %s: hết chỗ (Out of host capacity) — thử AD khác/lần sau.", ad)
        elif e.status == 429:
            log.warning("  AD %s: bị giới hạn tốc độ API (429) — chờ lâu hơn rồi thử lại.", ad)
            time.sleep(15)
        elif e.status == 401 or e.status == 404:
            # Đây LÀ lỗi cấu hình thật (sai OCID/quyền/key) — dừng hẳn, retry mãi cũng vô ích.
            log.error("  Lỗi cấu hình (%s): %s — KIỂM TRA LẠI OCID/QUYỀN, không phải lỗi hết chỗ.", e.status, e.message)
            send_telegram(f"❌ Script tạo VM Oracle dừng vì lỗi cấu hình ({e.status}): {e.message}\nKiểm tra lại OCID/quyền, không phải do hết chỗ.")
            sys.exit(1)
        else:
            log.warning("  AD %s: lỗi khác (%s): %s", ad, e.status, e.message)
        return False, e


def main():
    cfg = get_oci_config()
    identity_client = oci.identity.IdentityClient(cfg)
    compute_client = oci.core.ComputeClient(cfg)

    existing_id = instance_already_exists(compute_client)
    if existing_id:
        log.info("✅ Instance '%s' đã tồn tại (ID: %s) — KHÔNG thử tạo thêm. Lần chạy này coi như hoàn tất ngay.",
                  INSTANCE_DISPLAY_NAME, existing_id)
        return 0

    ads = list_availability_domains(identity_client)
    if not ads:
        log.error("Không lấy được danh sách Availability Domain — kiểm tra lại cấu hình/quyền.")
        sys.exit(1)

    start = time.time()
    attempt = 0
    while time.time() - start < MAX_DURATION_SEC:
        attempt += 1
        log.info("--- Lần thử #%d (đã chạy %.0fs/%ds) ---", attempt, time.time() - start, MAX_DURATION_SEC)
        for ad in ads:
            ok, result = try_launch_in_ad(compute_client, ad)
            if ok:
                msg = (f"🎉 ĐÃ TẠO THÀNH CÔNG VM '{INSTANCE_DISPLAY_NAME}'!\n"
                       f"Instance ID: {result.id}\nAD: {ad}\n"
                       f"Vào Console > Compute > Instances để xem Public IP (mất khoảng 1 phút để lên Running).")
                log.info(msg)
                send_telegram(msg)
                return 0
        time.sleep(RETRY_INTERVAL_SEC)

    log.info("Hết thời gian job lần này (chưa tạo được, đã thử %d lần) — chờ workflow chạy lần kế tiếp.", attempt)
    return 0  # KHÔNG để job "Failed" — đây là chưa bắt được chỗ trống, không phải lỗi thật


if __name__ == "__main__":
    sys.exit(main())
