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
OCPUS = float(os.getenv("OCI_OCPUS", "4"))
MEMORY_GB = float(os.getenv("OCI_MEMORY_GB", "24"))
BOOT_VOLUME_GB = float(os.getenv("OCI_BOOT_VOLUME_GB", "50"))

# Job GitHub Actions nên đặt timeout-minutes lớn hơn số này 1-2 phút để không bị Actions tự ngắt
# giữa chừng (mất log). SỬA 01/10/2026: tăng từ 280s (~4.7 phút) lên 600s (10 phút) — gộp với
# RETRY_INTERVAL_SEC tăng lên 60s bên dưới, vẫn giữ được ~10 lần thử/lượt chạy nhưng dãn tần suất
# gọi API ra, AN TOÀN HƠN cho tài khoản (tránh bị Oracle coi là gọi API dồn dập/bất thường).
MAX_DURATION_SEC = int(os.getenv("MAX_DURATION_SEC", "600"))
# SỬA 01/10/2026: tăng từ 20s lên 60s — 20s là hơi dồn dập cho 1 API tạo tài nguyên (khác với API
# chỉ đọc dữ liệu), nhiều người dùng cộng đồng khuyến nghị tối thiểu 60s/lần gọi LaunchInstance để
# tránh bị Oracle gắn cờ là spam/lạm dụng (ngoài việc dễ bị 429 Too Many Requests).
RETRY_INTERVAL_SEC = int(os.getenv("RETRY_INTERVAL_SEC", "60"))

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


def get_oci_config() -> dict:
    """SỬA: GitHub Actions không có sẵn file ~/.oci/config như máy cá nhân — OCI SDK bắt buộc cần
    1 FILE private key thật trên đĩa (không nhận nội dung PEM trực tiếp qua config dict ở 1 số
    phiên bản SDK cũ), nên ghi tạm nội dung OCI_PRIVATE_KEY ra 1 file trong thư mục tạm rồi trỏ
    key_file vào đó."""
    key_path = os.path.join(tempfile.gettempdir(), "oci_api_key.pem")
    with open(key_path, "w") as f:
        f.write(OCI_PRIVATE_KEY.strip() + "\n")
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
