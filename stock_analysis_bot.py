"""
stock_analysis_bot.py (bản mở rộng — 60+ mã)
================================================
Tự động lấy dữ liệu giá + khối lượng cổ phiếu Việt Nam (qua vnstock - nguồn công khai
VCI, KHÔNG cần đăng nhập tài khoản nào), tính RSI/MACD/CCI, phát hiện phân kỳ dương,
ước lượng dòng tiền lớn (qua khối lượng bất thường), kiểm tra định giá rẻ (P/E, P/B
so với trung bình lịch sử của chính mã đó), lấy xu hướng vĩ mô qua VN-Index, và đề
xuất chiến lược giải ngân với stop-loss cố định.

Gửi báo cáo qua Telegram và/hoặc Email.

Chạy độc lập trên máy của bạn theo lịch (cron / Task Scheduler) — xem README.md.
Không có mật khẩu hay dữ liệu nào của bạn đi qua Claude khi script này chạy.

Cài đặt: pip install -r requirements.txt
Cấu hình: copy .env.example -> .env rồi điền thông tin.

--------------------------------------------------------------------------------
SỬA NGÀY 28/09/2026 (bug giá "live" lấy nhầm dữ liệu cũ):
  get_foreign_flow_ssi() từng lấy `rows[-1]` mà không sắp xếp lại theo ngày, nên
  khi API SSI trả rows không đúng thứ tự tăng dần như kỳ vọng, script lấy nhầm giá
  của một phiên vài ngày trước (đã xảy ra thực tế: VTP báo giá 52.600đ trong khi giá
  đóng cửa thật hôm đó là 51.500đ, trùng khớp với dữ liệu phiên 4 ngày trước).
  Đã sửa: sắp xếp rows theo trading_date tường minh, chỉ dùng giá "live" khi nó mới
  hơn hoặc bằng phiên OHLC gần nhất VÀ không lệch quá 1.5% so với OHLC — nếu không,
  tự động dùng lại giá OHLC (nguồn đáng tin hơn) và ghi log cảnh báo.
--------------------------------------------------------------------------------
SỬA THÊM NGÀY 28/09/2026 (giảm thời gian chờ khi vnstock/VCI bị treo):
  get_valuation() gọi vnstock (nguồn VCI) để lấy P/E, P/B — thư viện vnstock KHÔNG cho
  cấu hình timeout HTTP nội bộ của nó (mặc định request ~30s), nên mỗi khi server
  iq.vietcap.com.vn/trading.vietcap.com.vn chậm/nghẽn, cả vòng quét bị "đứng hình" 30s/mã.
  Đã thêm: chạy các lệnh gọi vnstock đó trong 1 thread riêng, và CHỦ ĐỘNG bỏ qua kết quả
  nếu quá VALUATION_TIMEOUT_SEC giây (mặc định 8s, cấu hình qua .env) — script đi tiếp mã
  kế tiếp ngay, không phải chờ đủ 30s như trước. Không ảnh hưởng gì đến logic tính điểm/tín
  hiệu, chỉ là mã đó có thể tạm thời thiếu P/E, P/B ở lần chạy đó nếu server quá chậm.
  Cũng cho phép đổi nguồn P/E, P/B qua VALUATION_SOURCE (VCI hoặc TCBS, mặc định TCBS).

SỬA THÊM NGÀY 28/09/2026 (đọc kỹ docs chính thức của SSI FastConnect Python SDK):
  1. get_sector_map() giờ ưu tiên lấy phân ngành (ICB) TRỰC TIẾP từ SSI
     (data.market_data.get_securities_info_by_board) — dùng chung client/token đã đăng nhập,
     không cần phụ thuộc vnstock cho việc này nữa. Chỉ fallback sang vnstock nếu SSI lỗi/rỗng.
  2. Cấu hình rõ ràng timeout/max_retries/retry_delay cho SSIConfig (qua .env: SSI_TIMEOUT_SEC,
     SSI_MAX_RETRIES, SSI_RETRY_DELAY) — mặc định của ssi-sdk là timeout=60s, max_retries=5 với
     backoff tăng dần (2,4,8,16,32s...), nghĩa là 1 request SSI bị treo thật có thể khiến cả
     chương trình đứng hình VÀI PHÚT cho đúng 1 mã (rủi ro y hệt vụ vnstock, chỉ là chưa xảy ra
     vì SSI đang chạy ổn định) — đã hạ xuống mức an toàn hơn nhiều mà vẫn đủ dư dả cho request bình
     thường (quan sát thực tế chỉ ~150-300ms/request).
  3. Bắt riêng ssi_sdk.exceptions.RateLimitError (kèm retry_after) trong vòng lặp chính — trước
     đây code chỉ bắt SystemExit, vốn là hành vi rate-limit của vnai/vnstock, KHÔNG PHẢI của SSI
     SDK, nên nếu SSI thật sự trả rate limit, code cũ sẽ bỏ qua mã đó luôn thay vì chờ đúng theo
     retry_after rồi thử lại.
--------------------------------------------------------------------------------
SỬA NGÀY 29/09/2026 (lưu lịch sử theo ngày lên GitHub thay vì lưu ở máy):
  Trước đó save_daily_history() ghi 3 file CSV (macro_daily, sector_flow_daily,
  priority_daily) vào thư mục HISTORY_DIR trên máy chạy bot — nhưng máy chạy bot
  (cron/Task Scheduler) và máy sẽ xây dashboard web sau này chưa chắc là 1 máy, nên
  dữ liệu lưu cục bộ không tiện dùng lại. Đã đổi hoàn toàn sang lưu 1 file JSON DUY
  NHẤT trên GitHub (dùng chung GITHUB_TOKEN/GITHUB_REPO/GITHUB_BRANCH với
  ssi_data_export.py — có thể dùng chung 1 file .env), theo kiểu GET (lấy file +
  sha hiện có) -> gộp thêm dữ liệu hôm nay (khử trùng nếu chạy lại nhiều lần/ngày)
  -> cắt bớt cho chỉ giữ HISTORY_MAX_DAYS ngày gần nhất -> PUT (ghi đè lại, kèm sha).
  KHÔNG còn ghi bất kỳ file lịch sử nào ở máy nữa (bỏ hẳn ENABLE_HISTORY_LOG,
  HISTORY_DIR, _append_csv_row()).
  Nhân tiện mở rộng luôn dữ liệu lưu để làm nền tốt hơn cho dashboard web sau này:
  - tickers_daily giờ lưu CẢ 64 MÃ trong WATCHLIST (không chỉ 2 mã ưu tiên), có thêm
    cờ is_priority để dashboard vẫn phân biệt/nổi bật được mã ưu tiên khi cần, đồng
    thời vẫn xem được xu hướng toàn danh mục.
  - Thêm trade_value (giá trị giao dịch = giá x khối lượng), foreign_net_val (giá trị
    khối ngoại mua/bán ròng), sector, money_flow_basis vào mỗi dòng ticker — đều là
    số liệu SSI/vnstock đã tính sẵn trong lúc chạy, không tốn thêm request nào.
  - Thêm khối "_meta" mô tả cấu trúc dữ liệu (data_dictionary) + last_updated, để
    code xây dashboard sau này không cần đọc lại script mới hiểu được từng cột.

SỬA THÊM NGÀY 29/09/2026 (tách file lịch sử theo từng năm trên GitHub):
  HISTORY_GITHUB_PATH giờ là 1 MẪU tên file có "{year}" (mặc định "bot_history_{year}.json") —
  save_daily_history() tự thay {year} bằng năm hiện tại (datetime.now().year) khi đọc/ghi, nên
  mỗi năm sẽ tự có 1 file riêng trên GitHub (bot_history_2026.json, bot_history_2027.json,...).
  Sang năm mới, bot tự bắt đầu ghi vào file mới (rỗng) mà không đụng tới file năm cũ — không cần
  đổi gì thủ công. HISTORY_MAX_DAYS (cắt bớt dữ liệu quá cũ trong 1 file) vẫn giữ lại như 1 lớp an
  toàn phụ, đã nâng mặc định lên 400 ngày vì giờ 1 file chỉ còn chứa tối đa ~1 năm dữ liệu.

SỬA THÊM NGÀY 29/09/2026 (giá "live" bị đứng yên suốt phiên — báo cáo 09:00 và 11:11 y hệt nhau):
  get_foreign_flow_ssi() trước đó chỉ gọi get_securities_summary_historical() (API TỔNG HỢP LỊCH
  SỬ, dùng cho các phiên ĐÃ CHỐT) rồi lấy dòng cuối làm giá "hiện tại" — nhưng trong giờ giao dịch,
  dòng của hôm nay thường CHƯA được đưa vào lịch sử cho tới khi phiên kết thúc, nên hàm cứ trả mãi
  giá của phiên gần nhất ĐÃ CHỐT (hôm qua), khiến báo cáo chạy lúc 9h và 11h cùng ngày ra y hệt
  nhau — trông như bot bị đứng hình dù thực chất không phải. Đã sửa: ưu tiên gọi
  get_securities_summary(symbol) — đúng API "hiện tại" của SSI, cập nhật theo thời gian thực trong
  phiên — chỉ fallback về API lịch sử khi API hiện tại lỗi/rỗng (ngoài giờ giao dịch, cuối tuần,
  mã tạm ngưng giao dịch...).

SỬA THÊM LẦN 2 NGÀY 29/09/2026 (dòng tiền khối ngoại + room "biến mất" hoàn toàn trên Telegram lúc
  15:05, ngay sau khi đóng cửa): 2 sửa liền trước (tách "live"/"close", xem trên) làm
  get_foreign_flow_ssi() gọi CẢ 2 API/mã thay vì 1 — tăng gấp đôi số request SSI riêng phần này
  (64 -> 128 lệnh/lần quét 64 mã), nhiều khả năng có lúc vượt rate limit và lỗi bị nuốt êm (chỉ log
  debug) khiến TOÀN BỘ mã mất luôn dữ liệu khối ngoại mà không có cảnh báo rõ. Đã sửa 2 lớp: (1)
  chỉ gọi thêm API "hiện tại" khi ĐANG THỰC SỰ TRONG GIỜ GIAO DỊCH (_is_vn_trading_hours) — ngoài
  giờ, "hiện tại" và "đã chốt" vốn là 1 nên gọi thêm chỉ tốn request vô ích, nhờ vậy số request trở
  lại mức cũ (64) trong phần lớn thời gian trong ngày; (2) nếu "hiện tại" có giá nhưng THIẾU dữ
  liệu room khối ngoại (sát giờ mở/đóng cửa, API có thể chưa kịp cập nhật) mà "đã chốt" lại có, thì
  ghép phần room/khối ngoại từ "đã chốt" vào, giữ nguyên giá live — không để mất trắng thông tin.

SỬA THÊM LẦN 3 NGÀY 29/09/2026 (dòng tiền khối ngoại + room vẫn "đứng yên" y hệt cả ngày, kể cả
  sau khi đóng cửa — đã kiểm chứng bằng script chẩn đoán riêng, in dữ liệu thô từ SSI):
  1. BUG THẬT trong code: get_securities_summary(symbol) (API "hiện tại") thực tế trả về kiểu
     list[SecuritiesSummary], KHÔNG PHẢI 1 object đơn lẻ như tài liệu SDK gợi ý. Code cũ gọi thẳng
     _extract_summary_row(list_object) — getattr trên 1 cái list luôn ra rỗng/0 một cách ÂM THẦM
     (không lỗi, không cảnh báo), khiến API "hiện tại" từ đầu tới giờ chưa từng thực sự đóng góp
     dữ liệu. Đã sửa: lấy phần tử CUỐI của list (nếu có) làm dòng dữ liệu.
  2. GIỚI HẠN CỦA NGUỒN DỮ LIỆU (không phải lỗi code): kiểm chứng lúc 15:22, 37 phút sau khi đóng
     cửa, get_securities_summary_historical() vẫn CHƯA có dòng của hôm nay (chỉ tới hôm qua) — tức
     SSI có độ trễ công bố dữ liệu khối ngoại (total_foreign_buy_value...) LÂU HƠN cả sau giờ đóng
     cửa, không rõ chính xác bao lâu (có thể vài giờ, có thể qua hôm sau). Số "NN mua ròng" hiển
     thị suốt cả ngày 29/09 (52.6 tỷ cho BSR...) thực chất khớp 100% với dữ liệu phiên HÔM QUA
     (28/09), không phải lỗi tính toán — đơn giản là SSI chưa có số của hôm nay tại thời điểm đó.

SỬA THÊM LẦN 4 NGÀY 29/09/2026 (đã kiểm chứng: get_ohlc_1day_historical CÓ dữ liệu giá của hôm
  nay ngay trong ngày — VD VTP đóng cửa 52.600đ hôm 29/09 là SỐ THẬT của hôm nay, không phải dữ
  liệu cũ trùng hợp; trong khi API khối ngoại (get_securities_summary_historical) TRỄ HƠN, kiểm
  chứng 2 lần cách nhau 3 phút vẫn chỉ có tới hôm qua). 2 pipeline dữ liệu của SSI không đồng bộ
  ngày — điều này lộ ra 1 bug thật: analyze_ticker() từng lấy close_date (ngày gắn nhãn cho cả
  dòng lưu lịch sử) theo ngày của foreign_flow_close (TRỄ), nên giá ĐÚNG của hôm nay có thể bị gắn
  nhầm nhãn "hôm qua" khi lưu vào file JSON — ghi đè lên đúng dữ liệu hôm qua bằng giá hôm nay dán
  nhầm ngày. Đã sửa:
    - close_date giờ LUÔN lấy theo ngày của nến OHLC (đáng tin, có ngay trong ngày) — không còn
      phụ thuộc ngày của dữ liệu khối ngoại nữa.
    - foreign_net_val_close/foreign_room_pct_close CHỈ được điền khi ngày của foreign_flow_close
      KHỚP ĐÚNG với close_date (session_date) — nếu SSI chưa công bố kịp, để trống (None) thay vì
      gắn nhầm số của hôm qua vào hàng hôm nay. Lần chạy sau (khi SSI đã công bố kịp và session_date
      hôm đó trùng lại) sẽ tự điền đúng, vì save_daily_history() ghi đè theo đúng session_date.
--------------------------------------------------------------------------------
"""

import os
import sys
import time
import json
import base64
import logging
import smtplib
import concurrent.futures
from io import BytesIO
from email.mime.text import MIMEText
from datetime import datetime, timedelta, timezone

# Múi giờ Việt Nam (UTC+7) — dùng để hiển thị thời gian trong báo cáo/email/log commit,
# vì máy chủ chạy bot (ví dụ GitHub Actions) mặc định dùng giờ UTC, không phải giờ VN.
VN_TZ = timezone(timedelta(hours=7))


def now_vn() -> datetime:
    """Trả về thời điểm hiện tại theo giờ Việt Nam (UTC+7), dùng cho mọi chỗ HIỂN THỊ thời gian."""
    return datetime.now(VN_TZ)

import pandas as pd
import requests
from dotenv import load_dotenv

import matplotlib
matplotlib.use("Agg")  # chạy không cần màn hình (cron/Task Scheduler)
import matplotlib.pyplot as plt

try:
    from vnstock import Vnstock, Listing
    import vnai
except ImportError:
    print("Thiếu thư viện. Chạy: pip install -r requirements.txt")
    sys.exit(1)

try:
    import ta
except ImportError:
    print("Thiếu thư viện. Chạy: pip install -r requirements.txt")
    sys.exit(1)

load_dotenv()

# ========================= CẤU HÌNH =========================
DEFAULT_WATCHLIST = (
    "VPB,VTP,VGC,VHC,ACB,VND,VCG,TCX,STB,SIP,PVT,REE,KBC,PVD,NLG,NT2,PAN,PC1,"
    "MBS,MSH,MSN,HDB,HHV,GVR,DPG,DPM,DXG,DDV,BMP,CTR,BSR,BVH,CTG,BID,DCM,DGW,"
    "FPT,GAS,GEX,GMD,HAH,HCM,KDH,MBB,MWG,PHR,PNJ,POW,HPG,IDC,PVS,SAB,SSI,TCB,"
    "VCB,VHM,VIC,VJC,VCI,VNM"
)
WATCHLIST = [t.strip().upper() for t in os.getenv("WATCHLIST", DEFAULT_WATCHLIST).split(",") if t.strip()]
PRIORITY_TICKERS = [t.strip().upper() for t in os.getenv("PRIORITY_TICKERS", "VTP,CTR").split(",") if t.strip()]
# SỬA 30/09/2026: mã trong PRIORITY_TICKERS nhưng chưa có trong WATCHLIST sẽ được tự động
# thêm vào WATCHLIST để bot vẫn quét/phân tích được — người dùng chỉ cần sửa PRIORITY_TICKERS,
# không cần khai trùng mã ở cả 2 nơi nữa.
for _t in PRIORITY_TICKERS:
    if _t not in WATCHLIST:
        WATCHLIST.append(_t)

RSI_BUY_THRESHOLD = float(os.getenv("RSI_BUY_THRESHOLD", "60"))
SELL_RSI_THRESHOLD = float(os.getenv("SELL_RSI_THRESHOLD", "40"))
FOREIGN_ROOM_LOW_PCT = float(os.getenv("FOREIGN_ROOM_LOW_PCT", "10"))
STOP_LOSS_PCT = float(os.getenv("STOP_LOSS_PCT", "0.07"))
HISTORY_DAYS = int(os.getenv("HISTORY_DAYS", "260"))  # ~52 tuần, để tính đỉnh/đáy 52w
VOLUME_SPIKE_RATIO = float(os.getenv("VOLUME_SPIKE_RATIO", "1.5"))
ENABLE_VALUATION = os.getenv("ENABLE_VALUATION", "true").lower() == "true"
UNDERVALUED_MARGIN = float(os.getenv("UNDERVALUED_MARGIN", "0.9"))  # PE/PB hiện tại < 90% trung bình lịch sử
REQUEST_DELAY_SEC = float(os.getenv("REQUEST_DELAY_SEC", "0.5"))
VALUATION_CACHE_TTL_HOURS = float(os.getenv("VALUATION_CACHE_TTL_HOURS", "24"))
VNSTOCK_API_KEY = os.getenv("VNSTOCK_API_KEY", "").strip()
RATE_LIMIT_WAIT_SEC = int(os.getenv("RATE_LIMIT_WAIT_SEC", "65"))
OVERBOUGHT_RSI = float(os.getenv("OVERBOUGHT_RSI", "70"))
OVERSOLD_RSI = float(os.getenv("OVERSOLD_RSI", "30"))
SEND_CHART = os.getenv("SEND_CHART", "true").lower() == "true"
# Lưu lại số liệu quan trọng mỗi ngày lên GitHub (KHÔNG tốn thêm request API chứng khoán nào —
# chỉ ghi lại đúng những gì bot đã tính) — làm nền tảng để sau này vẽ biểu đồ xu hướng / xây
# dashboard web (dòng tiền lớn theo ngành qua nhiều ngày, diễn biến từng mã, vĩ mô VN-Index...).
# Tắt bằng ENABLE_HISTORY_LOG=false nếu không cần. Dùng chung GITHUB_TOKEN/GITHUB_REPO/GITHUB_BRANCH
# với ssi_data_export.py (có thể dùng chung 1 file .env) — chỉ khác đường dẫn file trên GitHub.
ENABLE_HISTORY_LOG = os.getenv("ENABLE_HISTORY_LOG", "true").lower() == "true"
GITHUB_TOKEN = os.getenv("GITHUB_TOKEN", "").strip()
GITHUB_REPO = os.getenv("GITHUB_REPO", "").strip()  # dạng "tenuser/tenrepo"
GITHUB_BRANCH = os.getenv("GITHUB_BRANCH", "main").strip()
# Đường dẫn (mẫu) cho file lịch sử của BOT này trên GitHub — khác với GITHUB_FILE_PATH/
# GEMINI_FILE_PATH của ssi_data_export.py để không ghi đè lẫn nhau dù dùng chung repo.
# Có "{year}" trong tên -> mỗi năm tự tạo 1 file riêng (bot_history_2026.json, bot_history_2027.json,...),
# file năm cũ không bị đụng tới nữa, năm mới bắt đầu từ file rỗng.
HISTORY_GITHUB_PATH = os.getenv("HISTORY_GITHUB_PATH", "bot_history_{year}.json").strip()
# Số ngày lịch sử tối đa giữ lại trong 1 file JSON (ngày cũ hơn sẽ bị cắt bớt để file không phình to mãi).
# Vì mỗi năm đã tách file riêng (tối đa ~366 ngày/file) nên chỉ cần > 366 là an toàn, không lo mất dữ liệu
# trong năm; để dư 400 phòng trường hợp chạy bù/chạy lại nhiều lần quanh giao thời năm.
HISTORY_MAX_DAYS = int(os.getenv("HISTORY_MAX_DAYS", "400"))
# Ngưỡng lệch tối đa cho phép giữa giá "live" (SecuritiesSummary) và giá OHLC trước khi
# coi giá "live" là bất thường/nhầm phiên và bỏ qua, dùng lại OHLC.
LIVE_PRICE_MAX_DEVIATION_PCT = float(os.getenv("LIVE_PRICE_MAX_DEVIATION_PCT", "1.5"))
# Thời gian chờ tối đa (giây) cho MỖI lệnh gọi vnstock bên trong get_valuation() (P/E, P/B).
# vnstock không cho cấu hình timeout HTTP nội bộ của nó (mặc định ~30s) nên phải chặn từ bên ngoài
# bằng thread + future.result(timeout=...) — xem ghi chú sửa ngày 28/09/2026 ở đầu file.
VALUATION_TIMEOUT_SEC = float(os.getenv("VALUATION_TIMEOUT_SEC", "8"))
# Nguồn dữ liệu P/E, P/B cho get_valuation() — "VCI" (Vietcap) hoặc "TCBS". Đổi thử qua .env
# (VALUATION_SOURCE=VCI hoặc VALUATION_SOURCE=TCBS) nếu 1 trong 2 bên hay bị timeout/chậm hơn.
VALUATION_SOURCE = os.getenv("VALUATION_SOURCE", "TCBS").strip().upper()

# ---- SSI FastConnect Data v3 (tùy chọn) — dùng API Key/API Secret, KHÔNG phải mật khẩu SSI ----
SSI_API_KEY = os.getenv("SSI_API_KEY", "").strip()
SSI_API_SECRET = os.getenv("SSI_API_SECRET", "").strip()
SSI_CLIENT_ID = os.getenv("SSI_CLIENT_ID", "").strip()  # tùy chọn, để trống nếu cổng không yêu cầu
DATA_SOURCE = os.getenv("DATA_SOURCE", "auto").strip().lower()  # auto | ssi | vnstock
ENABLE_FOREIGN_FLOW = os.getenv("ENABLE_FOREIGN_FLOW", "true").lower() == "true"
# Cấu hình SSI Config — mặc định của ssi-sdk khá "hào phóng" (timeout=60s, max_retries=5 với
# backoff tăng dần) nên nếu 1 request bị treo thật, cả script có thể đứng hình vài phút cho
# đúng 1 mã. Hạ xuống mức vẫn dư dả cho request bình thường (~150-300ms thực tế) nhưng an toàn
# hơn nhiều nếu SSI chậm/nghẽn bất thường. Chỉnh qua .env nếu cần.
SSI_TIMEOUT_SEC = int(os.getenv("SSI_TIMEOUT_SEC", "15"))
SSI_MAX_RETRIES = int(os.getenv("SSI_MAX_RETRIES", "3"))
SSI_RETRY_DELAY = float(os.getenv("SSI_RETRY_DELAY", "1.0"))

# SỬA 30/09/2026 (REST API của SSI trễ ~1 ngày — xem ghi chú lớn ở get_foreign_flow_ssi bên dưới):
# đã kiểm chứng thực tế — chạy giữa phiên sáng, get_ohlc_1day_historical VÀ get_securities_summary
# đều chỉ trả tới dữ liệu hôm qua, dù thị trường đang mở cửa. Đây là giới hạn THIẾT KẾ của API REST
# đó (dữ liệu tổng hợp theo lô, không phải lỗi/thiếu quyền — đã xác nhận API Key có đủ quyền
# "All api data" + "All stream data"). Dữ liệu THẬT SỰ real-time nằm ở kênh Streaming (WebSocket)
# riêng của SSI — bật bằng ENABLE_SSI_STREAM=true (mặc định bật) để dùng kênh này thay REST trong
# giờ giao dịch. Tắt (=false) để quay lại hoàn toàn REST như trước nếu streaming gây lỗi.
ENABLE_SSI_STREAM = os.getenv("ENABLE_SSI_STREAM", "true").lower() == "true"
# Thời gian (giây) mở kết nối streaming để "nghe" tick khớp lệnh + biến động khối ngoại cho CẢ
# DANH MỤC cùng lúc (1 kết nối duy nhất, không phải 1 request/mã như REST) trước khi ngắt và dùng
# kết quả thu được. Mã ít giao dịch có thể không kịp có tick nào trong khoảng này — sẽ tự dùng REST
# dự phòng cho riêng mã đó (xem get_foreign_flow_ssi). Tăng giá trị này nếu muốn "phủ" được nhiều
# mã hơn, nhưng nhớ vẫn phải nằm trong giới hạn timeout-minutes của workflow GitHub Actions.
SSI_STREAM_WAIT_SEC = float(os.getenv("SSI_STREAM_WAIT_SEC", "25"))

try:
    from ssi_sdk import Config as SSIConfig, Auth as SSIAuth, Data as SSIData, Stream as SSIStream
    from ssi_sdk.exceptions import RateLimitError as SSIRateLimitError
    from ssi_sdk.enums import Board as SSIBoard
    from ssi_sdk.models import TradeMessage as SSITradeMessage, ForeignRoomMessage as SSIForeignRoomMessage
    _SSI_SDK_AVAILABLE = True
except ImportError:
    _SSI_SDK_AVAILABLE = False

    class SSIRateLimitError(Exception):
        """Lớp giả — chỉ dùng để `except SSIRateLimitError` không lỗi khi thiếu ssi-sdk."""
        retry_after = None

_want_ssi = DATA_SOURCE == "ssi" or (DATA_SOURCE == "auto" and SSI_API_KEY and SSI_API_SECRET)
if _want_ssi and not _SSI_SDK_AVAILABLE:
    print("⚠️ Thiếu thư viện ssi-sdk (chạy: pip install -r requirements.txt) — dùng tạm vnstock.")
    RESOLVED_DATA_SOURCE = "vnstock"
elif _want_ssi:
    RESOLVED_DATA_SOURCE = "ssi"
else:
    RESOLVED_DATA_SOURCE = "vnstock"

# Tắt telemetry (không bắt buộc) + dùng API key nếu người dùng đã cấu hình (miễn phí,
# đăng ký ở vnstocks.com/login — KHÔNG phải mật khẩu QMV hay bất kỳ tài khoản nào khác)
os.environ.setdefault("VNSTOCK_TELEMETRY", "off")
if VNSTOCK_API_KEY:
    try:
        vnai.setup_api_key(VNSTOCK_API_KEY)
    except Exception:
        pass

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

SMTP_HOST = os.getenv("SMTP_HOST")
SMTP_PORT = int(os.getenv("SMTP_PORT", "587"))
SMTP_USER = os.getenv("SMTP_USER")
SMTP_PASS = os.getenv("SMTP_PASS")
EMAIL_TO = os.getenv("EMAIL_TO")
EMAIL_FROM = os.getenv("EMAIL_FROM", SMTP_USER)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)


# ========================= DỮ LIỆU GIÁ — VNSTOCK (nguồn dự phòng) =========================
def get_history_vnstock(symbol: str, days: int = HISTORY_DAYS) -> pd.DataFrame:
    stock = Vnstock().stock(symbol=symbol, source="VCI")
    end = datetime.now().strftime("%Y-%m-%d")
    df = stock.quote.history(end=end, count_back=days, interval="1D")
    return df.sort_values("time").reset_index(drop=True)


# ========================= DỮ LIỆU GIÁ — SSI FASTCONNECT DATA (SDK chính thức) =========================
_ssi_state = {"client": None, "auth": None}


def get_ssi_client():
    """Đăng nhập 1 lần cho cả lần chạy này, dùng lại cho mọi mã (không cần OTP để đọc dữ liệu)."""
    if _ssi_state["client"] is not None:
        return _ssi_state["client"]
    cfg = SSIConfig(
        client_id=SSI_CLIENT_ID, api_key=SSI_API_KEY, api_secret=SSI_API_SECRET,
        timeout=SSI_TIMEOUT_SEC, max_retries=SSI_MAX_RETRIES, retry_delay=SSI_RETRY_DELAY,
    )
    auth = SSIAuth(cfg)
    auth.authenticate()  # không truyền OTP — chỉ cần cho đọc dữ liệu, không cần cho đặt lệnh
    client = SSIData(auth)
    _ssi_state["client"] = client
    _ssi_state["auth"] = auth  # lưu lại để dùng chung cho kết nối Streaming (không cần login lại)
    return client


# ========================= DỮ LIỆU GIÁ — SSI STREAMING (WebSocket, THẬT SỰ real-time) =========================
# SỬA 30/09/2026: xem ghi chú lớn ở ENABLE_SSI_STREAM (phần CẤU HÌNH) và ở get_foreign_flow_ssi bên
# dưới — REST API (get_ohlc_1day_historical, get_securities_summary...) của SSI có độ trễ ~1 ngày
# theo THIẾT KẾ, không phải lỗi. Hàm này mở 1 kết nối WebSocket DUY NHẤT cho CẢ danh mục, "nghe" tick
# khớp lệnh (trade) + biến động khối ngoại (room) trong SSI_STREAM_WAIT_SEC giây rồi ngắt kết nối —
# trả về snapshot giá/dòng tiền khối ngoại MỚI NHẤT nhận được cho từng mã trong khoảng đó.
#
# Đây là best-effort: mã nào không có tick nào trong cửa sổ nghe (mã ít thanh khoản, hoặc lỗi kết
# nối) sẽ không có trong dict trả về — get_foreign_flow_ssi() tự dự phòng bằng REST cho riêng mã đó.
_current_stream_snapshot: dict = {}


def fetch_live_snapshot_ssi_stream(symbols: list, wait_seconds: float) -> dict:
    """Mở kết nối Streaming SSI, đăng ký nhận trade+room cho `symbols`, nghe trong `wait_seconds`
    giây rồi ngắt. Trả về {symbol: {"price", "trade_time", "total_volume", "net_val", "net_vol",
    "foreign_room_pct", "room_time"}} — chỉ chứa mã ĐÃ nhận được ít nhất 1 message trong lúc nghe.
    Trả về {} nếu lỗi bất kỳ (không chặn chương trình — vòng lặp chính tự dự phòng REST)."""
    if not _SSI_SDK_AVAILABLE or not symbols:
        return {}
    try:
        get_ssi_client()  # đảm bảo đã authenticate() — dùng lại đúng token đó cho streaming
        auth = _ssi_state.get("auth")
        if auth is None:
            return {}

        snapshot: dict = {}

        def _on_data(msg):
            try:
                if isinstance(msg, SSITradeMessage) and msg.symbol:
                    d = snapshot.setdefault(msg.symbol, {})
                    d["price"] = float(msg.price)
                    d["trade_time"] = msg.trading_time
                    d["total_volume"] = float(msg.total_volume) if msg.total_volume else d.get("total_volume")
                elif isinstance(msg, SSIForeignRoomMessage) and msg.symbol:
                    d = snapshot.setdefault(msg.symbol, {})
                    d["net_val"] = float(msg.buy_value - msg.sell_value)
                    d["net_vol"] = float(msg.buy_quantity - msg.sell_quantity)
                    d["foreign_room_pct"] = (
                        round(msg.current_room / msg.total_room * 100, 1) if msg.total_room else None
                    )
                    d["room_time"] = msg.trading_time
            except Exception as e:
                log.debug("Lỗi xử lý message streaming SSI: %s", e)

        with SSIStream(auth) as stream_client:
            stream_client.streaming.on_data = _on_data
            stream_client.streaming.connect()
            stream_client.streaming.subscribe_symbol_trade(symbols)
            stream_client.streaming.subscribe_symbol_room(symbols)
            stream_client.streaming.wait(timeout=wait_seconds)

        n_price = sum(1 for d in snapshot.values() if d.get("price") is not None)
        n_flow = sum(1 for d in snapshot.values() if d.get("net_val") is not None)
        log.info("Streaming SSI: nhận tick giá cho %d/%d mã, dữ liệu khối ngoại cho %d/%d mã (nghe %.0fs).",
                  n_price, len(symbols), n_flow, len(symbols), wait_seconds)
        return snapshot
    except Exception as e:
        log.warning("Streaming SSI lỗi (%s) — bỏ qua, toàn bộ mã dùng REST dự phòng như cũ.", e)
        return {}


def get_history_ssi(symbol: str, days: int = HISTORY_DAYS) -> pd.DataFrame:
    """Lấy giá OHLCV từ SSI FastConnect Data v3 (get_ohlc_1day_historical)."""
    client = get_ssi_client()
    to_date = datetime.now()
    from_date = to_date - timedelta(days=int(days * 1.6) + 10)  # nới rộng bù ngày nghỉ/lễ
    bars = client.market_data.get_ohlc_1day_historical(
        symbol, from_date.strftime("%Y/%m/%d 00:00:00"), to_date.strftime("%Y/%m/%d 23:59:59"), size=1000
    )
    if not bars:
        return pd.DataFrame(columns=["time", "open", "high", "low", "close", "volume"])
    df = pd.DataFrame([{
        "time": b.trading_date, "open": b.open_price, "high": b.high_price,
        "low": b.low_price, "close": b.close_price, "volume": b.volume,
    } for b in bars])
    df["time"] = pd.to_datetime(df["time"], errors="coerce")
    return df.dropna(subset=["time"]).sort_values("time").reset_index(drop=True)


def _extract_summary_row(row) -> dict:
    """Chuẩn hoá 1 dòng SecuritiesSummary (dù lấy từ API 'hiện tại' hay 'lịch sử') thành 1 dict
    gọn — dùng chung cho cả nhánh 'live' và 'close' bên dưới."""
    if row is None:
        return None
    net_val = (getattr(row, "total_foreign_buy_value", 0) or 0) - (getattr(row, "total_foreign_sell_value", 0) or 0)
    net_vol = (getattr(row, "total_foreign_buy", 0) or 0) - (getattr(row, "total_foreign_sell", 0) or 0)
    total_room = getattr(row, "total_foreign_room", 0) or 0
    remain_room = getattr(row, "remain_foreign_room", 0) or 0
    room_pct = (remain_room / total_room * 100) if total_room else None
    d = getattr(row, "trading_date", None) or getattr(row, "date", None)
    row_date = pd.to_datetime(d, errors="coerce") if d else pd.NaT
    return {
        "net_val": float(net_val), "net_vol": float(net_vol),
        "remain_foreign_room": float(remain_room), "total_foreign_room": float(total_room),
        "foreign_room_pct": round(room_pct, 1) if room_pct is not None else None,
        "close_price": float(row.close_price) if getattr(row, "close_price", None) else None,
        "change_pct": float(row.price_change_percent) if getattr(row, "price_change_percent", None) is not None else None,
        "date": row_date if pd.notna(row_date) else None,
    }


def _is_vn_trading_hours(now: datetime) -> bool:
    """Ước lượng thô giờ giao dịch HOSE (T2-T6, ~9:00-11:30 và ~13:00-14:45, giờ VN) — dùng để
    quyết định có cần gọi thêm API "hiện tại" hay không (xem SỬA NGÀY 29/09/2026 bên dưới). Chỉ cần
    ước lượng, không cần chính xác tuyệt đối tới từng phút — sai lệch vài phút không ảnh hưởng gì."""
    if now.weekday() >= 5:  # 5=Sat, 6=Sun
        return False
    t = now.time()
    morning = datetime.strptime("09:00", "%H:%M").time() <= t <= datetime.strptime("11:30", "%H:%M").time()
    afternoon = datetime.strptime("13:00", "%H:%M").time() <= t <= datetime.strptime("14:45", "%H:%M").time()
    return morning or afternoon


def get_foreign_flow_ssi(symbol: str):
    """Dòng tiền khối ngoại + room khối ngoại + giá, lấy từ SSI. Trả về {"live": {...}|None,
    "close": {...}|None} — HAI bộ dữ liệu tách biệt, dùng cho 2 mục đích khác nhau:
      - "live": dữ liệu HIỆN TẠI, cập nhật real-time trong phiên — dùng để hiển thị lên Telegram
        khi bot chạy giữa giờ giao dịch.
      - "close": dữ liệu của phiên giao dịch GẦN NHẤT ĐÃ CHỐT (hôm qua nếu đang trong giờ giao
        dịch, hôm nay nếu chạy sau khi đóng cửa) — dùng để lưu lịch sử (save_daily_history), luôn
        nhất quán bất kể bot chạy giờ nào trong ngày, không bị số liệu "nhảy" giữa các lần chạy.

    SỬA NGÀY 29/09/2026 (giá "live" bị đứng yên suốt phiên — báo cáo 09:00 và 11:11 y hệt nhau):
    Trước đây hàm này CHỈ gọi get_securities_summary_historical(symbol, 7 ngày trước, hôm_nay) rồi
    lấy dòng cuối làm giá "hiện tại". Nhưng theo đúng tài liệu SDK, đây là API TỔNG HỢP LỊCH SỬ
    (dùng để lấy các phiên ĐÃ CHỐT trong quá khứ) — SSI có 1 API RIÊNG cho đúng ý "hiện tại":
    get_securities_summary(symbol) (không có "_historical", không cần khoảng ngày).

    SỬA THÊM NGÀY 29/09/2026 (tách riêng "live" và "close"): Telegram cần giá LIVE trong phiên còn
    file lịch sử lưu GitHub thì cần giá của phiên ĐÃ CHỐT gần nhất — nên hàm gọi CẢ 2 API.

    SỬA THÊM LẦN 2 NGÀY 29/09/2026 (mất hẳn dòng tiền khối ngoại + room trên Telegram lúc 15:05,
    ngay sau khi đóng cửa): gọi CẢ 2 API/mã (thay vì 1 như trước) làm TĂNG GẤP ĐÔI số request SSI
    (64 -> 128 lệnh chỉ riêng phần này, cho 64 mã) — nhiều khả năng đã vượt rate limit ở 1 số thời
    điểm, và lỗi bị "nuốt" êm (chỉ log debug) bên trong hàm này nên không thấy cảnh báo rõ. Đã sửa
    2 lớp:
      1) CHỈ gọi thêm API "hiện tại" (get_securities_summary) khi ĐANG THỰC SỰ TRONG GIỜ GIAO DỊCH
         (_is_vn_trading_hours) — ngoài giờ, "hiện tại" và "đã chốt" vốn là 1, gọi thêm chỉ tốn
         request vô ích và tăng rủi ro rate limit. Nhờ vậy số request trở lại như cũ (64) trong
         phần lớn thời gian trong ngày.
      2) Nếu "hiện tại" trả về giá nhưng THIẾU hẳn dữ liệu room khối ngoại (total_foreign_room = 0,
         có thể xảy ra ngay sát giờ đóng/mở cửa khi API chưa kịp cập nhật) mà "đã chốt" lại CÓ, thì
         GHÉP phần room/khối ngoại từ "đã chốt" vào, giữ nguyên giá live — tránh mất trắng thông
         tin trên Telegram dù giá vẫn hiển thị đúng theo phiên."""
    client = get_ssi_client()
    # SỬA 30/09/2026: PHẢI dùng giờ VIỆT NAM ở đây (now_vn()), không phải datetime.now() (giờ hệ
    # thống máy chủ). Trên GitHub Actions, máy chủ chạy giờ UTC — _is_vn_trading_hours() so sánh
    # trực tiếp now.time() với các mốc "09:00"/"11:30"/"13:00"/"14:45" (vốn là giờ VN), nên nếu
    # truyền giờ UTC vào, bot chạy đúng 09:20 giờ VN (=02:20 UTC) sẽ bị coi là "ngoài giờ giao dịch"
    # và bỏ qua luôn API "hiện tại" — chỉ dùng dữ liệu phiên ĐÃ CHỐT (hôm qua), dù đang trong phiên.
    now = now_vn()

    # (1) Dữ liệu phiên ĐÃ CHỐT gần nhất — luôn qua API lịch sử, không phụ thuộc giờ chạy bot.
    close_data = None
    try:
        rows = client.market_data.get_securities_summary_historical(
            symbol, (now - timedelta(days=7)).strftime("%Y/%m/%d"), now.strftime("%Y/%m/%d")
        )
        if rows:
            def _row_date(row):
                d = getattr(row, "trading_date", None) or getattr(row, "date", None)
                return pd.to_datetime(d, errors="coerce") if d else pd.NaT
            # Sắp xếp tường minh theo ngày — không tin thứ tự API trả về (xem ghi chú sửa 28/09/2026).
            rows_sorted = sorted(rows, key=_row_date)
            close_data = _extract_summary_row(rows_sorted[-1])
    except Exception as e:
        log.debug("Không lấy được dữ liệu phiên đã chốt (lịch sử) cho %s: %s", symbol, e)

    # (2) Dữ liệu HIỆN TẠI (real-time trong phiên).
    #
    # SỬA 30/09/2026 (REST "hiện tại" hoá ra CŨNG trễ ~1 ngày như REST "đã chốt" — đã kiểm chứng
    # thực tế: chạy giữa phiên sáng, get_securities_summary trả về đúng dữ liệu HÔM QUA, không phải
    # hôm nay, dù thị trường đang mở cửa và API Key có đủ quyền "All api data"/"All stream data").
    # REST API của SSI (data-securitiesSummary, data-ohlc...) vốn là dữ liệu TỔNG HỢP THEO LÔ — chỉ
    # kênh Streaming (WebSocket) mới thực sự real-time. Vì vậy giờ ƯU TIÊN dùng snapshot lấy từ
    # fetch_live_snapshot_ssi_stream() (chạy 1 lần cho CẢ danh mục trước vòng lặp — xem main()), chỉ
    # fallback về REST "hiện tại" (biết là có thể trễ) cho những mã KHÔNG có trong snapshot đó (ít
    # thanh khoản, chưa kịp có tick nào trong cửa sổ nghe, hoặc streaming bị lỗi/tắt).
    live_data = None
    stream_row = _current_stream_snapshot.get(symbol)
    if stream_row and stream_row.get("price") is not None:
        live_data = {
            "net_val": stream_row.get("net_val"),
            "net_vol": stream_row.get("net_vol"),
            "remain_foreign_room": None,
            "total_foreign_room": None,
            "foreign_room_pct": stream_row.get("foreign_room_pct"),
            "close_price": stream_row["price"],
            "change_pct": None,  # streaming không có sẵn % so với hôm qua — analyze_ticker tự tính
            "date": pd.Timestamp(now.replace(tzinfo=None)),  # vừa nhận NGAY LƯỢT CHẠY NÀY — chắc chắn là "bây giờ"
            "total_volume": stream_row.get("total_volume"),
            "is_stream": True,
        }
        # Streaming có tick giá nhưng CHƯA có tick khối ngoại nào trong cửa sổ nghe (mã ít giao dịch
        # khối ngoại) — ghép tạm phần khối ngoại từ REST "đã chốt" (còn hơn không có gì), giữ nguyên
        # giá/thời điểm streaming (đáng tin hơn REST).
        if live_data.get("net_val") is None and close_data:
            for k in ("net_val", "net_vol", "remain_foreign_room", "total_foreign_room", "foreign_room_pct"):
                live_data[k] = close_data.get(k)
    elif _is_vn_trading_hours(now):
        # Streaming không có dữ liệu cho mã này (tắt/lỗi/mã chưa có tick trong cửa sổ nghe) — dự
        # phòng bằng REST "hiện tại" như trước đây (biết là có thể trễ 1 ngày, nhưng còn hơn không).
        #
        # SỬA NGÀY 29/09/2026 (get_securities_summary trả về LIST, không phải 1 object): đã kiểm
        # chứng thực tế — trả về list[SecuritiesSummary], KHÔNG PHẢI 1 object đơn lẻ như tài liệu
        # SDK gợi ý. Lấy phần tử CUỐI của list (nếu có) làm dòng dữ liệu.
        try:
            current = client.market_data.get_securities_summary(symbol)
            current_row = current[-1] if isinstance(current, list) and current else (
                current if current is not None and not isinstance(current, list) else None
            )
            live_data = _extract_summary_row(current_row)
        except Exception as e:
            log.debug("get_securities_summary (hiện tại) lỗi cho %s: %s", symbol, e)

        # Có giá live nhưng room/khối ngoại rỗng (total_foreign_room = 0) trong khi "đã chốt" có
        # sẵn — ghép phần room/khối ngoại từ "đã chốt" vào, KHÔNG đổi giá live.
        if live_data is not None and not live_data.get("total_foreign_room") and close_data:
            for k in ("net_val", "net_vol", "remain_foreign_room", "total_foreign_room", "foreign_room_pct"):
                live_data[k] = close_data.get(k)

    if live_data is None and close_data is None:
        return None
    # Ngoài giờ giao dịch/cuối tuần (hoặc "hiện tại" lỗi/rỗng): dùng "đã chốt" thay thế cho live
    # (2 số nên giống nhau lúc đó rồi, không lệch múi giờ đâu mà lo).
    return {"live": live_data or close_data, "close": close_data or live_data}


def get_market_breadth_ssi() -> dict:
    """Độ rộng toàn sàn HOSE + dòng tiền tự doanh, lấy từ SSI (chỉ số VNINDEX), 1 lần gọi cho cả báo cáo.
    Trả về None nếu lỗi hoặc không dùng SSI — không chặn chương trình."""
    if RESOLVED_DATA_SOURCE != "ssi":
        return None
    try:
        client = get_ssi_client()
        today_str = now_vn().strftime("%Y/%m/%d")  # ngày theo giờ VN, không phải giờ UTC của máy chủ
        s = client.market_data.get_index_summary_historical("VNINDEX", today_str)
        if s is None:
            return None
        return {
            "advance": int(s.total_advance_stock or 0),
            "decline": int(s.total_decline_stock or 0),
            "steady": int(s.total_steady_stock or 0),
            "ceiling": int(s.total_ceiling_stock or 0),
            "floor": int(s.total_floor_stock or 0),
            "prop_buy_value": float(s.total_prop_buy_value or 0),
            "prop_sell_value": float(s.total_prop_sell_value or 0),
            "total_match_value": float(s.total_match_value or 0),
        }
    except Exception as e:
        log.debug("Không lấy được độ rộng toàn sàn từ SSI: %s", e)
        return None


# ========================= DỮ LIỆU GIÁ — ĐIỀU PHỐI NGUỒN =========================
def get_history(symbol: str, days: int = HISTORY_DAYS) -> pd.DataFrame:
    """Dùng SSI nếu đã cấu hình (RESOLVED_DATA_SOURCE == 'ssi'), tự chuyển sang vnstock nếu SSI lỗi."""
    if RESOLVED_DATA_SOURCE == "ssi":
        try:
            df = get_history_ssi(symbol, days)
            if not df.empty:
                return df
            log.warning("SSI không có dữ liệu cho %s, chuyển sang vnstock.", symbol)
        except Exception as e:
            detail = getattr(e, "response_body", None) or getattr(e, "code", None)
            log.warning("SSI lỗi khi lấy dữ liệu %s (%s)%s — chuyển sang vnstock.",
                        symbol, e, f" | Chi tiết SSI: {detail}" if detail else "")
        # Dự phòng: vnstock trả giá theo đơn vị "nghìn đồng", còn SSI trả nguyên giá VND.
        # Quy đổi về cùng đơn vị VND với SSI để không bị lệch giá 1000 lần cho riêng mã này.
        df = get_history_vnstock(symbol, days)
        for col in ("open", "high", "low", "close"):
            if col in df.columns:
                df[col] = df[col] * 1000
        return df
    return get_history_vnstock(symbol, days)


# ========================= CHỈ BÁO KỸ THUẬT + KHỐI LƯỢNG =========================
def compute_indicators(df: pd.DataFrame) -> pd.DataFrame:
    df["rsi"] = ta.momentum.RSIIndicator(df["close"], window=14).rsi()
    macd = ta.trend.MACD(df["close"])
    df["macd"] = macd.macd()
    df["macd_signal"] = macd.macd_signal()
    df["macd_diff"] = macd.macd_diff()
    df["cci"] = ta.trend.CCIIndicator(df["high"], df["low"], df["close"], window=20).cci()
    df["volume_ma20"] = df["volume"].rolling(20).mean()
    df["volume_ratio"] = df["volume"] / df["volume_ma20"]
    return df


def detect_bullish_divergence(df: pd.DataFrame, lookback: int = 20) -> bool:
    """Heuristic đơn giản: đáy giá sau THẤP hơn đáy trước, nhưng đáy RSI sau CAO hơn."""
    window = df.tail(lookback).reset_index(drop=True)
    if len(window) < lookback or window["rsi"].isna().any():
        return False
    price_min_idx = window["close"].idxmin()
    earlier = window[window.index < price_min_idx]
    if earlier.empty:
        return False
    earlier_min_idx = earlier["close"].idxmin()
    price_lower_low = window.loc[price_min_idx, "close"] < earlier.loc[earlier_min_idx, "close"]
    rsi_higher_low = window.loc[price_min_idx, "rsi"] > earlier.loc[earlier_min_idx, "rsi"]
    return bool(price_lower_low and rsi_higher_low)


def detect_bearish_divergence(df: pd.DataFrame, lookback: int = 20) -> bool:
    """Heuristic đơn giản (đối xứng phân kỳ dương): đỉnh giá sau CAO hơn đỉnh trước,
    nhưng đỉnh RSI sau THẤP hơn — lực tăng đang suy yếu dù giá vẫn lên, cảnh báo đảo chiều xuống."""
    window = df.tail(lookback).reset_index(drop=True)
    if len(window) < lookback or window["rsi"].isna().any():
        return False
    price_max_idx = window["close"].idxmax()
    earlier = window[window.index < price_max_idx]
    if earlier.empty:
        return False
    earlier_max_idx = earlier["close"].idxmax()
    price_higher_high = window.loc[price_max_idx, "close"] > earlier.loc[earlier_max_idx, "close"]
    rsi_lower_high = window.loc[price_max_idx, "rsi"] < earlier.loc[earlier_max_idx, "rsi"]
    return bool(price_higher_high and rsi_lower_high)


def macd_cross_up_from_negative(df: pd.DataFrame) -> bool:
    if len(df) < 2:
        return False
    prev, curr = df.iloc[-2], df.iloc[-1]
    if pd.isna(prev["macd"]) or pd.isna(curr["macd"]):
        return False
    return bool(prev["macd"] < 0 and prev["macd"] <= prev["macd_signal"] and curr["macd"] > curr["macd_signal"])


def macd_cross_down_from_positive(df: pd.DataFrame) -> bool:
    """Cảnh báo: MACD vừa cắt xuống Signal khi đang ở vùng dương — tín hiệu tăng đang yếu đi."""
    if len(df) < 2:
        return False
    prev, curr = df.iloc[-2], df.iloc[-1]
    if pd.isna(prev["macd"]) or pd.isna(curr["macd"]):
        return False
    return bool(prev["macd"] > 0 and prev["macd"] >= prev["macd_signal"] and curr["macd"] < curr["macd_signal"])


def format_volume(v) -> str:
    """Định dạng khối lượng dễ đọc: 1.2 triệu cp / 350 nghìn cp."""
    if v is None or pd.isna(v):
        return "N/A"
    v = float(v)
    if v >= 1_000_000:
        return f"{v/1_000_000:.1f} triệu cp"
    if v >= 1_000:
        return f"{v/1_000:.0f} nghìn cp"
    return f"{v:.0f} cp"


def detect_money_flow(df: pd.DataFrame, foreign_flow: dict = None) -> tuple:
    """
    Trả về (nhãn, cơ_sở): nhãn 'vao_manh'|'ra_manh'|'binh_thuong', cơ_sở 'khoi_ngoai'|'khoi_luong'.
    Ưu tiên dòng tiền khối ngoại thật (SSI) nếu có; không thì dùng khối lượng bất thường.
    """
    if foreign_flow is not None and foreign_flow.get("net_val") is not None:
        net_val = foreign_flow["net_val"]
        threshold = 2_000_000_000  # 2 tỷ VNĐ mua/bán ròng — ngưỡng coi là đáng chú ý
        if net_val >= threshold:
            return "vao_manh", "khoi_ngoai"
        if net_val <= -threshold:
            return "ra_manh", "khoi_ngoai"
        return "binh_thuong", "khoi_ngoai"

    if len(df) < 21:
        return "binh_thuong", "khoi_luong"
    last, prev = df.iloc[-1], df.iloc[-2]
    if pd.isna(last["volume_ratio"]):
        return "binh_thuong", "khoi_luong"
    price_up = last["close"] > prev["close"]
    if last["volume_ratio"] >= VOLUME_SPIKE_RATIO and price_up:
        return "vao_manh", "khoi_luong"
    if last["volume_ratio"] >= VOLUME_SPIKE_RATIO and not price_up:
        return "ra_manh", "khoi_luong"
    return "binh_thuong", "khoi_luong"


# ========================= CACHE ĐỊNH GIÁ LỊCH SỬ =========================
# Dữ liệu P/E, P/B trung bình theo quý thay đổi chậm — cache qua ngày để tránh
# gọi lại vnstock 2 lần/mã ở những lần chạy sau trong cùng ngày (tiết kiệm nhiều thời gian
# vì đây là phần chậm nhất khi dùng nguồn SSI cho giá).
VALUATION_CACHE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "valuation_cache.json")
_valuation_cache_state = {"data": None}


def _load_valuation_cache() -> dict:
    if _valuation_cache_state["data"] is not None:
        return _valuation_cache_state["data"]
    try:
        with open(VALUATION_CACHE_PATH, "r", encoding="utf-8") as f:
            _valuation_cache_state["data"] = json.load(f)
    except Exception:
        _valuation_cache_state["data"] = {}
    return _valuation_cache_state["data"]


def _save_valuation_cache():
    if _valuation_cache_state["data"] is None:
        return
    try:
        with open(VALUATION_CACHE_PATH, "w", encoding="utf-8") as f:
            json.dump(_valuation_cache_state["data"], f)
    except Exception as e:
        log.debug("Không lưu được cache định giá: %s", e)


# ========================= ĐỊNH GIÁ (P/E, P/B) — BEST-EFFORT =========================
# Pool thread riêng cho các lệnh gọi vnstock/VCI trong get_valuation() — dùng chung 1 pool
# cho cả lần chạy để không tạo/hủy thread liên tục, nhưng vẫn cho phép "bỏ mặc" (abandon)
# một cuộc gọi bị treo mà không phải chờ nó tự kết thúc.
_valuation_executor = concurrent.futures.ThreadPoolExecutor(max_workers=4, thread_name_prefix="valuation")


def _call_with_timeout(func, timeout: float, *args, **kwargs):
    """Chạy func(*args, **kwargs) trong 1 thread riêng, CHỦ ĐỘNG bỏ qua nếu quá `timeout` giây.

    Vì vnstock không cho cấu hình timeout HTTP nội bộ của nó, đây là cách duy nhất để giới hạn
    thời gian chờ từ bên ngoài. Lưu ý: nếu request thật sự bị treo lâu hơn timeout, thread chạy
    ngầm đó vẫn có thể tiếp tục tới khi tự nó timeout/xong (không có cách nào "giết" 1 thread
    Python đang chờ I/O từ bên ngoài) — nhưng vòng lặp chính KHÔNG còn phải chờ nó nữa, nên script
    đi tiếp mã kế tiếp ngay lập tức thay vì đứng hình ~30s/mã như trước.
    """
    future = _valuation_executor.submit(func, *args, **kwargs)
    try:
        return future.result(timeout=timeout)
    except concurrent.futures.TimeoutError:
        raise TimeoutError(f"vnstock không phản hồi sau {timeout}s (đã bỏ qua, đi tiếp)")


def _find_column(cols, exact_tokens=None, contains_all=None):
    for c in cols:
        low = str(c).lower()
        tokens = low.replace("/", "_").split("_")
        if exact_tokens and any(t in tokens for t in exact_tokens):
            return c
        if contains_all and all(k in low for k in contains_all):
            return c
    return None


def get_valuation(symbol: str) -> dict:
    """
    Trả về {pe, pb, pe_avg_hist, pb_avg_hist, undervalued}.
    LƯU Ý: tên cột API tài chính có thể thay đổi theo phiên bản vnstock — hàm này dò
    tên cột tự động (fuzzy). Nếu không dò được, trả về giá trị None, không báo lỗi.
    Phần lịch sử (pe_avg_hist/pb_avg_hist) được cache qua ngày để chạy nhanh hơn.
    """
    result = {"pe": None, "pb": None, "pe_avg_hist": None, "pb_avg_hist": None, "undervalued": False}
    if not ENABLE_VALUATION:
        return result

    cache = _load_valuation_cache()
    cached = cache.get(symbol)
    use_cached_hist = bool(
        cached and (time.time() - cached.get("ts", 0)) < VALUATION_CACHE_TTL_HOURS * 3600
    )
    if use_cached_hist:
        result["pe_avg_hist"] = cached.get("pe_avg_hist")
        result["pb_avg_hist"] = cached.get("pb_avg_hist")

    try:
        stock = _call_with_timeout(Vnstock().stock, VALUATION_TIMEOUT_SEC, symbol=symbol, source=VALUATION_SOURCE)

        # Định giá hiện tại (snapshot) — luôn lấy mới, không cache (cần dữ liệu mới nhất)
        snap = _call_with_timeout(stock.company.ratio_summary, VALUATION_TIMEOUT_SEC)
        if snap is not None and not snap.empty:
            pe_col = _find_column(snap.columns, exact_tokens=["pe"]) or _find_column(snap.columns, contains_all=["price", "earn"])
            pb_col = _find_column(snap.columns, exact_tokens=["pb"]) or _find_column(snap.columns, contains_all=["price", "book"])
            if pe_col:
                result["pe"] = float(snap.iloc[0][pe_col])
            if pb_col:
                result["pb"] = float(snap.iloc[0][pb_col])

        # Trung bình lịch sử (khoảng 10 quý gần nhất) — chỉ gọi lại nếu cache đã hết hạn/chưa có
        if not use_cached_hist:
            hist = _call_with_timeout(stock.finance.ratio, VALUATION_TIMEOUT_SEC, period="quarter")
            if hist is not None and not hist.empty:
                hist_cols = hist.columns if not isinstance(hist.columns, pd.MultiIndex) else [c[-1] for c in hist.columns]
                hist.columns = hist_cols
                pe_col_h = _find_column(hist.columns, exact_tokens=["pe"]) or _find_column(hist.columns, contains_all=["price", "earn"])
                pb_col_h = _find_column(hist.columns, exact_tokens=["pb"]) or _find_column(hist.columns, contains_all=["price", "book"])
                recent = hist.head(10)  # dữ liệu thường mới nhất ở đầu
                if pe_col_h:
                    vals = pd.to_numeric(recent[pe_col_h], errors="coerce").dropna()
                    if len(vals) >= 3:
                        result["pe_avg_hist"] = float(vals.mean())
                if pb_col_h:
                    vals = pd.to_numeric(recent[pb_col_h], errors="coerce").dropna()
                    if len(vals) >= 3:
                        result["pb_avg_hist"] = float(vals.mean())
            cache[symbol] = {
                "pe_avg_hist": result["pe_avg_hist"],
                "pb_avg_hist": result["pb_avg_hist"],
                "ts": time.time(),
            }

        conds = []
        if result["pe"] and result["pe_avg_hist"]:
            conds.append(result["pe"] < result["pe_avg_hist"] * UNDERVALUED_MARGIN)
        if result["pb"] and result["pb_avg_hist"]:
            conds.append(result["pb"] < result["pb_avg_hist"] * UNDERVALUED_MARGIN)
        result["undervalued"] = bool(conds and any(conds))
    except Exception as e:
        log.debug("Không lấy được định giá cho %s: %s", symbol, e)
    return result


# ========================= NGÀNH (ICB) — 1 lần gọi cho cả danh mục =========================
def get_sector_map_ssi() -> dict:
    """symbol -> tên ngành (ICB), lấy TRỰC TIẾP từ SSI FastConnect (get_securities_info_by_board),
    dùng chung client/token đã đăng nhập — không cần phụ thuộc vnstock cho việc này nữa.
    Trả về {} nếu lỗi hoặc rỗng (để get_sector_map() tự fallback sang vnstock)."""
    if not _SSI_SDK_AVAILABLE:
        return {}
    try:
        client = get_ssi_client()
        mapping = {}
        for board in (SSIBoard.HOSE, SSIBoard.HNX, SSIBoard.UPCOM):
            try:
                infos = client.market_data.get_securities_info_by_board(board)
            except Exception as e:
                log.debug("Không lấy được securities_info cho sàn %s: %s", board, e)
                continue
            for info in infos or []:
                name = getattr(info, "icb_name", None)
                sym = getattr(info, "symbol", None)
                if sym and name:
                    mapping[sym] = name
        return mapping
    except Exception as e:
        log.debug("Không lấy được phân ngành từ SSI: %s", e)
        return {}


def get_sector_map_vnstock() -> dict:
    """symbol -> tên ngành (ICB cấp 2, cấp 1 nếu thiếu cấp 2), lấy qua vnstock (dự phòng).
    Trả về {} nếu lỗi, không chặn chương trình."""
    try:
        df = Listing(source="vci").symbols_by_industries(lang="vi")
        if df is None or df.empty:
            return {}
        mapping = {}
        lv1 = df[df["icb_level"] == 1]
        lv2 = df[df["icb_level"] == 2]
        for sym, name in zip(lv1["symbol"], lv1["icb_name"]):
            mapping[sym] = name
        for sym, name in zip(lv2["symbol"], lv2["icb_name"]):  # cấp 2 cụ thể hơn, ghi đè cấp 1
            mapping[sym] = name
        return mapping
    except Exception as e:
        log.debug("Không lấy được dữ liệu phân ngành qua vnstock: %s", e)
        return {}


def get_sector_map() -> dict:
    """Ưu tiên lấy phân ngành từ SSI (nếu đang dùng SSI làm nguồn chính) — nhanh hơn, không tốn
    thêm phụ thuộc vnstock. Chỉ fallback sang vnstock nếu SSI lỗi/rỗng hoặc không dùng SSI."""
    if RESOLVED_DATA_SOURCE == "ssi":
        mapping = get_sector_map_ssi()
        if mapping:
            return mapping
        log.warning("Không lấy được phân ngành từ SSI (rỗng/lỗi) — chuyển sang vnstock.")
    return get_sector_map_vnstock()


# ========================= VĨ MÔ (VN-INDEX) =========================
def get_macro_context() -> dict:
    """Xu hướng VN-Index để tham khảo chiến lược giải ngân. Trả về None nếu không lấy được.
    Luôn dùng vnstock cho VNINDEX — endpoint OHLC của SSI chỉ hỗ trợ mã cổ phiếu, không hỗ trợ mã chỉ số."""
    try:
        df = get_history_vnstock("VNINDEX", days=100)
        if df.empty:
            return None
        df = compute_indicators(df)
        last = df.iloc[-1]
        if last["macd"] > 0 and 40 <= last["rsi"] <= 75:
            trend = "tang"
        elif last["macd"] < 0:
            trend = "giam"
        else:
            trend = "di_ngang"
        return {"price": float(last["close"]), "rsi": round(float(last["rsi"]), 1),
                "macd_diff": round(float(last["macd_diff"]), 2), "trend": trend}
    except Exception as e:
        log.warning("Không lấy được dữ liệu VN-Index để đánh giá vĩ mô: %s", e)
        return None


def get_macro_closing_for_date(session_date) -> dict:
    """Vĩ mô VN-Index của ĐÚNG 1 phiên cụ thể (session_date, kiểu date) — dùng RIÊNG cho lưu lịch
    sử (save_daily_history), tách biệt với get_macro_context() (vốn lấy dòng MỚI NHẤT trong dữ
    liệu vnstock, có thể là 1 phiên đang hình thành dở dang trong ngày — dùng cho hiển thị Telegram).

    SỬA NGÀY 29/09/2026: cần hàm riêng vì get_macro_context() không đảm bảo trả về đúng phiên đã
    chốt khi chạy giữa giờ giao dịch (vnstock có thể cập nhật dần nến "hôm nay" trong ngày) — nếu
    dùng thẳng get_macro_context() để lưu lịch sử, macro_daily có thể bị lệch ngày/lẫn dữ liệu
    nửa-phiên so với tickers_daily và sector_flow_daily (vốn đã chốt hẳn theo session_date)."""
    if session_date is None:
        return None
    try:
        df = get_history_vnstock("VNINDEX", days=100)
        if df.empty:
            return None
        df = compute_indicators(df)
        row_dates = pd.to_datetime(df["time"]).dt.date
        match = df[row_dates == session_date]
        if match.empty:
            match = df[row_dates <= session_date]
            if match.empty:
                return None
            match = match.tail(1)
        last = match.iloc[-1]
        if last["macd"] > 0 and 40 <= last["rsi"] <= 75:
            trend = "tang"
        elif last["macd"] < 0:
            trend = "giam"
        else:
            trend = "di_ngang"
        return {"price": float(last["close"]), "rsi": round(float(last["rsi"]), 1),
                "macd_diff": round(float(last["macd_diff"]), 2), "trend": trend}
    except Exception as e:
        log.debug("Không lấy được VN-Index đã chốt cho ngày %s: %s", session_date, e)
        return None


def suggest_strategy(macro: dict, buy_count: int) -> str:
    if macro is None:
        return "Không xác định được xu hướng vĩ mô lúc này — nên giải ngân từng phần, thận trọng."
    if macro["trend"] == "tang" and buy_count >= 5:
        return "VN-Index đang trong xu hướng tăng và có nhiều tín hiệu mua — có thể giải ngân 30-50% vốn dự kiến, ưu tiên mã có định giá rẻ."
    if macro["trend"] == "tang":
        return "VN-Index đang tăng nhưng số tín hiệu mua còn ít — giải ngân thăm dò 20-30%, chờ thêm xác nhận."
    if macro["trend"] == "giam":
        return "VN-Index đang suy yếu — nên hạn chế giải ngân mới (tối đa 10-15%), ưu tiên quan sát và giữ tỷ trọng tiền mặt cao."
    return "VN-Index đi ngang — giải ngân từng phần 15-25%, ưu tiên các mã có tín hiệu rõ ràng nhất."


# ========================= PHÂN TÍCH 1 MÃ =========================
def analyze_ticker(symbol: str, sector_map: dict = None) -> dict:
    df = get_history(symbol)
    if df.empty or len(df) < 30:
        raise ValueError(f"Không đủ dữ liệu cho {symbol}")
    df = compute_indicators(df)
    last = df.iloc[-1]
    prev = df.iloc[-2] if len(df) >= 2 else last

    macd_cross = macd_cross_up_from_negative(df)
    macd_cross_down = macd_cross_down_from_positive(df)
    divergence = detect_bullish_divergence(df)
    # Phân kỳ âm (cho tín hiệu BÁN) chỉ tính cho mã ưu tiên — theo đúng yêu cầu, không cần cho cả danh mục
    bearish_divergence = detect_bearish_divergence(df) if symbol in PRIORITY_TICKERS else False

    foreign_flow = None
    if RESOLVED_DATA_SOURCE == "ssi" and ENABLE_FOREIGN_FLOW:
        foreign_flow = get_foreign_flow_ssi(symbol)
    # detect_money_flow dùng để CHẤM ĐIỂM/HIỂN THỊ ngay lúc chạy — dùng bản "live" (real-time
    # trong phiên). Bản "close" (phiên đã chốt) chỉ dùng riêng cho lưu lịch sử, xem bên dưới.
    foreign_flow_live = foreign_flow["live"] if foreign_flow else None
    foreign_flow_close = foreign_flow["close"] if foreign_flow else None
    money_flow, money_flow_basis = detect_money_flow(df, foreign_flow=foreign_flow_live)
    valuation = get_valuation(symbol)

    window52 = df.tail(min(len(df), 252))
    week52_high = float(window52["high"].max())
    week52_low = float(window52["low"].min())

    rsi_val = last["rsi"]
    rsi_rounded = round(float(rsi_val), 1) if pd.notna(rsi_val) else None
    price = float(last["close"])
    prev_price = float(prev["close"])
    price_change_pct = round((price / prev_price - 1) * 100, 2) if prev_price else None
    ohlc_price_for_debug = price  # giá gốc từ nến ngày, lưu TRƯỚC khi ghi đè — dùng để chẩn đoán
    ohlc_change_pct_debug = price_change_pct  # % thay đổi gốc từ nến ngày, cũng lưu TRƯỚC khi ghi đè
    ohlc_date = pd.to_datetime(last["time"]) if pd.notna(last.get("time")) else None

    # Ưu tiên GIÁ + % THAY ĐỔI THẬT trong phiên từ SSI (đã có sẵn trong foreign_flow, không tốn
    # thêm request) — nến ngày (OHLC) có thể CHƯA cập nhật đúng phiên hôm nay khi chạy giữa giờ
    # giao dịch, khiến giá/% bị lùi 1 phiên (ví dụ hiện % thay đổi của hôm qua thay vì hôm nay).
    #
    # SỬA 28/09/2026: giá "live" chỉ được chấp nhận khi ĐỒNG THỜI thỏa 2 điều kiện —
    #   (1) MỚI HƠN HOẶC BẰNG phiên OHLC gần nhất (không lấy nhầm 1 phiên cũ hơn), và
    #   (2) không lệch quá LIVE_PRICE_MAX_DEVIATION_PCT so với giá OHLC (chặn dữ liệu rác).
    # Nếu không thỏa, giữ nguyên giá OHLC — nguồn đáng tin hơn — và ghi log cảnh báo thay vì
    # âm thầm dùng một con số có thể sai.
    #
    # SỬA THÊM 29/09/2026: giá "live" giờ lấy từ foreign_flow_live (API get_securities_summary —
    # xem ghi chú sửa 29/09/2026 ở get_foreign_flow_ssi). Giá/​% "đã chốt" (foreign_flow_close,
    # cùng ohlc_price_for_debug/ohlc_change_pct_debug) KHÔNG bị ghi đè ở đây — giữ nguyên để
    # save_daily_history() dùng lưu lịch sử, tách biệt hẳn với giá hiển thị Telegram.
    price_source = "ohlc"
    live_volume_override = None  # chỉ có giá trị khi giá đến từ streaming (xem nhánh is_stream)
    if foreign_flow_live and foreign_flow_live.get("close_price"):
        live_price = foreign_flow_live["close_price"]

        # SỬA 30/09/2026: giá từ kênh STREAMING (WebSocket) là tick thật nhận trực tiếp trong lượt
        # chạy này — ĐÁNG TIN NGAY, không cần (và không nên) đối chiếu lệch % với OHLC nữa, vì OHLC
        # REST cũng bị trễ ~1 ngày (đã kiểm chứng — xem ghi chú lớn ở get_foreign_flow_ssi), nên lấy
        # nó làm "mốc" để nghi ngờ giá streaming thật trong phiên là sai logic — 1 mã biến động vài %
        # so với giá đóng cửa HÔM QUA là chuyện bình thường, không phải dữ liệu rác.
        if foreign_flow_live.get("is_stream"):
            price = live_price
            price_source = "stream"
            # Streaming không có sẵn field % thay đổi — tự tính so với giá đóng cửa gần nhất mà OHLC
            # có (ohlc_price_for_debug) — đây chính là mốc chuẩn để tính "% thay đổi trong phiên".
            if ohlc_price_for_debug:
                price_change_pct = round((live_price / ohlc_price_for_debug - 1) * 100, 2)
            if foreign_flow_live.get("total_volume"):
                live_volume_override = foreign_flow_live["total_volume"]
        else:
            # Giá từ REST "hiện tại" (get_securities_summary) — vẫn có thể trễ 1 phiên như REST khác,
            # nên GIỮ nguyên 2 lớp kiểm tra cũ (mới hơn OHLC + không lệch quá ngưỡng) trước khi tin.
            live_date = foreign_flow_live.get("date")
            is_fresh = (
                live_date is not None and ohlc_date is not None
                and pd.notna(live_date) and live_date.normalize() >= ohlc_date.normalize()
            )
            deviation_pct = abs(live_price - ohlc_price_for_debug) / ohlc_price_for_debug * 100 if ohlc_price_for_debug else None
            is_sane = deviation_pct is not None and deviation_pct <= LIVE_PRICE_MAX_DEVIATION_PCT

            if is_fresh and is_sane:
                price = live_price
                price_source = "live"
                if foreign_flow_live.get("change_pct") is not None:
                    price_change_pct = foreign_flow_live["change_pct"]
            else:
                reason = "không phải phiên mới nhất" if not is_fresh else f"lệch {deviation_pct:.1f}% so với OHLC"
                log.warning("%s: bỏ qua giá 'live' (%.0f, %s) — dùng giá OHLC (%.0f) thay thế.",
                            symbol, live_price, reason, ohlc_price_for_debug)

    # Ngày của phiên ĐÃ CHỐT gần nhất — LUÔN lấy theo ngày của nến OHLC (đáng tin, có sẵn ngay
    # trong ngày — đã kiểm chứng qua ssi_foreign_flow_debug.py: get_ohlc_1day_historical CÓ dữ liệu
    # của hôm nay ngay cả giữa/​sau phiên). KHÔNG dùng ngày của foreign_flow_close nữa — SSI công bố
    # dữ liệu khối ngoại (total_foreign_buy_value...) TRỄ HƠN giá (đã kiểm chứng thực tế: 15:25, 40
    # phút sau khi đóng cửa, API khối ngoại vẫn chỉ có tới hôm qua) — nếu dùng ngày của nó làm
    # session_date, giá ĐÚNG của hôm nay sẽ bị gắn nhầm nhãn "hôm qua" khi lưu lịch sử.
    close_date = ohlc_date
    close_date_str = close_date.strftime("%Y-%m-%d") if close_date is not None and pd.notna(close_date) else None
    close_price = ohlc_price_for_debug
    close_volume = float(last["volume"]) if pd.notna(last["volume"]) else None

    # Dữ liệu khối ngoại "đã chốt" CHỈ dùng khi ngày của nó KHỚP đúng session_date (close_date) —
    # nếu SSI chưa công bố kịp (ngày của foreign_flow_close cũ hơn), để trống (None) thay vì gắn
    # nhầm số liệu của hôm qua vào hàng của hôm nay. Lần chạy sau (khi SSI đã công bố kịp) sẽ tự
    # điền đúng, vì save_daily_history() ghi đè theo đúng session_date mỗi lần chạy.
    foreign_close_date = (foreign_flow_close or {}).get("date")
    foreign_close_matches = (
        foreign_flow_close is not None and close_date is not None
        and foreign_close_date is not None and pd.notna(foreign_close_date)
        and pd.notna(close_date) and foreign_close_date.normalize() == close_date.normalize()
    )

    result = {
        "ticker": symbol,
        "price": price,
        "price_change_pct": price_change_pct,
        "price_source": price_source,
        "ohlc_price_debug": ohlc_price_for_debug,
        # CHẨN ĐOÁN 30/09/2026: ngày của nến OHLC và ngày của dữ liệu "live" (foreign_flow_live) —
        # in ra log để xác nhận SSI có thực sự trả về dữ liệu của ĐÚNG NGÀY HÔM NAY hay không, hay
        # đang lặng lẽ trả về 1 ngày cũ hơn (foreign_flow_live == foreign_flow_close do live rỗng).
        "ohlc_date_debug": close_date_str,
        "live_date_debug": (
            foreign_flow_live.get("date").strftime("%Y-%m-%d %H:%M")
            if foreign_flow_live and foreign_flow_live.get("date") is not None and pd.notna(foreign_flow_live.get("date"))
            else None
        ),
        # SỬA 30/09/2026: ưu tiên khối lượng LUỸ KẾ THẬT nhận từ streaming (live_volume_override,
        # cập nhật liên tục trong phiên) — last["volume"] (OHLC) chỉ đáng tin khi KHÔNG có streaming,
        # vì OHLC REST cũng bị trễ ~1 ngày (xem ghi chú lớn ở get_foreign_flow_ssi).
        "volume": (
            live_volume_override if live_volume_override is not None
            else (float(last["volume"]) if pd.notna(last["volume"]) else None)
        ),
        # Giá trị giao dịch ước tính = giá × khối lượng (không có sẵn field "value" riêng từ nguồn
        # giá đang dùng — đây là cách tính chuẩn phổ biến, đủ chính xác để so sánh/sắp xếp).
        "trade_value": (
            price * live_volume_override if live_volume_override is not None
            else (price * float(last["volume"])) if pd.notna(last["volume"]) else None
        ),
        # --- Bộ dữ liệu "ĐÃ CHỐT" của phiên gần nhất — dùng RIÊNG cho lưu lịch sử (save_daily_history),
        # KHÔNG bị ảnh hưởng bởi giá "live" ở trên, để dashboard sau này luôn có đúng 1 điểm dữ liệu/
        # phiên, nhất quán dù bot chạy giờ nào trong ngày (xem ghi chú sửa 29/09/2026).
        "close_date": close_date_str,
        "close_price": close_price,
        "close_price_change_pct": ohlc_change_pct_debug,
        "close_volume": close_volume,
        "close_trade_value": (close_price * close_volume) if close_volume is not None else None,
        "foreign_net_val_close": foreign_flow_close["net_val"] if foreign_close_matches else None,
        "foreign_room_pct_close": foreign_flow_close["foreign_room_pct"] if foreign_close_matches else None,
        "rsi": rsi_rounded,
        "macd_diff": round(float(last["macd_diff"]), 2) if pd.notna(last["macd_diff"]) else None,
        "cci": round(float(last["cci"]), 1) if pd.notna(last["cci"]) else None,
        "volume_ratio": round(float(last["volume_ratio"]), 2) if pd.notna(last["volume_ratio"]) else None,
        "money_flow": money_flow,
        "money_flow_basis": money_flow_basis,
        "foreign_net_val": foreign_flow_live["net_val"] if foreign_flow_live else None,
        "foreign_room_pct": foreign_flow_live["foreign_room_pct"] if foreign_flow_live else None,
        "macd_cross_up": macd_cross,
        "macd_cross_down": macd_cross_down,
        "bullish_divergence": divergence,
        "pe": valuation["pe"],
        "pb": valuation["pb"],
        "undervalued": valuation["undervalued"],
        "week52_high": week52_high,
        "week52_low": week52_low,
        "pct_from_52w_high": round((price / week52_high - 1) * 100, 1) if week52_high else None,
        "pct_from_52w_low": round((price / week52_low - 1) * 100, 1) if week52_low else None,
        "overbought": bool(rsi_rounded is not None and rsi_rounded > OVERBOUGHT_RSI),
        "oversold": bool(rsi_rounded is not None and rsi_rounded < OVERSOLD_RSI),
        "bearish_divergence": bearish_divergence,
        "sector": (sector_map or {}).get(symbol, "Chưa phân loại"),
    }
    # Tín hiệu MUA: bắt buộc CẢ BA điều kiện kỹ thuật (RSI + MACD cắt lên + Phân kỳ dương).
    # Định giá rẻ (P/E, P/B) KHÔNG bắt buộc — chỉ là điểm cộng, tính trong signal_score.
    result["is_buy_signal"] = bool(
        result["rsi"] is not None and result["rsi"] < RSI_BUY_THRESHOLD and macd_cross and divergence
    )
    # Tín hiệu BÁN (đối xứng, chỉ áp dụng cho mã ưu tiên): RSI cao + MACD cắt xuống + Phân kỳ âm.
    result["is_sell_signal"] = bool(
        symbol in PRIORITY_TICKERS
        and result["rsi"] is not None and result["rsi"] > SELL_RSI_THRESHOLD
        and macd_cross_down and bearish_divergence
    )
    result["signal_score"] = sum([
        bool(result["rsi"] is not None and result["rsi"] < RSI_BUY_THRESHOLD),
        bool(macd_cross),
        bool(divergence),
        bool(result["undervalued"]),
    ])
    result["buy_zone"] = f"{price * 0.97:,.0f} - {price * 1.01:,.0f}"
    result["stop_loss"] = f"{price * (1 - STOP_LOSS_PCT):,.0f}"
    return result


# ========================= BÁO CÁO =========================
def _sector_valuation_verdict(pe, pb, sector_pe_avg, sector_pb_avg, sector_count):
    """So P/E, P/B của 1 mã với trung bình ngành (tính từ chính các mã đã quét trong lần chạy này)."""
    if sector_count < 3 or (sector_pe_avg is None and sector_pb_avg is None):
        return "chưa đủ mã cùng ngành trong danh mục để so sánh"
    signals = []
    if pe and sector_pe_avg:
        if pe < sector_pe_avg * 0.85:
            signals.append("re")
        elif pe > sector_pe_avg * 1.15:
            signals.append("dat")
        else:
            signals.append("ngang")
    if pb and sector_pb_avg:
        if pb < sector_pb_avg * 0.85:
            signals.append("re")
        elif pb > sector_pb_avg * 1.15:
            signals.append("dat")
        else:
            signals.append("ngang")
    if not signals:
        return "chưa đủ dữ liệu P/E, P/B để so sánh"
    if all(s == "re" for s in signals):
        return "RẺ hơn TB ngành ✅"
    if all(s == "dat" for s in signals):
        return "ĐẮT hơn TB ngành ⚠️"
    if all(s == "ngang" for s in signals):
        return "tương đương TB ngành"
    return "trái chiều so với ngành (P/E và P/B không đồng nhất)"


def compute_sector_stats(ok: list) -> dict:
    """Thống kê P/E, P/B trung bình + dòng tiền theo ngành (từ chính các mã đã quét trong lần chạy
    này — không tốn thêm request nào). Tách thành hàm riêng để build_report() và save_daily_history()
    (lưu lịch sử để vẽ biểu đồ xu hướng sau này) dùng chung, tránh tính 2 lần."""
    sector_stats = {}
    for r in ok:
        st = sector_stats.setdefault(r["sector"], {"pe_list": [], "pb_list": [], "inflow_count": 0, "net_val_sum": 0.0, "count": 0})
        st["count"] += 1
        if r["pe"]:
            st["pe_list"].append(r["pe"])
        if r["pb"]:
            st["pb_list"].append(r["pb"])
        if r["money_flow"] == "vao_manh":
            st["inflow_count"] += 1
            if r["money_flow_basis"] == "khoi_ngoai" and r["foreign_net_val"]:
                st["net_val_sum"] += r["foreign_net_val"]
    for st in sector_stats.values():
        st["pe_avg"] = sum(st["pe_list"]) / len(st["pe_list"]) if st["pe_list"] else None
        st["pb_avg"] = sum(st["pb_list"]) / len(st["pb_list"]) if st["pb_list"] else None
    return sector_stats


def compute_closing_sector_stats(ok: list) -> dict:
    """Giống compute_sector_stats() nhưng dùng bộ dữ liệu "ĐÃ CHỐT" của phiên gần nhất
    (foreign_net_val_close) thay vì dữ liệu "live" (foreign_net_val) — dùng RIÊNG cho lưu lịch sử
    (save_daily_history), để sector_flow_daily luôn phản ánh đúng dòng tiền của 1 phiên đã hoàn
    tất, không lẫn số liệu dòng tiền nửa-phiên tuỳ theo giờ bot chạy."""
    threshold = 2_000_000_000  # trùng ngưỡng dùng trong detect_money_flow()
    stats = {}
    for r in ok:
        st = stats.setdefault(r["sector"], {"pe_list": [], "pb_list": [], "inflow_count": 0, "net_val_sum": 0.0, "count": 0})
        st["count"] += 1
        if r["pe"]:
            st["pe_list"].append(r["pe"])
        if r["pb"]:
            st["pb_list"].append(r["pb"])
        net_val_close = r.get("foreign_net_val_close")
        if net_val_close is not None and net_val_close >= threshold:
            st["inflow_count"] += 1
            st["net_val_sum"] += net_val_close
    for st in stats.values():
        st["pe_avg"] = sum(st["pe_list"]) / len(st["pe_list"]) if st["pe_list"] else None
        st["pb_avg"] = sum(st["pb_list"]) / len(st["pb_list"]) if st["pb_list"] else None
    return stats


def build_report(results: list, macro: dict, market_breadth: dict = None, sector_stats: dict = None) -> str:
    now = now_vn().strftime("%H:%M %d/%m/%Y")
    ok = [r for r in results if r]
    buy_signals = sorted([r for r in ok if r["is_buy_signal"]], key=lambda x: x["signal_score"], reverse=True)
    # SỬA 28-29/09/2026: sắp theo GIÁ TRỊ giao dịch (giá × khối lượng, đơn vị VNĐ) thay vì
    # volume_ratio (1 tỉ lệ trừu tượng) hay khối lượng thuần (không phản ánh đúng quy mô dòng tiền
    # — 1 mã giá thấp khối lượng lớn có thể giá trị nhỏ hơn 1 mã giá cao khối lượng vừa phải).
    inflow = sorted([r for r in ok if r["money_flow"] == "vao_manh"], key=lambda x: x["trade_value"] or 0, reverse=True)

    up = sum(1 for r in ok if (r["price_change_pct"] or 0) > 0)
    down = sum(1 for r in ok if (r["price_change_pct"] or 0) < 0)
    unchanged = len(ok) - up - down

    if sector_stats is None:
        sector_stats = compute_sector_stats(ok)

    lines = [f"📊 BÁO CÁO PHÂN TÍCH {len(WATCHLIST)} MÃ — {now}", ""]

    if macro:
        trend_label = {"tang": "TĂNG 📈", "giam": "GIẢM 📉", "di_ngang": "ĐI NGANG ↔️"}[macro["trend"]]
        lines.append(f"🌏 VN-Index: {macro['price']:,.0f} | RSI {macro['rsi']} | Xu hướng: {trend_label}")
    lines.append(f"📐 Độ rộng thị trường (trong danh mục): {up} mã tăng / {down} mã giảm / {unchanged} đứng giá")
    if market_breadth:
        mb = market_breadth
        lines.append(f"🏛️ Độ rộng toàn sàn HOSE: {mb['advance']} mã tăng / {mb['decline']} mã giảm / {mb['steady']} đứng giá"
                     f" | Trần: {mb['ceiling']} | Sàn: {mb['floor']}")
        prop_net = mb["prop_buy_value"] - mb["prop_sell_value"]
        prop_label = "mua ròng" if prop_net >= 0 else "bán ròng"
        lines.append(f"🏦 Tự doanh CTCK toàn sàn: {prop_label} {abs(prop_net)/1_000_000_000:.1f} tỷ đ")
    lines.append(f"💡 Chiến lược giải ngân gợi ý: {suggest_strategy(macro, len(buy_signals))}")
    lines.append(f"⛔ Stop-loss áp dụng: -{int(STOP_LOSS_PCT*100)}% từ giá vào\n")

    lines.append(f"🔎 Tín hiệu MUA, xếp theo độ mạnh ({len(buy_signals)}/{len(ok)} mã đạt tiêu chí):")
    if buy_signals:
        for r in buy_signals:
            tags = []
            if r["macd_cross_up"]:
                tags.append("MACD cắt lên")
            if r["bullish_divergence"]:
                tags.append("Phân kỳ dương")
            if r["undervalued"]:
                tags.append("Định giá rẻ")
            chg = r["price_change_pct"]
            chg_str = f"{chg:+.1f}%" if chg is not None else "N/A"
            lines.append(f"✅ {r['ticker']} [{r['signal_score']}/4đ] ({r['sector']}): {r['price']:,.0f}đ ({chg_str}) | RSI {r['rsi']} | {', '.join(tags)}")
            lines.append(f"   📦 KL: {format_volume(r['volume'])} | 🎯 Vùng mua: {r['buy_zone']} | 🛑 Stop-loss: {r['stop_loss']}")
    else:
        lines.append("(Chưa có mã nào đạt đủ tiêu chí)")

    if inflow:
        basis_label = "khối ngoại mua ròng" if inflow[0]["money_flow_basis"] == "khoi_ngoai" else f"khối lượng > {VOLUME_SPIKE_RATIO}x TB20 phiên"
        lines.append(f"\n💰 Dòng tiền vào mạnh (theo {basis_label}, sắp theo giá trị giao dịch):")
        for r in inflow[:8]:
            chg = r["price_change_pct"]
            chg_str = f"{chg:+.1f}%" if chg is not None else "N/A"
            value_str = f"{r['trade_value']/1_000_000_000:.1f} tỷ đ" if r["trade_value"] else "N/A"
            if r["money_flow_basis"] == "khoi_ngoai" and r["foreign_net_val"] is not None:
                flow_str = f"NN mua ròng {r['foreign_net_val']/1_000_000_000:.1f} tỷ đ"
            else:
                flow_str = f"x{r['volume_ratio']} TB20"
            lines.append(f"🔸 {r['ticker']}: {r['price']:,.0f}đ ({chg_str}) | 💵 GT {value_str} | 📦 KL {format_volume(r['volume'])} | {flow_str}")

        # Ngành đang hút dòng tiền nhiều nhất (tính trên toàn bộ danh mục, không chỉ top 8 ở trên)
        sector_inflow = [(s, st) for s, st in sector_stats.items() if st["inflow_count"] > 0]
        if sector_inflow:
            has_val = any(st["net_val_sum"] for _, st in sector_inflow)
            sector_inflow.sort(key=lambda x: x[1]["net_val_sum"] if has_val else x[1]["inflow_count"], reverse=True)
            parts = []
            for s, st in sector_inflow[:5]:
                if has_val and st["net_val_sum"]:
                    parts.append(f"{s} ({st['net_val_sum']/1_000_000_000:.1f} tỷ)")
                else:
                    parts.append(f"{s} ({st['inflow_count']} mã)")
            lines.append(f"   🏭 Ngành hút dòng tiền mạnh nhất: {', '.join(parts)}")

    lines.append("\n📌 Mã ưu tiên theo dõi:")
    for r in ok:
        if r["ticker"] in PRIORITY_TICKERS:
            chg = r["price_change_pct"]
            chg_str = f"{chg:+.1f}%" if chg is not None else "N/A"
            st = sector_stats.get(r["sector"], {"pe_avg": None, "pb_avg": None, "count": 0})
            pe_str = f"P/E {r['pe']:.1f}" + (f" (TB ngành {st['pe_avg']:.1f})" if st["pe_avg"] else "") if r["pe"] else "P/E N/A"
            pb_str = f"P/B {r['pb']:.1f}" + (f" (TB ngành {st['pb_avg']:.1f})" if st["pb_avg"] else "") if r["pb"] else "P/B N/A"
            verdict = _sector_valuation_verdict(r["pe"], r["pb"], st["pe_avg"], st["pb_avg"], st["count"])
            lines.append(f"🔹 {r['ticker']} ({r['sector']}): {r['price']:,.0f}đ ({chg_str}) | 📦 KL {format_volume(r['volume'])}")
            lines.append(f"   📊 RSI {r['rsi']} | MACD diff {r['macd_diff']} | CCI {r['cci']}")
            lines.append(f"   💵 {pe_str} | {pb_str} → {verdict}")
            lines.append(f"   📈 52 tuần: cách đỉnh {r['pct_from_52w_high']:+.1f}% / cách đáy {r['pct_from_52w_low']:+.1f}%")
            if r["foreign_room_pct"] is not None:
                room_note = " ⚠️ (gần hết, khối ngoại khó mua thêm)" if r["foreign_room_pct"] < FOREIGN_ROOM_LOW_PCT else ""
                lines.append(f"   🌐 Room khối ngoại còn: {r['foreign_room_pct']:.1f}%{room_note}")
            if r["is_sell_signal"]:
                lines.append(f"   🔻 TÍN HIỆU BÁN: RSI {r['rsi']} > {SELL_RSI_THRESHOLD:.0f}, MACD cắt xuống, Phân kỳ âm")
            warnings = []
            if r["overbought"]:
                warnings.append(f"quá mua (RSI {r['rsi']} > {OVERBOUGHT_RSI:.0f})")
            if r["oversold"]:
                warnings.append(f"quá bán (RSI {r['rsi']} < {OVERSOLD_RSI:.0f})")
            if r["macd_cross_down"] and not r["is_sell_signal"]:
                warnings.append("MACD vừa cắt xuống — tín hiệu tăng yếu đi")
            if warnings:
                lines.append(f"   ⚠️ Cảnh báo: {'; '.join(warnings)}")
            lines.append("")

    failed = [t for t, r in zip(WATCHLIST, results) if r is None]
    if failed:
        lines.append(f"\n⚠️ Không lấy được dữ liệu: {', '.join(failed)}")

    lines.append("\n⚠️ Đây là công cụ phân tích kỹ thuật tự động theo quy tắc do bạn đặt ra, "
                  "không phải khuyến nghị đầu tư cá nhân hóa. Cân nhắc rủi ro trước khi giao dịch.")
    return "\n".join(lines)


# ========================= LỊCH SỬ THEO NGÀY (đẩy lên GitHub — nền tảng để vẽ biểu đồ/dashboard) =========================
def _github_headers():
    return {"Authorization": f"Bearer {GITHUB_TOKEN}", "Accept": "application/vnd.github+json"}


def _fetch_github_json(repo_path: str):
    """Lấy nội dung JSON hiện có trên GitHub (nếu có) + sha của file (cần để PUT ghi đè đúng
    phiên bản). Trả về (dict_du_lieu, sha) — dict_du_lieu = None nếu file chưa tồn tại hoặc lỗi."""
    api_url = f"https://api.github.com/repos/{GITHUB_REPO}/contents/{repo_path}"
    try:
        resp = requests.get(api_url, headers=_github_headers(), params={"ref": GITHUB_BRANCH}, timeout=20)
        if resp.status_code == 404:
            return None, None
        if resp.status_code != 200:
            log.warning("Không lấy được lịch sử cũ trên GitHub (%s): %s", resp.status_code, resp.text[:300])
            return None, None
        payload = resp.json()
        sha = payload.get("sha")
        content_b64 = payload.get("content", "")
        raw = base64.b64decode(content_b64).decode("utf-8")
        return json.loads(raw), sha
    except Exception as e:
        log.warning("Lỗi khi đọc lịch sử cũ trên GitHub: %s", e)
        return None, None


def _push_github_json(data: dict, repo_path: str, sha: str = None) -> bool:
    """Ghi đè (hoặc tạo mới) 1 file JSON trên GitHub bằng Contents API."""
    if not GITHUB_TOKEN or not GITHUB_REPO:
        log.info("Chưa cấu hình GITHUB_TOKEN/GITHUB_REPO — bỏ qua lưu lịch sử lên GitHub.")
        return False
    api_url = f"https://api.github.com/repos/{GITHUB_REPO}/contents/{repo_path}"
    content_b64 = base64.b64encode(json.dumps(data, ensure_ascii=False, default=str, indent=2).encode("utf-8")).decode("utf-8")
    body = {
        "message": f"Cập nhật lịch sử bot — {now_vn().strftime('%Y-%m-%d %H:%M')} (giờ VN)",
        "content": content_b64,
        "branch": GITHUB_BRANCH,
    }
    if sha:
        body["sha"] = sha
    try:
        resp = requests.put(api_url, headers=_github_headers(), json=body, timeout=30)
        if resp.status_code in (200, 201):
            return True
        log.error("Đẩy lịch sử lên GitHub thất bại (%s): %s", resp.status_code, resp.text[:300])
        return False
    except Exception as e:
        log.error("Lỗi khi đẩy lịch sử lên GitHub: %s", e)
        return False


def save_daily_history(results: list, macro: dict, market_breadth: dict, sector_stats: dict):
    """Gộp số liệu của PHIÊN GIAO DỊCH GẦN NHẤT ĐÃ CHỐT vào 1 file JSON DUY NHẤT trên GitHub
    (KHÔNG lưu bất kỳ file nào ở máy chạy bot) — KHÔNG tốn thêm request API chứng khoán nào ngoài
    dự kiến. Chạy đều đặn đủ lâu sẽ tích lũy thành dữ liệu theo thời gian, dùng để vẽ biểu đồ xu
    hướng / xây dashboard web (dòng tiền lớn theo ngành nhiều ngày liên tục, diễn biến từng mã,
    vĩ mô VN-Index...).

    SỬA NGÀY 29/09/2026 (tách "giá hiển thị Telegram" khỏi "giá lưu lịch sử"): bot của bạn không
    chạy theo lịch cố định — chạy giờ nào tuỳ lúc rảnh/nhớ, có thể là giữa giờ giao dịch. Nếu lưu
    thẳng giá "live" (dùng cho Telegram) vào lịch sử, dữ liệu 1 "ngày" trong file sẽ tuỳ thuộc vào
    đúng thời điểm bạn bấm chạy lần cuối trong ngày đó — không nhất quán, khó dùng cho dashboard.
    Nên giờ save_daily_history() dùng bộ dữ liệu "close_*" riêng trong mỗi kết quả phân tích
    (close_price, close_price_change_pct, close_volume, close_trade_value, foreign_net_val_close,
    foreign_room_pct_close — xem analyze_ticker()/get_foreign_flow_ssi()), luôn là dữ liệu của
    PHIÊN GẦN NHẤT ĐÃ CHỐT bất kể giờ chạy:
      - Chạy trong giờ giao dịch  -> "close_*" là dữ liệu phiên LIỀN TRƯỚC (hôm qua).
      - Chạy sau khi đóng cửa    -> "close_*" là dữ liệu phiên HÔM NAY (đã chốt xong).
    Mỗi dòng được lưu dưới đúng NGÀY CỦA PHIÊN đó (session_date, lấy từ close_date của các mã) —
    KHÔNG phải ngày bot chạy — nên nếu bạn chạy bot vào 11h nhưng phiên gần nhất đã chốt là hôm
    qua, dữ liệu sẽ được ghi vào đúng ngày hôm qua, không lẫn với dữ liệu hôm nay khi hôm nay chốt
    xong (chạy lại sau đó sẽ tự thêm 1 dòng MỚI cho hôm nay, không ghi đè lên hôm qua).

    Mỗi năm tự tách 1 file riêng trên GitHub theo mẫu HISTORY_GITHUB_PATH (mặc định
    "bot_history_{year}.json", ví dụ bot_history_2026.json, bot_history_2027.json...) — sang năm
    mới bot tự ghi vào file mới, KHÔNG đụng tới file của năm trước.

    Cấu trúc file JSON (mỗi file 1 năm), "date" luôn là ngày của PHIÊN (không phải ngày chạy bot):
    - macro_daily: 1 dòng/phiên — VN-Index, độ rộng thị trường, dòng tiền tự doanh toàn sàn.
    - sector_flow_daily: 1 dòng/ngành/phiên — dòng tiền khối ngoại ròng + định giá TB theo ngành.
    - tickers_daily: 1 dòng/mã/phiên cho TOÀN BỘ WATCHLIST (có cờ is_priority để dashboard vẫn
      phân biệt/nổi bật được mã ưu tiên) — giá, chỉ báo, định giá, dòng tiền, room khối ngoại.
    Nếu bot chạy nhiều lần và vẫn ra cùng 1 session_date (ví dụ chạy 2 lần trong cùng giờ giao dịch,
    phiên liền trước vẫn là cùng 1 ngày), dữ liệu ngày đó sẽ được GHI ĐÈ (không nhân đôi).
    Chỉ giữ lại HISTORY_MAX_DAYS phiên gần nhất trong file để đề phòng phình bất thường.
    """
    if not ENABLE_HISTORY_LOG:
        return
    if not GITHUB_TOKEN or not GITHUB_REPO:
        log.info("Chưa cấu hình GITHUB_TOKEN/GITHUB_REPO — bỏ qua lưu lịch sử lên GitHub.")
        return

    now = now_vn()
    ok = [r for r in results if r and r.get("close_date")]
    if not ok:
        log.warning("Không có mã nào xác định được ngày phiên đã chốt (close_date) — bỏ qua lưu lịch sử lần này.")
        return

    # session_date = ngày PHIÊN ĐÃ CHỐT đại diện cho cả lần lưu này — lấy theo ngày xuất hiện
    # NHIỀU NHẤT trong số các mã (thường tất cả các mã đều cùng 1 ngày; lấy theo số đông để chống
    # lệch nếu 1-2 mã bị trục trặc dữ liệu riêng lẻ).
    date_counts = {}
    for r in ok:
        date_counts[r["close_date"]] = date_counts.get(r["close_date"], 0) + 1
    session_date_str = max(date_counts.items(), key=lambda kv: kv[1])[0]
    session_date = datetime.strptime(session_date_str, "%Y-%m-%d").date()

    repo_path = HISTORY_GITHUB_PATH.format(year=session_date.year)
    ok = [r for r in ok if r["close_date"] == session_date_str]  # chỉ giữ đúng các mã khớp session_date

    data, sha = _fetch_github_json(repo_path)
    if not data or not isinstance(data, dict):
        data = {"macro_daily": [], "sector_flow_daily": [], "tickers_daily": []}

    macro_closing = get_macro_closing_for_date(session_date)
    if macro_closing is None:
        # Dự phòng: dùng macro "live" (dùng cho Telegram) nếu không lấy được đúng phiên đã chốt —
        # còn hơn bỏ trắng, dù có thể không khớp 100% session_date trong trường hợp hiếm gặp này.
        macro_closing = macro
        if macro_closing:
            log.debug("Không lấy được VN-Index đã chốt cho %s — tạm dùng macro hiện tại để lưu lịch sử.", session_date_str)

    macro_row = {
        "date": session_date_str,
        "vnindex_price": macro_closing["price"] if macro_closing else None,
        "vnindex_rsi": macro_closing["rsi"] if macro_closing else None,
        "vnindex_trend": macro_closing["trend"] if macro_closing else None,
        "breadth_up": sum(1 for r in ok if (r["close_price_change_pct"] or 0) > 0),
        "breadth_down": sum(1 for r in ok if (r["close_price_change_pct"] or 0) < 0),
        "hose_advance": market_breadth["advance"] if market_breadth else None,
        "hose_decline": market_breadth["decline"] if market_breadth else None,
        "hose_ceiling": market_breadth["ceiling"] if market_breadth else None,
        "hose_floor": market_breadth["floor"] if market_breadth else None,
        "prop_net_value": (market_breadth["prop_buy_value"] - market_breadth["prop_sell_value"]) if market_breadth else None,
    }

    closing_sector_stats = compute_closing_sector_stats(ok)
    sector_rows_today = []
    for sector, st in closing_sector_stats.items():
        sector_rows_today.append({
            "date": session_date_str, "sector": sector, "count": st["count"],
            "inflow_count": st["inflow_count"], "net_val_sum": st["net_val_sum"],
            "pe_avg": st.get("pe_avg"), "pb_avg": st.get("pb_avg"),
        })

    ticker_rows_today = []
    for r in ok:
        ticker_rows_today.append({
            "date": session_date_str, "ticker": r["ticker"], "sector": r["sector"],
            "is_priority": r["ticker"] in PRIORITY_TICKERS,
            "price": r["close_price"], "price_change_pct": r["close_price_change_pct"],
            "volume": r["close_volume"], "trade_value": r["close_trade_value"],
            "rsi": r["rsi"], "macd_diff": r["macd_diff"], "cci": r["cci"],
            "pe": r["pe"], "pb": r["pb"], "foreign_room_pct": r["foreign_room_pct_close"],
            "money_flow": r["money_flow"], "money_flow_basis": r["money_flow_basis"],
            "foreign_net_val": r["foreign_net_val_close"],
            "is_buy_signal": r["is_buy_signal"], "is_sell_signal": r["is_sell_signal"],
        })

    # Khử trùng: bỏ dữ liệu cũ của đúng session_date (phòng trường hợp bot chạy lại nhiều lần mà
    # phiên gần nhất đã chốt vẫn là cùng 1 ngày), rồi thêm dữ liệu mới vào, sau đó cắt bớt chỉ giữ
    # HISTORY_MAX_DAYS phiên gần nhất.
    data["macro_daily"] = [row for row in data.get("macro_daily", []) if row.get("date") != session_date_str]
    data["macro_daily"].append(macro_row)
    data["macro_daily"] = data["macro_daily"][-HISTORY_MAX_DAYS:]

    data["sector_flow_daily"] = [row for row in data.get("sector_flow_daily", []) if row.get("date") != session_date_str]
    data["sector_flow_daily"].extend(sector_rows_today)
    kept_dates = sorted(set(row["date"] for row in data["sector_flow_daily"]))[-HISTORY_MAX_DAYS:]
    data["sector_flow_daily"] = [row for row in data["sector_flow_daily"] if row["date"] in kept_dates]

    data["tickers_daily"] = [row for row in data.get("tickers_daily", []) if row.get("date") != session_date_str]
    data["tickers_daily"].extend(ticker_rows_today)
    kept_dates = sorted(set(row["date"] for row in data["tickers_daily"]))[-HISTORY_MAX_DAYS:]
    data["tickers_daily"] = [row for row in data["tickers_daily"] if row["date"] in kept_dates]

    data["_meta"] = {
        "year": session_date.year,
        "last_updated": now.isoformat(timespec="seconds"),
        "data_dictionary": {
            "macro_daily": "1 dòng/phiên (date = ngày phiên đã chốt, không phải ngày chạy bot): "
                            "vnindex_price/rsi/trend, breadth_up/down (trong danh mục theo dõi), "
                            "hose_advance/decline/ceiling/floor (toàn sàn HOSE), prop_net_value (tự doanh mua ròng, VNĐ).",
            "sector_flow_daily": "1 dòng/ngành/phiên: count (số mã), inflow_count (số mã dòng tiền vào mạnh), "
                                  "net_val_sum (tổng khối ngoại mua ròng theo ngành, VNĐ), pe_avg/pb_avg (định giá TB ngành).",
            "tickers_daily": "1 dòng/mã/phiên cho TOÀN BỘ watchlist (is_priority đánh dấu mã ưu tiên): price, "
                              "price_change_pct, volume, trade_value (giá x khối lượng, VNĐ) — TẤT CẢ là giá ĐÃ CHỐT "
                              "của phiên (date), không phải giá live tại thời điểm bot chạy; rsi/macd_diff/cci, "
                              "pe/pb, foreign_room_pct, money_flow (vao_manh/binh_thuong...), money_flow_basis "
                              "(khoi_ngoai/khoi_luong), foreign_net_val (khối ngoại mua/bán ròng, VNĐ, tính theo phiên "
                              "đã chốt), is_buy_signal, is_sell_signal.",
        },
    }

    if _push_github_json(data, repo_path, sha):
        log.info("✓ Đã lưu lịch sử phiên %s lên GitHub (%s) — chạy lúc %s.",
                  session_date_str, repo_path, now.strftime("%H:%M %d/%m/%Y"))


# ========================= BIỂU ĐỒ =========================
def generate_market_chart(ok_results: list, buy_signals: list):
    """Tạo ảnh biểu đồ (độ rộng thị trường + điểm tín hiệu MUA) trong bộ nhớ. Trả về BytesIO hoặc None nếu lỗi."""
    try:
        up = sum(1 for r in ok_results if (r["price_change_pct"] or 0) > 0)
        down = sum(1 for r in ok_results if (r["price_change_pct"] or 0) < 0)
        unchanged = len(ok_results) - up - down

        fig, axes = plt.subplots(1, 2, figsize=(10, 4.2))
        axes[0].bar(["Tăng", "Giảm", "Đứng giá"], [up, down, unchanged],
                    color=["#2e7d32", "#c62828", "#9e9e9e"])
        axes[0].set_title(f"Độ rộng thị trường ({len(ok_results)} mã)")

        top = sorted(buy_signals, key=lambda x: x["signal_score"], reverse=True)[:10]
        if top:
            tickers = [r["ticker"] for r in reversed(top)]
            scores = [r["signal_score"] for r in reversed(top)]
            axes[1].barh(tickers, scores, color="#1565c0")
            axes[1].set_title("Điểm tín hiệu MUA (thang 4)")
            axes[1].set_xlim(0, 4)
        else:
            axes[1].text(0.5, 0.5, "Không có tín hiệu MUA", ha="center", va="center")
            axes[1].set_axis_off()

        plt.tight_layout()
        buf = BytesIO()
        plt.savefig(buf, format="png", dpi=110)
        plt.close(fig)
        buf.seek(0)
        return buf
    except Exception as e:
        log.warning("Không tạo được biểu đồ: %s", e)
        return None


# ========================= GỬI THÔNG BÁO =========================
def send_telegram_photo(image_buf: BytesIO, caption: str = ""):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID or image_buf is None:
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendPhoto"
    try:
        files = {"photo": ("chart.png", image_buf, "image/png")}
        data = {"chat_id": TELEGRAM_CHAT_ID, "caption": caption[:1024]}
        resp = requests.post(url, data=data, files=files, timeout=20)
        if resp.status_code != 200:
            log.error("Gửi ảnh Telegram thất bại (%s): %s", resp.status_code, resp.text)
        else:
            log.info("Đã gửi biểu đồ qua Telegram.")
    except Exception as e:
        log.error("Lỗi khi gửi ảnh Telegram: %s", e)


def _split_message_by_lines(message: str, max_len: int = 3500) -> list:
    """Chia tin nhắn thành nhiều phần, luôn cắt ở cuối dòng — không cắt giữa câu."""
    lines = message.split("\n")
    chunks = []
    current = ""
    for line in lines:
        candidate = f"{current}\n{line}" if current else line
        if len(candidate) > max_len and current:
            chunks.append(current)
            current = line
        else:
            current = candidate
    if current:
        chunks.append(current)
    return chunks


def send_telegram(message: str):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        log.info("Chưa cấu hình Telegram — bỏ qua gửi Telegram.")
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    chunks = _split_message_by_lines(message, max_len=3500)
    for idx, chunk in enumerate(chunks, 1):
        prefix = f"[{idx}/{len(chunks)}]\n" if len(chunks) > 1 else ""
        resp = requests.post(url, data={"chat_id": TELEGRAM_CHAT_ID, "text": prefix + chunk}, timeout=15)
        if resp.status_code != 200:
            log.error("Gửi Telegram thất bại (%s): %s", resp.status_code, resp.text)
        time.sleep(0.5)
    log.info("Đã gửi báo cáo qua Telegram (%d phần).", len(chunks))


def send_email(subject: str, body: str):
    if not SMTP_HOST or not EMAIL_TO:
        log.info("Chưa cấu hình email — bỏ qua gửi email.")
        return
    try:
        msg = MIMEText(body, "plain", "utf-8")
        msg["Subject"] = subject
        msg["From"] = EMAIL_FROM
        msg["To"] = EMAIL_TO
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=20) as server:
            server.starttls()
            if SMTP_USER and SMTP_PASS:
                server.login(SMTP_USER, SMTP_PASS)
            server.sendmail(EMAIL_FROM, [EMAIL_TO], msg.as_string())
        log.info("Đã gửi báo cáo qua email tới %s.", EMAIL_TO)
    except Exception as e:
        log.error("Gửi email thất bại: %s", e)


# ========================= MAIN =========================
def main():
    log.info("Bắt đầu quét %d mã.", len(WATCHLIST))
    log.info("Nguồn dữ liệu giá/khối lượng: %s%s", RESOLVED_DATA_SOURCE.upper(),
              " (P/E, P/B và ngành vẫn lấy từ vnstock)" if RESOLVED_DATA_SOURCE == "ssi" else "")
    if ENABLE_VALUATION:
        log.info("Nguồn dữ liệu định giá (P/E, P/B) qua vnstock: %s (đổi bằng VALUATION_SOURCE trong .env)", VALUATION_SOURCE)
    if RESOLVED_DATA_SOURCE == "ssi" and not (SSI_API_KEY and SSI_API_SECRET):
        log.warning("DATA_SOURCE=ssi nhưng thiếu SSI_API_KEY/SSI_API_SECRET trong .env!")
    try:
        tier_info = vnai.get_tier_info()
        log.info("Tier vnstock hiện tại: %s | Giới hạn: %s req/phút",
                  tier_info.get("tier"), tier_info.get("limits", {}).get("per_minute"))
        if tier_info.get("tier") == "guest" and VNSTOCK_API_KEY:
            log.warning("Đã cấu hình VNSTOCK_API_KEY nhưng tier vẫn là 'guest' — key có thể không hợp lệ.")
        elif tier_info.get("tier") == "guest":
            log.info("Chưa cấu hình VNSTOCK_API_KEY trong .env — đang dùng giới hạn Guest (20 req/phút).")
    except Exception as e:
        log.debug("Không lấy được thông tin tier: %s", e)
    try:
        macro = get_macro_context()
    except SystemExit:
        log.warning("Đạt giới hạn tốc độ API khi lấy VN-Index. Bỏ qua phần vĩ mô lần này.")
        macro = None

    sector_map = get_sector_map()  # 1 lần gọi cho cả danh mục, {} nếu lỗi
    market_breadth = get_market_breadth_ssi()  # độ rộng toàn sàn + dòng tiền tự doanh, None nếu không dùng SSI/lỗi

    # SỬA 30/09/2026: mở streaming SSI 1 LẦN cho CẢ danh mục (không phải 1 request/mã như REST) —
    # xem ghi chú lớn ở ENABLE_SSI_STREAM và get_foreign_flow_ssi. Chỉ làm việc này khi đang THỰC SỰ
    # trong giờ giao dịch — ngoài giờ, REST "đã chốt" vốn đã đủ dùng và đáng tin (dữ liệu ổn định,
    # không còn "đang chạy" nữa), không cần mở kết nối streaming tốn thời gian vô ích.
    global _current_stream_snapshot
    if RESOLVED_DATA_SOURCE == "ssi" and ENABLE_FOREIGN_FLOW and ENABLE_SSI_STREAM and _is_vn_trading_hours(now_vn()):
        log.info("Đang mở kết nối Streaming SSI để lấy dữ liệu real-time (nghe %.0fs)...", SSI_STREAM_WAIT_SEC)
        _current_stream_snapshot = fetch_live_snapshot_ssi_stream(WATCHLIST, SSI_STREAM_WAIT_SEC)

    results = []
    for symbol in WATCHLIST:
        r = None
        for attempt in (1, 2):  # thử lại 1 lần nếu bị chặn rate limit
            try:
                r = analyze_ticker(symbol, sector_map=sector_map)
                log.info("%s: giá %.0f (nguồn: %s, OHLC gốc: %.0f, ngày OHLC: %s, ngày dữ liệu live: %s) "
                         "| RSI %s | dòng tiền %s",
                         symbol, r["price"], r["price_source"], r["ohlc_price_debug"],
                         r["ohlc_date_debug"], r["live_date_debug"], r["rsi"], r["money_flow"])
                break
            except SystemExit as e:
                # vnstock/vnai chủ động gọi sys.exit() khi vượt rate limit — đây KHÔNG
                # phải lỗi thường, nên phải bắt riêng SystemExit (except Exception không bắt được).
                log.warning("Đạt giới hạn tốc độ API (vnstock) khi xử lý %s (%s). Chờ %ds rồi thử lại...",
                            symbol, e, RATE_LIMIT_WAIT_SEC)
                time.sleep(RATE_LIMIT_WAIT_SEC)
            except SSIRateLimitError as e:
                # Rate limit THẬT SỰ từ SSI FastConnect (khác với SystemExit của vnstock/vnai ở trên).
                # SDK trả kèm retry_after — dùng đúng thời gian server khuyến nghị thay vì đoán.
                wait = getattr(e, "retry_after", None) or RATE_LIMIT_WAIT_SEC
                log.warning("SSI báo rate limit khi xử lý %s — chờ %.0fs (theo retry_after) rồi thử lại...",
                            symbol, wait)
                time.sleep(wait)
            except Exception as e:
                log.error("Lỗi khi phân tích %s: %s", symbol, e)
                break
        results.append(r)
        time.sleep(REQUEST_DELAY_SEC)

    ok_results = [r for r in results if r]
    sector_stats = compute_sector_stats(ok_results)  # tính 1 lần, dùng chung cho báo cáo + lưu lịch sử
    report = build_report(results, macro, market_breadth, sector_stats=sector_stats)

    if SEND_CHART:
        buy_signals = [r for r in ok_results if r["is_buy_signal"]]
        chart = generate_market_chart(ok_results, buy_signals)
        send_telegram_photo(chart, caption="📊 Tổng quan thị trường & tín hiệu MUA")

    send_telegram(report)
    send_email(f"Báo cáo phân tích cổ phiếu {now_vn().strftime('%H:%M %d/%m/%Y')}", report)
    save_daily_history(results, macro, market_breadth, sector_stats)
    _save_valuation_cache()
    log.info("Hoàn tất.")


if __name__ == "__main__":
    main()
