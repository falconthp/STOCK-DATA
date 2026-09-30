"""
ssi_data_export.py
=====================================
Script XUẤT DỮ LIỆU RIÊNG — KHÔNG liên quan đến stock_analysis_bot.py, không gửi
Telegram/email. Chỉ lấy toàn bộ dữ liệu liên quan từ SSI FastConnect Data cho danh
sách mã của bạn, lưu ra file CSV để bạn tự đẩy lên GitHub và cho AI khác (Gemini...)
đọc phân tích qua link.
Dùng lại đúng SSI_API_KEY / SSI_API_SECRET / SSI_CLIENT_ID trong file .env đã có sẵn
(cùng thư mục với stock_analysis_bot.py) — không cần tạo lại.
Cài đặt (nếu chưa có): pip install -r requirements.txt
Chạy: py ssi_data_export.py
--------------------------------------------------------------------------------
SỬA NGÀY 28/09/2026 (đọc kỹ docs chính thức của SSI FastConnect Python SDK, đồng bộ với
các sửa đổi đã áp dụng cho stock_analysis_bot.py):
  1. get_client() giờ cấu hình rõ timeout/max_retries/retry_delay cho SSIConfig (qua .env dùng
     chung: SSI_TIMEOUT_SEC, SSI_MAX_RETRIES, SSI_RETRY_DELAY) — mặc định của ssi-sdk khá "hào
     phóng" (timeout=60s, max_retries=5 với backoff tăng dần 2,4,8,16,32s...), nghĩa là 1 request
     bị treo thật có thể khiến cả script đứng hình vài phút cho đúng 1 mã.
  2. export_securities_info() giờ gọi get_securities_info_by_board() theo TỪNG SÀN (HOSE/HNX/UPCOM
     — chỉ 3 request) thay vì gọi get_securities_info() RIÊNG CHO TỪNG MÃ (64 request trước đây)
     rồi lọc lại đúng danh sách ALL_SYMBOLS — giảm số request ~20 lần, nhanh hơn đáng kể.
  3. _call_with_retry() giờ ưu tiên dùng đúng retry_after mà SSI trả về trong RateLimitError (nếu
     có) thay vì luôn chờ cố định `backoff` giây — chờ đúng theo khuyến nghị của server.
--------------------------------------------------------------------------------
"""
import os
import sys
import time
import json
import base64
import logging
import re
from datetime import datetime, timedelta
import pandas as pd
import requests
from dotenv import load_dotenv
try:
    from ssi_sdk import Config as SSIConfig, Auth as SSIAuth, Data as SSIData
    from ssi_sdk.exceptions import RateLimitError
    from ssi_sdk.enums import Board
except ImportError:
    print("Thiếu thư viện ssi-sdk. Chạy: pip install -r requirements.txt")
    sys.exit(1)
load_dotenv()
# ========================= CẤU HÌNH =========================
SSI_API_KEY = os.getenv("SSI_API_KEY", "").strip()
SSI_API_SECRET = os.getenv("SSI_API_SECRET", "").strip()
SSI_CLIENT_ID = os.getenv("SSI_CLIENT_ID", "").strip()
if not SSI_API_KEY or not SSI_API_SECRET:
    print("❌ Thiếu SSI_API_KEY / SSI_API_SECRET trong .env — không thể xuất dữ liệu.")
    print("   Dùng đúng file .env đã cấu hình cho stock_analysis_bot.py.")
    sys.exit(1)
DEFAULT_WATCHLIST = (
    "VPB,VTP,VGC,VHC,ACB,VND,VCG,TCX,STB,SIP,PVT,REE,KBC,PVD,NLG,NT2,PAN,PC1,"
    "MBS,MSH,MSN,HDB,HHV,GVR,DPG,DPM,DXG,DDV,BMP,CTR,BSR,BVH,CTG,BID,DCM,DGW,"
    "FPT,GAS,GEX,GMD,HAH,HCM,KDH,MBB,MWG,PHR,PNJ,POW,HPG,IDC,PVS,SAB,SSI,TCB,"
    "VCB,VHM,VIC,VJC,VCI,VNM"
)
# Dùng lại đúng WATCHLIST trong .env nếu có (60 mã bạn đang quét)
BASE_WATCHLIST = [t.strip().upper() for t in os.getenv("WATCHLIST", DEFAULT_WATCHLIST).split(",") if t.strip()]
# Danh sách mã BỔ SUNG riêng cho việc export (không ảnh hưởng bot chính) — điền vào .env
EXTRA_SYMBOLS = [t.strip().upper() for t in os.getenv("EXPORT_EXTRA_SYMBOLS", "").split(",") if t.strip()]
ALL_SYMBOLS = sorted(set(BASE_WATCHLIST) | set(EXTRA_SYMBOLS))
EXPORT_HISTORY_DAYS = int(os.getenv("EXPORT_HISTORY_DAYS", "260"))
EXPORT_BREADTH_DAYS = int(os.getenv("EXPORT_BREADTH_DAYS", "30"))
EXPORT_REQUEST_DELAY_SEC = float(os.getenv("EXPORT_REQUEST_DELAY_SEC", "0.05"))
OUTPUT_DIR = os.getenv("EXPORT_OUTPUT_DIR", "ssi_export")
# Cấu hình SSI Config — dùng chung biến .env với stock_analysis_bot.py (nếu đã có sẵn thì tự dùng
# lại đúng giá trị đó, không cần khai báo lại). Xem ghi chú sửa ngày 28/09/2026 ở đầu file.
SSI_TIMEOUT_SEC = int(os.getenv("SSI_TIMEOUT_SEC", "15"))
SSI_MAX_RETRIES = int(os.getenv("SSI_MAX_RETRIES", "3"))
SSI_RETRY_DELAY = float(os.getenv("SSI_RETRY_DELAY", "1.0"))
# ---- Đẩy tự động lên GitHub (tùy chọn) — dùng Personal Access Token, KHÔNG phải mật khẩu GitHub ----
AUTO_PUSH_GITHUB = os.getenv("AUTO_PUSH_GITHUB", "false").lower() == "true"
GITHUB_TOKEN = os.getenv("GITHUB_TOKEN", "").strip()
GITHUB_REPO = os.getenv("GITHUB_REPO", "").strip()  # dạng "tenuser/tenrepo"
GITHUB_BRANCH = os.getenv("GITHUB_BRANCH", "main").strip()
GITHUB_FILE_PATH = os.getenv("GITHUB_FILE_PATH", "all_data.json").strip()
GEMINI_FILE_PATH = os.getenv("GEMINI_FILE_PATH", "gemini_data.json").strip()
GEMINI_SUMMARY_DAYS = int(os.getenv("GEMINI_SUMMARY_DAYS", "20"))  # chỉ giữ N phiên gần nhất/mã cho file gọn
# ---- Gemini API (tùy chọn) — tự phân tích qua Files API (upload thẳng file), KHÔNG cần mở
# Gemini/import tay. SỬA 30/09/2026: trước dùng url_context (đưa link cho Gemini tự tải) — không
# ổn định với link raw.githubusercontent.com, đổi sang upload file trực tiếp cho chắc chắn.
ENABLE_GEMINI_ANALYSIS = os.getenv("ENABLE_GEMINI_ANALYSIS", "false").lower() == "true"
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "").strip()
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-3.5-flash-lite").strip()
GEMINI_PROMPT = os.getenv("GEMINI_PROMPT", (
    "Bạn là một người bạn am hiểu chứng khoán, đang nhắn tin nhanh cho bạn thân về tình hình thị "
    "trường hôm nay — KHÔNG PHẢI viết báo cáo phân tích trang trọng kiểu chuyên gia/ngân hàng đầu tư. "
    "Dựa vào dữ liệu JSON trong file đính kèm, hãy PHÂN TÍCH VÀ ĐƯA RA NHẬN ĐỊNH/KHUYẾN NGHỊ CỤ THỂ "
    "bằng tiếng Việt — đây là yêu cầu QUAN TRỌNG NHẤT, không được chỉ liệt kê số liệu thô mà không có "
    "đánh giá đi kèm. Gồm 4 phần:\n"
    "1) Các mã có tín hiệu mua tốt dựa trên xu hướng giá và dòng tiền khối ngoại (securities_summary) "
    "— với MỖI mã nêu ra, sau khi liệt kê số liệu PHẢI có thêm 1 câu NHẬN ĐỊNH riêng giải thích TẠI "
    "SAO đây là tín hiệu tốt (ví dụ: tăng giá kèm khối ngoại mua ròng liên tục nhiều phiên là dấu "
    "hiệu dòng tiền lớn đang gom hàng, khác với tăng giá đơn thuần do đầu cơ ngắn hạn);\n"
    "2) Mã nào có room khối ngoại (remain_foreign_room/total_foreign_room) gần hết — kèm nhận định "
    "room cạn có ý nghĩa gì với khả năng khối ngoại mua thêm/định giá mã đó;\n"
    "3) Nhận định xu hướng chung dựa trên market_index_summary (độ rộng toàn sàn, dòng tiền tự doanh, "
    "biến động VN-Index) — thị trường đang tích cực/tiêu cực/giằng co, dòng tiền tự doanh đang mua "
    "ròng hay bán ròng và nói lên điều gì;\n"
    "4) BẮT BUỘC kết thúc bằng 1 đoạn 'KẾT LUẬN' ngắn (3-5 câu): tổng hợp lại toàn bộ 3 phần trên "
    "thành 1 nhận định chung về trạng thái thị trường hôm nay và gợi ý chiến lược giải ngân/quan sát "
    "phù hợp (ví dụ: nên giải ngân thăm dò, nên đứng ngoài quan sát, nên chốt lời một phần...). Đây "
    "KHÔNG phải khuyến nghị đầu tư cá nhân hóa, chỉ là góc nhìn tham khảo dựa trên dữ liệu.\n"
    "\n\nYÊU CẦU BẮT BUỘC VỀ NGÔN NGỮ VÀ ĐỊNH DẠNG (vì nội dung này gửi qua Telegram dạng chữ thường):\n"
    "- VĂN PHONG: viết ĐỜI THƯỜNG, BÌNH DÂN, như đang nhắn tin cho bạn bè — TUYỆT ĐỐI TRÁNH các cụm từ "
    "sáo rỗng kiểu báo cáo tài chính/chuyên gia phân tích như 'phản ánh lực cầu chủ động', 'tích lũy "
    "mạnh tay cho mục tiêu trung hạn', 'cấu trúc xu hướng chưa bị phá vỡ', 'dòng vốn tổ chức quốc tế "
    "đánh giá cao vùng định giá'... Thay vào đó nói thẳng, ngắn gọn, dễ hiểu như đang giải thích miệng: "
    "ví dụ 'giá tăng mà khối ngoại vẫn gom đều — có vẻ dòng tiền lớn đang mua thật, không phải lướt "
    "sóng' thay vì câu chữ hoa mỹ dài dòng.\n"
    "- MỖI câu Nhận định chỉ 1 câu NGẮN (dưới 25 từ), đi thẳng vào ý, không vòng vo.\n"
    "- Viết tiếng Việt CÓ DẤU ĐẦY ĐỦ, đúng chính tả (ví dụ 'tín hiệu mua tốt', KHÔNG viết 'tin hieu mua tot'). "
    "TUYỆT ĐỐI không được bỏ dấu tiếng Việt dưới bất kỳ hình thức nào.\n"
    "- TUYỆT ĐỐI KHÔNG dùng cú pháp Markdown (không #, ##, không **chữ đậm**, không gạch ngang ---) "
    "và KHÔNG dùng công thức LaTeX (không $$...$$). Chỉ dùng chữ thường, xuống dòng, và emoji "
    "(ví dụ 📈 📉 ⚠️ ✅) để phân đoạn.\n"
    "- TUYỆT ĐỐI KHÔNG đánh số thứ tự hay chữ cái trước mỗi mã/mục (KHÔNG viết '1.', 'a,', 'b,', '-', "
    "'*' ở đầu dòng). Bắt đầu mỗi mã trực tiếp bằng tên mã, ví dụ đúng: 'VCH: tăng 3,25%...' — KHÔNG "
    "viết 'a, VCH' hay '1) VCH'.\n"
    "- Mã cổ phiếu LUÔN viết IN HOA (ví dụ VCH, BSR, VTP) — KHÔNG viết thường (vch, bsr, vtp).\n"
    "- Với MỖI mã, trình bày mỗi số liệu trên MỘT DÒNG RIÊNG, có emoji nhỏ đầu dòng để dễ quét mắt "
    "(💵 cho giá, 💰 cho khối ngoại mua/bán ròng, 🌐 cho room khối ngoại) — KHÔNG viết thành đoạn văn "
    "dài dồn nhiều số liệu vào 1 câu. Ví dụ đúng:\n"
    "  VCH\n"
    "  💵 Giá: 146.000đ (+0,48%)\n"
    "  💰 Khối ngoại mua ròng: 2,6 tỷ đồng\n"
    "  👉 Mua ròng 3 phiên liền, giá vẫn tăng nhẹ — có vẻ đang gom, không phải xả.\n"
    "- Số tiền từ 1 triệu đồng trở lên PHẢI đổi sang đơn vị 'triệu đồng' hoặc 'tỷ đồng' (ví dụ 2807340000 "
    "phải viết là '2,8 tỷ đồng'), TUYỆT ĐỐI không viết số dài nguyên như 2807340000. Giá cổ phiếu và số "
    "cổ phiếu dùng dấu chấm phân cách nghìn (ví dụ 146.000đ, 19.200 cổ phiếu).\n"
    "- Mỗi phần chỉ nêu tối đa 3-4 mã tiêu biểu nhất, không cần liệt kê hết toàn bộ danh sách.\n"
    "- Phần KẾT LUẬN cũng viết ngắn gọn, đời thường, tối đa 3 câu — không lặp lại số liệu đã nêu ở "
    "trên, chỉ chốt lại 1 câu về xu hướng chung + 1 câu gợi ý nên làm gì.\n"
    "- Với mỗi mã nêu ra, PHẢI trích số liệu CỤ THỂ thật lấy đúng từ file dữ liệu — không suy diễn hay "
    "dùng kiến thức chung về mã đó nếu số liệu không có trong file. Câu NHẬN ĐỊNH thì được phép suy luận "
    "hợp lý dựa trên số liệu đó, nhưng phải bám sát dữ liệu thật, không bịa thêm thông tin ngoài file. "
    "Nếu một phần không có dữ liệu phù hợp, chỉ cần viết ngắn gọn 'không có dữ liệu phần này', không "
    "giải thích dài dòng."
)).strip()
# Gửi kết quả Gemini qua Telegram — dùng lại đúng TELEGRAM_BOT_TOKEN/CHAT_ID trong .env (nếu có)
SEND_GEMINI_TO_TELEGRAM = os.getenv("SEND_GEMINI_TO_TELEGRAM", "false").lower() == "true"
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)
_client_state = {"client": None}
def _call_with_retry(fn, *args, retries=2, backoff=3.0, **kwargs):
    """Gọi API, tự chờ và thử lại nếu bị chặn tốc độ (RateLimitError) — an toàn để giảm thời gian
    nghỉ chủ động mà không sợ mất dữ liệu khi lâu lâu vượt giới hạn. Ưu tiên dùng đúng retry_after
    mà SSI trả về (nếu có) thay vì luôn chờ cố định `backoff` giây."""
    for attempt in range(retries + 1):
        try:
            return fn(*args, **kwargs)
        except RateLimitError as e:
            if attempt < retries:
                wait = getattr(e, "retry_after", None) or backoff
                log.warning("Bị giới hạn tốc độ API, chờ %.0fs rồi thử lại (%d/%d)...", wait, attempt + 1, retries)
                time.sleep(wait)
            else:
                raise
def get_client():
    if _client_state["client"] is not None:
        return _client_state["client"]
    cfg = SSIConfig(
        client_id=SSI_CLIENT_ID, api_key=SSI_API_KEY, api_secret=SSI_API_SECRET,
        timeout=SSI_TIMEOUT_SEC, max_retries=SSI_MAX_RETRIES, retry_delay=SSI_RETRY_DELAY,
    )
    auth = SSIAuth(cfg)
    auth.authenticate()  # không cần OTP — chỉ đọc dữ liệu
    _client_state["client"] = SSIData(auth)
    return _client_state["client"]
# ========================= TỪNG LOẠI DỮ LIỆU =========================
def export_ohlc(client, symbols, from_date, to_date) -> pd.DataFrame:
    """Giá + khối lượng lịch sử (OHLCV) — dùng để tính RSI/MACD/CCI, vẽ biểu đồ."""
    rows = []
    for sym in symbols:
        try:
            bars = _call_with_retry(
                client.market_data.get_ohlc_1day_historical,
                sym, from_date.strftime("%Y/%m/%d 00:00:00"), to_date.strftime("%Y/%m/%d 23:59:59"), size=1000
            )
            for b in bars:
                rows.append({
                    "symbol": sym, "date": b.trading_date, "open": b.open_price, "high": b.high_price,
                    "low": b.low_price, "close": b.close_price, "volume": b.volume, "value": b.value,
                })
            log.info("OHLC %s: %d phiên", sym, len(bars))
        except Exception as e:
            log.warning("Lỗi OHLC %s: %s", sym, e)
        time.sleep(EXPORT_REQUEST_DELAY_SEC)
    return pd.DataFrame(rows)
def export_securities_summary(client, symbols, from_date, to_date) -> pd.DataFrame:
    """Biến động giá, khớp lệnh, dòng tiền khối ngoại, room khối ngoại — theo từng phiên."""
    rows = []
    for sym in symbols:
        try:
            items = _call_with_retry(
                client.market_data.get_securities_summary_historical,
                sym, from_date.strftime("%Y/%m/%d"), to_date.strftime("%Y/%m/%d")
            )
            for it in items:
                rows.append({
                    "symbol": sym, "date": it.trading_date,
                    "price_change": it.price_change, "price_change_percent": it.price_change_percent,
                    "open": it.open_price, "high": it.high_price, "low": it.low_price,
                    "close": it.close_price, "average_price": it.average_price,
                    "total_match_vol": it.total_match, "total_match_value": it.total_match_value,
                    "total_deal_vol": it.total_deal, "total_deal_value": it.total_deal_value,
                    "foreign_buy_vol": it.total_foreign_buy, "foreign_buy_value": it.total_foreign_buy_value,
                    "foreign_sell_vol": it.total_foreign_sell, "foreign_sell_value": it.total_foreign_sell_value,
                    "remain_foreign_room": it.remain_foreign_room, "total_foreign_room": it.total_foreign_room,
                })
            log.info("Summary %s: %d phiên", sym, len(items))
        except Exception as e:
            log.warning("Lỗi securities summary %s: %s", sym, e)
        time.sleep(EXPORT_REQUEST_DELAY_SEC)
    return pd.DataFrame(rows)
def export_securities_info(client, symbols) -> pd.DataFrame:
    """Thông tin cơ bản, tĩnh: tên công ty, sàn, ngành ICB, số cổ phiếu lưu hành.
    SỬA 28/09/2026: trước đây gọi get_securities_info(symbol) RIÊNG CHO TỪNG MÃ — với 64 mã là
    64 request. Giờ gọi get_securities_info_by_board() theo TỪNG SÀN (chỉ 3 request: HOSE, HNX,
    UPCOM — SSI trả về TOÀN BỘ mã trên sàn đó trong 1 lần gọi) rồi lọc lại đúng danh sách `symbols`
    — giảm số request đi ~20 lần, nhanh hơn đáng kể mà kết quả giống hệt."""
    wanted = set(symbols)
    rows = []
    seen = set()
    for board in (Board.HOSE, Board.HNX, Board.UPCOM):
        try:
            infos = _call_with_retry(client.market_data.get_securities_info_by_board, board)
            log.info("securities_info sàn %s: %d mã (lọc lại còn đúng danh sách bạn cần)", board, len(infos or []))
        except Exception as e:
            log.warning("Lỗi securities_info_by_board %s: %s", board, e)
            continue
        for info in infos or []:
            sym = getattr(info, "symbol", None)
            if not sym or sym not in wanted or sym in seen:
                continue
            seen.add(sym)
            rows.append({
                "symbol": info.symbol, "name_vi": info.symbol_name_vi, "name_en": info.symbol_name_en,
                "board": info.board, "icb_code": info.icb_code, "icb_name": info.icb_name,
                "listed_shares": info.listed_shares,
                "first_trading_date": info.first_trading_date, "last_trading_date": info.last_trading_date,
            })
        time.sleep(EXPORT_REQUEST_DELAY_SEC)
    missing = wanted - seen
    if missing:
        log.warning("Không tìm thấy securities_info cho %d mã (không thuộc HOSE/HNX/UPCOM?): %s",
                     len(missing), ", ".join(sorted(missing)))
    return pd.DataFrame(rows)
def export_market_index_summary(client, days: int) -> pd.DataFrame:
    """Độ rộng toàn sàn HOSE + dòng tiền tự doanh, từng ngày (chỉ index có, KHÔNG có API lấy nguyên khoảng — phải lặp)."""
    rows = []
    today = datetime.now()
    for i in range(days):
        d = today - timedelta(days=i)
        try:
            s = _call_with_retry(client.market_data.get_index_summary_historical, "VNINDEX", d.strftime("%Y/%m/%d"))
            if s:
                rows.append({
                    "date": s.trading_date, "index_value": s.index_value,
                    "index_change": s.index_change, "index_change_percent": s.index_change_percent,
                    "advance": s.total_advance_stock, "decline": s.total_decline_stock, "steady": s.total_steady_stock,
                    "ceiling": s.total_ceiling_stock, "floor": s.total_floor_stock,
                    "prop_buy_value": s.total_prop_buy_value, "prop_sell_value": s.total_prop_sell_value,
                    "total_match_value": s.total_match_value, "total_deal_value": s.total_deal_value,
                })
        except Exception as e:
            log.debug("Không có dữ liệu VN-Index ngày %s: %s", d.strftime("%Y/%m/%d"), e)
        time.sleep(EXPORT_REQUEST_DELAY_SEC)
    return pd.DataFrame(rows)
# ========================= GỘP 1 FILE + ĐẨY GITHUB =========================
def _clean_for_json(df: pd.DataFrame) -> list:
    """Đổi NaN -> None để ra JSON hợp lệ (JSON chuẩn không có NaN)."""
    if df is None or df.empty:
        return []
    return df.astype(object).where(pd.notnull(df), None).to_dict(orient="records")
def build_gemini_payload(df_summary, df_info, df_breadth) -> dict:
    """File RIÊNG, GỌN HƠN dành cho Gemini — bỏ ohlc_history VÀ chỉ giữ GEMINI_SUMMARY_DAYS phiên
    gần nhất/mã trong securities_summary (thay vì cả EXPORT_HISTORY_DAYS ngày). securities_summary
    đầy đủ cũng nhiều dòng gần bằng ohlc_history (16 cột/dòng), nên chỉ bỏ ohlc_history là chưa đủ
    gọn — phải cắt luôn số ngày. Giữ file all_data.json đầy đủ cho các nhu cầu khác."""
    df_recent = df_summary
    if df_summary is not None and not df_summary.empty and "symbol" in df_summary.columns and "date" in df_summary.columns:
        df_recent = (
            df_summary.sort_values(["symbol", "date"])
            .groupby("symbol", group_keys=False)
            .tail(GEMINI_SUMMARY_DAYS)
        )
    return {
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "symbols": ALL_SYMBOLS,
        "data_dictionary": {
            "securities_summary": f"Biến động giá, % thay đổi, dòng tiền + room khối ngoại — CHỈ {GEMINI_SUMMARY_DAYS} PHIÊN GẦN NHẤT/mã (foreign_buy/sell_vol/value, remain_foreign_room, total_foreign_room, price_change_percent)",
            "securities_info": "Thông tin cơ bản (tĩnh): tên công ty, sàn, ngành ICB",
            "market_index_summary": "Độ rộng toàn sàn HOSE + dòng tiền tự doanh theo ngày, chỉ số VN-Index",
        },
        "note": (f"Dữ liệu thô từ SSI FastConnect Data, không phải khuyến nghị đầu tư. File này ĐÃ RÚT GỌN "
                 f"(bỏ lịch sử giá chi tiết, chỉ giữ {GEMINI_SUMMARY_DAYS} phiên gần nhất/mã) để đảm bảo AI "
                 f"đọc được TOÀN BỘ nội dung — không bị cắt giữa file."),
        "securities_summary": _clean_for_json(df_recent),
        "securities_info": _clean_for_json(df_info),
        "market_index_summary": _clean_for_json(df_breadth),
    }
def build_combined_data(df_ohlc, df_summary, df_info, df_breadth) -> dict:
    """Gộp cả 4 bộ dữ liệu vào 1 dict — xuất ra 1 file JSON duy nhất, 1 link Raw duy nhất.
    KHÔNG có master_data (ceiling/floor) — SSI không có API lấy riêng theo từng mã cho loại này,
    buộc phải kéo toàn sàn nên đã bỏ theo yêu cầu chỉ lấy đúng mã bạn cần."""
    return {
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "symbols": ALL_SYMBOLS,
        "data_dictionary": {
            "ohlc_history": "Giá & khối lượng lịch sử theo phiên: symbol, date, open, high, low, close, volume, value",
            "securities_summary": "Biến động giá, dòng tiền + room khối ngoại theo phiên (foreign_buy/sell_vol/value, remain_foreign_room, total_foreign_room)",
            "securities_info": "Thông tin cơ bản (tĩnh): tên công ty, sàn, ngành ICB, số cổ phiếu lưu hành",
            "market_index_summary": "Độ rộng toàn sàn HOSE (advance/decline/steady/ceiling/floor) + dòng tiền tự doanh (prop_buy/sell_value) theo ngày, chỉ số VN-Index — LƯU Ý: đây là dữ liệu vĩ mô/toàn sàn có chủ đích, không phải dữ liệu riêng mã",
        },
        "note": "Đây là dữ liệu thô từ SSI FastConnect Data, không phải khuyến nghị đầu tư. Dùng để AI phân tích tham khảo.",
        "ohlc_history": _clean_for_json(df_ohlc),
        "securities_summary": _clean_for_json(df_summary),
        "securities_info": _clean_for_json(df_info),
        "market_index_summary": _clean_for_json(df_breadth),
    }
def push_to_github(local_path: str, repo_path: str):
    """Đẩy 1 file lên GitHub qua Contents API (tự tạo mới hoặc cập nhật đè). Trả về link Raw, None nếu lỗi/chưa cấu hình."""
    if not GITHUB_TOKEN or not GITHUB_REPO:
        log.info("Chưa cấu hình GITHUB_TOKEN/GITHUB_REPO — bỏ qua đẩy GitHub tự động.")
        return None
    api_url = f"https://api.github.com/repos/{GITHUB_REPO}/contents/{repo_path}"
    headers = {"Authorization": f"Bearer {GITHUB_TOKEN}", "Accept": "application/vnd.github+json"}
    with open(local_path, "rb") as f:
        content_b64 = base64.b64encode(f.read()).decode("utf-8")
    sha = None
    try:
        resp = requests.get(api_url, headers=headers, params={"ref": GITHUB_BRANCH}, timeout=20)
        if resp.status_code == 200:
            sha = resp.json().get("sha")
    except Exception as e:
        log.debug("Không kiểm tra được file cũ trên GitHub (có thể là lần đầu tạo): %s", e)
    payload = {
        "message": f"Cập nhật dữ liệu SSI — {datetime.now().strftime('%Y-%m-%d %H:%M')}",
        "content": content_b64,
        "branch": GITHUB_BRANCH,
    }
    if sha:
        payload["sha"] = sha  # bắt buộc phải có nếu file đã tồn tại, để GitHub biết là cập nhật đè
    try:
        resp = requests.put(api_url, headers=headers, json=payload, timeout=30)
        if resp.status_code in (200, 201):
            raw_url = f"https://raw.githubusercontent.com/{GITHUB_REPO}/{GITHUB_BRANCH}/{repo_path}"
            log.info("✓ Đã đẩy %s lên GitHub.", repo_path)
            return raw_url
        log.error("Đẩy GitHub thất bại (%s): %s", resp.status_code, resp.text[:300])
        return None
    except Exception as e:
        log.error("Lỗi khi đẩy lên GitHub: %s", e)
        return None
# ========================= GEMINI API (tự động, không thao tác tay) =========================
def analyze_with_gemini(local_path: str) -> str:
    """Gọi Gemini API — UPLOAD THẲNG nội dung file (Gemini Files API) thay vì đưa link cho Gemini
    tự đọc. Trả về chuỗi phân tích, None nếu lỗi/chưa cấu hình.

    SỬA 30/09/2026: bản trước dùng công cụ url_context, đưa link raw.githubusercontent.com cho
    Gemini tự fetch — thực tế Gemini báo "Không thể truy cập vào đường dẫn JSON được cung cấp",
    dù link mở bình thường trên trình duyệt (url_context không đáng tin cậy với link GitHub raw
    mới đẩy lên, có thể do cache/robots của Google khi fetch). Dùng Files API (client.files.upload)
    để đưa thẳng nội dung file cho Gemini là cách chắc chắn hơn, không phụ thuộc Gemini tự tải
    mạng ngoài."""
    if not ENABLE_GEMINI_ANALYSIS:
        return None
    if not GEMINI_API_KEY:
        log.warning("Thiếu GEMINI_API_KEY trong .env — bỏ qua phân tích Gemini tự động.")
        return None
    try:
        from google import genai
        from google.genai.errors import ServerError
    except ImportError:
        log.warning("Thiếu thư viện google-genai. Chạy: pip install google-genai")
        return None
    client = genai.Client(api_key=GEMINI_API_KEY)
    retries, backoff = 3, 5.0
    for attempt in range(retries + 1):
        try:
            uploaded = client.files.upload(file=local_path, config={"mime_type": "application/json"})
            response = client.models.generate_content(
                model=GEMINI_MODEL,
                contents=[GEMINI_PROMPT, uploaded],
            )
            parts = response.candidates[0].content.parts if response.candidates else []
            text = "".join(p.text for p in parts if getattr(p, "text", None))
            return text.strip() or None
        except ServerError as e:
            if attempt < retries:
                log.warning("Gemini đang quá tải tạm thời (%s) — chờ %.0fs rồi thử lại (%d/%d)...",
                            e, backoff, attempt + 1, retries)
                time.sleep(backoff)
                backoff *= 1.5  # tăng dần thời gian chờ mỗi lần thử lại
            else:
                log.error("Gemini vẫn quá tải sau %d lần thử: %s", retries, e)
                return None
        except Exception as e:
            log.error("Lỗi khi gọi Gemini API: %s", e)
            return None
def _sanitize_for_telegram(text: str) -> str:
    """Dọn cú pháp Markdown/LaTeX còn sót (phòng khi Gemini không tuân thủ hết hướng dẫn định dạng
    trong GEMINI_PROMPT) — Telegram không tự render các ký hiệu này, hiển thị sẽ lộ nguyên #, **, $$."""
    text = re.sub(r"\$\$(.*?)\$\$", r"\1", text, flags=re.DOTALL)  # cong thuc block $$...$$
    text = re.sub(r"(?<!\d)\$(.+?)\$(?!\d)", r"\1", text)  # cong thuc inline $...$
    text = re.sub(r"^#{1,6}\s*", "", text, flags=re.MULTILINE)  # header ###
    text = re.sub(r"\*\*(.+?)\*\*", r"\1", text)  # **dam**
    text = re.sub(r"__(.+?)__", r"\1", text)  # __dam__
    text = re.sub(r"^-{3,}\s*$", "", text, flags=re.MULTILINE)  # gach ngang phan doan ---
    # SỬA 30/09/2026: phòng hờ khi Gemini vẫn lỡ đánh số/chữ cái đầu dòng dù prompt đã dặn không
    # dùng (đã thấy thực tế: "a, VCH", "b, BSR"...) — xoá các tiền tố kiểu "a, ", "1) ", "2. " ở
    # đầu dòng, giữ nguyên phần nội dung phía sau.
    text = re.sub(r"^\s*(?:[a-zA-Z]|[0-9]{1,2})[,.\)]\s+", "", text, flags=re.MULTILINE)
    text = re.sub(r"^[-*•]\s+", "", text, flags=re.MULTILINE)  # gach dau dong -, *, •
    text = re.sub(r"\n{3,}", "\n\n", text)  # gon dong trong lien tiep
    return text.strip()
def send_telegram_text(message: str):
    """Gửi tin nhắn Telegram — viết riêng cho script này, KHÔNG import từ stock_analysis_bot.py
    (giữ 2 script tách biệt hoàn toàn như đã yêu cầu). Dùng lại đúng TELEGRAM_BOT_TOKEN/CHAT_ID trong .env."""
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        log.info("Chưa cấu hình Telegram — bỏ qua gửi kết quả Gemini qua Telegram.")
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    chunk_size = 3800
    for i in range(0, len(message), chunk_size):
        try:
            resp = requests.post(url, data={"chat_id": TELEGRAM_CHAT_ID, "text": message[i:i + chunk_size]}, timeout=15)
            if resp.status_code != 200:
                log.error("Gửi Telegram thất bại (%s): %s", resp.status_code, resp.text[:200])
        except Exception as e:
            log.error("Lỗi khi gửi Telegram: %s", e)
# ========================= MÔ TẢ DỮ LIỆU (cho Gemini/người đọc) =========================
def write_data_dictionary(output_dir: str):
    content = """# Mô tả dữ liệu (Data dictionary)
Dữ liệu xuất từ SSI FastConnect Data, dùng để đưa vào AI (Gemini, Claude...) phân tích.
Đây KHÔNG phải khuyến nghị đầu tư — chỉ là dữ liệu thô để tham khảo.
## ohlc_history.csv — Giá & khối lượng lịch sử (theo phiên)
- symbol: mã cổ phiếu
- date: ngày giao dịch
- open, high, low, close: giá mở/cao/thấp/đóng (VNĐ)
- volume: khối lượng khớp lệnh (cổ phiếu)
- value: giá trị khớp lệnh (VNĐ)
## securities_summary.csv — Biến động giá, dòng tiền khối ngoại (theo phiên)
- symbol, date
- price_change, price_change_percent: thay đổi giá tuyệt đối / theo %
- open, high, low, close, average_price: giá trong phiên
- total_match_vol, total_match_value: khối lượng/giá trị khớp lệnh
- total_deal_vol, total_deal_value: khối lượng/giá trị giao dịch thỏa thuận
- foreign_buy_vol, foreign_buy_value: khối ngoại mua (khối lượng/giá trị)
- foreign_sell_vol, foreign_sell_value: khối ngoại bán (khối lượng/giá trị)
- remain_foreign_room, total_foreign_room: room khối ngoại còn lại / tổng room
  (room còn thấp = khối ngoại khó mua thêm dù muốn)
## securities_info.csv — Thông tin cơ bản (tĩnh, không theo ngày)
- symbol, name_vi, name_en: tên công ty
- board: sàn niêm yết (HOSE/HNX/UPCOM)
- icb_code, icb_name: mã và tên ngành theo chuẩn ICB
- listed_shares: số cổ phiếu đang lưu hành
- first_trading_date, last_trading_date: ngày giao dịch đầu/cuối
## market_index_summary.csv — Độ rộng & dòng tiền TOÀN SÀN HOSE (theo ngày, VN-Index)
- date, index_value: giá trị chỉ số VN-Index
- index_change, index_change_percent: biến động chỉ số
- advance, decline, steady: số mã tăng / giảm / đứng giá TOÀN SÀN (không chỉ danh mục của bạn)
- ceiling, floor: số mã tăng trần / giảm sàn toàn sàn
- prop_buy_value, prop_sell_value: giá trị mua/bán ròng của các công ty chứng khoán (tự doanh)
- total_match_value, total_deal_value: tổng giá trị khớp lệnh / thỏa thuận toàn sàn
## Gợi ý câu hỏi có thể hỏi AI sau khi import các file này
- "Dựa vào ohlc_history.csv và securities_summary.csv, mã nào có RSI < 60 và khối ngoại mua ròng liên tục 5 phiên gần nhất?"
- "So sánh xu hướng dòng tiền tự doanh trong market_index_summary.csv với biến động VN-Index — có tương quan không?"
- "Mã nào trong securities_summary.csv có room khối ngoại dưới 10%?"
"""
    with open(os.path.join(output_dir, "README_data.md"), "w", encoding="utf-8") as f:
        f.write(content)
# ========================= MAIN =========================
def main():
    log.info("Xuất dữ liệu SSI cho %d mã: %s", len(ALL_SYMBOLS), ", ".join(ALL_SYMBOLS))
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    client = get_client()
    to_date = datetime.now()
    from_date = to_date - timedelta(days=EXPORT_HISTORY_DAYS)
    df_ohlc = export_ohlc(client, ALL_SYMBOLS, from_date, to_date)
    df_ohlc.to_csv(os.path.join(OUTPUT_DIR, "ohlc_history.csv"), index=False, encoding="utf-8-sig")
    log.info("✓ ohlc_history.csv (%d dòng)", len(df_ohlc))
    df_summary = export_securities_summary(client, ALL_SYMBOLS, from_date, to_date)
    df_summary.to_csv(os.path.join(OUTPUT_DIR, "securities_summary.csv"), index=False, encoding="utf-8-sig")
    log.info("✓ securities_summary.csv (%d dòng)", len(df_summary))
    df_info = export_securities_info(client, ALL_SYMBOLS)
    df_info.to_csv(os.path.join(OUTPUT_DIR, "securities_info.csv"), index=False, encoding="utf-8-sig")
    log.info("✓ securities_info.csv (%d dòng)", len(df_info))
    df_breadth = export_market_index_summary(client, EXPORT_BREADTH_DAYS)
    df_breadth.to_csv(os.path.join(OUTPUT_DIR, "market_index_summary.csv"), index=False, encoding="utf-8-sig")
    log.info("✓ market_index_summary.csv (%d dòng)", len(df_breadth))
    write_data_dictionary(OUTPUT_DIR)
    log.info("✓ README_data.md")
    # Gộp tất cả vào 1 file JSON duy nhất — đây là file dùng để lấy 1 link Raw duy nhất cho Gemini
    combined = build_combined_data(df_ohlc, df_summary, df_info, df_breadth)
    combined_path = os.path.join(OUTPUT_DIR, "all_data.json")
    with open(combined_path, "w", encoding="utf-8") as f:
        json.dump(combined, f, ensure_ascii=False, allow_nan=False, default=str)
    log.info("✓ all_data.json (file GỘP ĐẦY ĐỦ — dùng file này để lấy 1 link Raw duy nhất)")
    # File RIÊNG, GỌN HƠN dành cho Gemini (bỏ ohlc_history) — đảm bảo Gemini đọc được hết nội dung
    gemini_payload = build_gemini_payload(df_summary, df_info, df_breadth)
    gemini_path = os.path.join(OUTPUT_DIR, "gemini_data.json")
    with open(gemini_path, "w", encoding="utf-8") as f:
        json.dump(gemini_payload, f, ensure_ascii=False, allow_nan=False, default=str)
    log.info("✓ gemini_data.json (file GỌN — dùng riêng cho Gemini, %d KB)", os.path.getsize(gemini_path) // 1024)
    if AUTO_PUSH_GITHUB:
        raw_url = push_to_github(combined_path, GITHUB_FILE_PATH)
        if raw_url:
            print(f"\n🔗 Link Raw (để dùng thủ công nếu cần):\n{raw_url}\n")
        gemini_raw_url = push_to_github(gemini_path, GEMINI_FILE_PATH)
        if gemini_raw_url:
            print(f"🔗 Link Raw (bản gọn, cho tham khảo thủ công — Gemini KHÔNG đọc qua link này nữa):\n{gemini_raw_url}\n")
        if ENABLE_GEMINI_ANALYSIS:
            # SỬA 30/09/2026: gọi Gemini bằng file CỤC BỘ (gemini_path) qua Files API, KHÔNG còn
            # phụ thuộc việc đẩy GitHub có thành công hay không, và không còn nhờ Gemini tự tải
            # link raw.githubusercontent.com nữa (đã xác nhận không ổn định).
            log.info("Đang gọi Gemini API để phân tích tự động (upload thẳng file, không qua link)...")
            analysis = analyze_with_gemini(gemini_path)
            if analysis:
                analysis = _sanitize_for_telegram(analysis)
                print("\n===== PHÂN TÍCH TỪ GEMINI =====\n")
                print(analysis)
                print("\n================================\n")
                if SEND_GEMINI_TO_TELEGRAM:
                    send_telegram_text(f"🤖 Phân tích từ Gemini:\n\n{analysis}")
            else:
                log.warning("Không lấy được phân tích từ Gemini — kiểm tra GEMINI_API_KEY hoặc log lỗi phía trên.")
    else:
        log.info("AUTO_PUSH_GITHUB chưa bật (=false) — bạn cần tự tải all_data.json lên GitHub.")
    log.info("HOÀN TẤT. Toàn bộ file nằm trong thư mục: %s/", OUTPUT_DIR)
if __name__ == "__main__":
    main()
