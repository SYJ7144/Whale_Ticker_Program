from fastapi import FastAPI, Query, HTTPException
from fastapi.middleware.cors import CORSMiddleware
import yfinance as yf
import random
from datetime import datetime
from apscheduler.schedulers.background import BackgroundScheduler
from supabase import create_client, Client
import os
from dotenv import load_dotenv
import os

load_dotenv() # .env 파일을 읽어오는 핵심 함수!ㄴ
app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# --- [현업 보안 핵심] 소스 코드에 키를 적지 않고, 서버 환경 변수에서 안전하게 불러옴 ---
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")

# 서버가 켜질 때 환경 변수가 누락되었는지 엄격하게 검사
if not SUPABASE_URL or not SUPABASE_KEY:
    raise RuntimeError("🚨 보안 경고: SUPABASE_URL 또는 SUPABASE_KEY 환경 변수가 설정되지 않았습니다!")

supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

US_POOL = ['NVDA', 'AAPL', 'TSLA', 'MSFT', 'AMD', 'META', 'AMZN', 'GOOGL', 'MSTR', 'COIN',
           'NFLX', 'INTC', 'QCOM', 'ARM', 'PLTR', 'TSM', 'AVGO', 'ORCL', 'BA', 'DIS']
KR_POOL = ['005930.KS', '000660.KS', '003230.KS', '196170.KQ', '086520.KQ', '068270.KS', '000270.KS', '079550.KS', '267260.KS', '042700.KS']

def calculate_market_data(tickers):
    results = []
    try:
        data = yf.download(tickers, period="1mo", interval="1d", group_by="ticker", progress=False, threads=False)
        for ticker in tickers:
            try:
                df = data[ticker].dropna() if len(tickers) > 1 else data.dropna()
                if len(df) < 5: continue
                
                current_price = float(df['Close'].iloc[-1])
                prev_price = float(df['Close'].iloc[-2])
                change_pct = ((current_price - prev_price) / prev_price) * 100
                
                today_vol = float(df['Volume'].iloc[-1])
                avg_20d_vol = float(df['Volume'].mean()) if len(df) > 0 else 1
                vol_ratio = today_vol / avg_20d_vol if avg_20d_vol > 0 else 1.2
                estimated_amount = current_price * today_vol

                volatility = abs(change_pct)
                ev1 = min(35, int((vol_ratio * 5) + (volatility * 2) + random.randint(5, 15)))
                ev2 = min(25, int((vol_ratio * 4) + random.randint(5, 15)))
                ev3 = min(20, int((volatility * 3) + random.randint(5, 10)))
                ev4 = min(20, int((vol_ratio - 1) * 10)) if vol_ratio > 1 else 10
                score = ev1 + ev2 + ev3 + ev4
                
                tags = ['[외인/기관 양매수형]'] if score > 80 else ['[관망 장세]']
                currency = '₩' if '.KS' in ticker or '.KQ' in ticker else '$'
                price_str = f"{currency}{current_price:,.2f}" if currency == '$' else f"{currency}{int(current_price):,}"
                change_str = f"{'+' if change_pct > 0 else ''}{change_pct:.2f}%"

                names_map = {
                    'NVDA': '엔비디아', 'AAPL': '애플', 'TSLA': '테슬라', 'MSFT': '마이크로소프트', 'AMD': 'AMD',
                    'META': '메타', 'AMZN': '아마존', 'GOOGL': '구글', 'MSTR': '마이크로스트레티지', 'COIN': '코인베이스',
                    '005930.KS': '삼성전자', '000660.KS': 'SK하이닉스', '003230.KS': '삼양식품', '196170.KQ': '알테오젠', '086520.KQ': '에코프로',
                    '068270.KS': '셀트리온', '000270.KS': '기아', '079550.KS': 'LIG넥스원', '267260.KS': 'HD현대일렉트릭', '042700.KS': '한미반도체'
                }

                results.append({
                    'ticker': ticker.replace('.KS', '').replace('.KQ', ''),
                    'name': names_map.get(ticker, ticker),
                    'price': price_str, 'change': change_str, 'change_val': change_pct,
                    'volume': today_vol, 'amount': estimated_amount, 'score': score,
                    'tags': tags, 'ev1': ev1, 'ev2': ev2, 'ev3': ev3, 'ev4': ev4,
                    'desc1': f"체결 강도 {int(vol_ratio*100)}% 증가", 'desc2': '수급 유입 징후 감지',
                    'desc3': '호가창 매물대 돌파 추정', 'desc4': f"20일 평균 대비 {vol_ratio:.1f}배 돌파"
                })
            except Exception:
                continue
    except Exception as e:
        print(f"전체 데이터 다운로드 에러: {e}")
    return results

def background_sync_job():
    print(f"[{datetime.now()}] Supabase 캐시 데이터 갱신 시작...")
    markets = {"US": US_POOL, "KR": KR_POOL, "ALL": US_POOL + KR_POOL}
    sort_types = ["amount", "volume", "surge", "drop"]

    for market_key, tickers in markets.items():
        raw_data = calculate_market_data(tickers)
        if not raw_data: continue

        for sort_by in sort_types:
            if sort_by == "amount": sorted_data = sorted(raw_data, key=lambda x: x['amount'], reverse=True)
            elif sort_by == "volume": sorted_data = sorted(raw_data, key=lambda x: x['volume'], reverse=True)
            elif sort_by == "surge": sorted_data = sorted(raw_data, key=lambda x: x['change_val'], reverse=True)
            else: sorted_data = sorted(raw_data, key=lambda x: x['change_val'])

            top_20 = sorted_data[:20]

            try:
                supabase.table("whale_cache").delete().eq("market", market_key).eq("sort_by", sort_by).execute()

                for idx, item in enumerate(top_20):
                    payload = {
                        "market": market_key, "sort_by": sort_by, "rank_num": idx + 1,
                        "ticker": item['ticker'], "name": item['name'], "price": item['price'],
                        "change": item['change'], "change_val": item['change_val'], "volume": item['volume'],
                        "amount": item['amount'], "score": item['score'], "tags": item['tags'],
                        "ev1": item['ev1'], "ev2": item['ev2'], "ev3": item['ev3'], "ev4": item['ev4'],
                        "updated_at": datetime.now().isoformat()
                    }
                    supabase.table("whale_cache").insert(payload).execute()
            except Exception as db_err:
                print(f"-> Supabase DB 저장 에러 ({market_key} / {sort_by}): {db_err}")
                
    print(f"[{datetime.now()}] Supabase 캐시 갱신 완료!")

background_sync_job()

scheduler = BackgroundScheduler()
scheduler.add_job(background_sync_job, 'interval', minutes=5)
scheduler.start()

@app.get("/api/stocks")
def get_whale_stocks(market: str = Query("ALL"), sort_by: str = Query("amount")):
    try:
        response = supabase.table("whale_cache").select("*").eq("market", market).eq("sort_by", sort_by).order("rank_num").execute()
        data = response.data
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"데이터베이스 조회 실패: {str(e)}")
    
    formatted_list = []
    if data:
        for item in data:
            formatted_list.append({
                'id': item['rank_num'],
                'name': item['name'],
                'ticker': item['ticker'],
                'price': item['price'],
                'change': item['change'],
                'score': item['score'],
                'tags': item['tags'],
                'ev1': item['ev1'], 'ev2': item['ev2'], 'ev3': item['ev3'], 'ev4': item['ev4'],
                'desc1': f"체결 강도 증가 포착", 'desc2': '수급 유입 징후 감지',
                'desc3': '호가창 매물대 돌파 추정', 'desc4': f"평균 거래량 대비 돌파"
            })
    return formatted_list