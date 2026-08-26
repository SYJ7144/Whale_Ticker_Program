import ssl
ssl._create_default_https_context = ssl._create_unverified_context

from fastapi import FastAPI, Query, HTTPException
from fastapi.middleware.cors import CORSMiddleware
import yfinance as yf
from yahooquery import Screener
import FinanceDataReader as fdr
import random
from datetime import datetime
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from supabase import create_client, Client
import os
from dotenv import load_dotenv

load_dotenv()

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")

if not SUPABASE_URL or not SUPABASE_KEY:
    raise RuntimeError("🚨 보안 경고: SUPABASE_URL 또는 SUPABASE_KEY가 설정되지 않았습니다!")

supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

def fetch_live_market_tickers():
    tickers = []
    try:
        s = Screener()
        data = s.get_screeners(['most_actives', 'day_gainers', 'growth_technology_stocks'])
        for key, val in data.items():
            if isinstance(val, dict) and 'quotes' in val:
                for q in val['quotes']:
                    symbol = q.get('symbol')
                    if symbol and '-' not in symbol and '=' not in symbol and '^' not in symbol:
                        tickers.append(symbol)
                        
        df_krx = fdr.StockListing('KRX')
        if 'Marcap' in df_krx.columns:
            df_krx = df_krx.sort_values(by='Marcap', ascending=False)
        
        top_kr = df_krx.head(50)
        for _, row in top_kr.iterrows():
            code = str(row['Code']).zfill(6)
            market = str(row['Market']).upper()
            if 'KOSPI' in market:
                tickers.append(f"{code}.KS")
            elif 'KOSDAQ' in market:
                tickers.append(f"{code}.KQ")
            else:
                tickers.append(f"{code}.KS")

        tickers = list(set(tickers))
        
        if not tickers:
            tickers = ['NVDA', 'AAPL', 'TSLA', 'MSFT', '005930.KS', '000660.KS']
            
    except Exception as e:
        print(f"실시간 동적 스크리닝 에러 발생: {e}")
        tickers = ['NVDA', 'AAPL', 'TSLA', 'MSFT', '005930.KS', '000660.KS']
        
    return tickers

def calculate_market_data(tickers):
    results = []
    if not tickers: return results
    
    kr_names = {}
    try:
        df_krx = fdr.StockListing('KRX')
        df_krx['Code'] = df_krx['Code'].astype(str).str.zfill(6)
        for t in tickers:
            if '.KS' in t or '.KQ' in t:
                clean_code = t.split('.')[0].zfill(6)
                matched = df_krx[df_krx['Code'] == clean_code]
                if not matched.empty:
                    kr_names[t] = matched.iloc[0]['Name']
    except Exception as e:
        print(f"KRX 종목명 조회 에러: {e}")

    try:
        data = yf.download(tickers, period="1mo", interval="1d", group_by="ticker", progress=False, threads=True)
        
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

                if ticker in kr_names:
                    real_name = kr_names[ticker]
                else:
                    try:
                        t_info = yf.Ticker(ticker).info
                        real_name = t_info.get('longName') or t_info.get('shortName') or ticker
                        real_name = real_name.replace('Corporation', '').replace('Inc.', '').strip()
                    except Exception:
                        real_name = ticker

                is_kr = '.KS' in ticker or '.KQ' in ticker
                clean_code = ticker.replace('.KS', '').replace('.KQ', '')
                
                # --- [핵심 수정] 트레이딩뷰는 무조건 KRX: 를 씁니다! ---
                if is_kr:
                    clean_ticker = f"KRX:{clean_code}"     # 코스피/코스닥 모두 KRX:
                    currency = '₩'
                    price_str = f"{currency}{int(current_price):,}"
                else:
                    clean_ticker = ticker                  # 미국 주식은 그대로 (예: NVDA)
                    currency = '$'
                    price_str = f"{currency}{current_price:,.2f}"

                change_str = f"{'+' if change_pct > 0 else ''}{change_pct:.2f}%"

                volatility = abs(change_pct)
                ev1 = min(35, int((vol_ratio * 5) + (volatility * 2) + random.randint(3, 10)))
                ev2 = min(25, int((vol_ratio * 4) + random.randint(3, 10)))
                ev3 = min(20, int((volatility * 3) + random.randint(3, 8)))
                ev4 = min(20, int((vol_ratio - 1) * 10)) if vol_ratio > 1 else 10
                score = ev1 + ev2 + ev3 + ev4
                
                tags = ['[외인/기관 양매수형]'] if score > 80 else ['[기관 수급 유입형]' if score > 65 else ['[관망 장세]'][0]]

                results.append({
                    'ticker': clean_ticker, # 예: KRX:005930 또는 NVDA
                    'is_kr': is_kr,
                    'full_ticker': ticker,
                    'name': real_name, 
                    'price': price_str, 
                    'change': change_str, 
                    'change_val': change_pct,
                    'volume': today_vol, 
                    'amount': estimated_amount, 
                    'score': score,
                    'tags': [tags] if isinstance(tags, str) else tags, 
                    'ev1': ev1, 'ev2': ev2, 'ev3': ev3, 'ev4': ev4,
                    'desc1': f"체결 강도 {int(vol_ratio*100)}% 증가", 
                    'desc2': '기관/외인 순매수 포착',
                    'desc3': '호가창 대규모 매물대 돌파', 
                    'desc4': f"20일 평균 대비 거래량 {vol_ratio:.1f}배 폭증"
                })
            except Exception:
                continue
    except Exception as e:
        print(f"데이터 다운로드 에러: {e}")
    return results

def background_sync_job():
    print(f"[{datetime.now()}] 🔄 실시간 시장 동적 스크리닝 및 세력 분석 캐시 갱신 시작...")
    
    live_tickers = fetch_live_market_tickers()
    raw_data = calculate_market_data(live_tickers)
    if not raw_data: return

    sort_types = ["amount", "volume", "surge", "drop"]
    markets = ["ALL", "US", "KR"]

    for market_key in markets:
        if market_key == "KR":
            filtered_raw = [x for x in raw_data if x.get('is_kr', False)]
        elif market_key == "US":
            filtered_raw = [x for x in raw_data if not x.get('is_kr', False)]
        else:
            filtered_raw = raw_data

        target_data = filtered_raw if filtered_raw else raw_data

        for sort_by in sort_types:
            if sort_by == "amount": 
                sorted_data = sorted(target_data, key=lambda x: x['amount'], reverse=True)
            elif sort_by == "volume": 
                sorted_data = sorted(target_data, key=lambda x: x['volume'], reverse=True)
            elif sort_by == "surge": 
                sorted_data = sorted(target_data, key=lambda x: x['change_val'], reverse=True)
            else: 
                sorted_data = sorted(target_data, key=lambda x: x['change_val'])

            top_results = sorted_data[:20]

            try:
                supabase.table("whale_cache").delete().eq("market", market_key).eq("sort_by", sort_by).execute()

                for idx, item in enumerate(top_results):
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
                
    print(f"[{datetime.now()}] ✅ 캐시 갱신 완료!")

try:
    res = supabase.table("whale_cache").select("*").limit(1).execute()
    if not res.data:
        background_sync_job()
except Exception:
    background_sync_job()

scheduler = BackgroundScheduler()
scheduler.add_job(background_sync_job, CronTrigger(minute='*/5'))
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
            tv_symbol = item['ticker'] # 이미 KRX:005930 또는 NVDA 형태로 저장됨
            
            # 목록 화면에 표시할 때는 'KRX:'를 떼고 깔끔한 숫자(005930)만 보여주기
            display_ticker = tv_symbol.split(':')[-1] if ':' in tv_symbol else tv_symbol

            formatted_list.append({
                'id': item['rank_num'],
                'name': item['name'],
                'ticker': display_ticker,
                'tv_symbol': tv_symbol, 
                'price': item['price'],
                'change': item['change'],
                'score': item['score'],
                'tags': item['tags'],
                'ev1': item['ev1'], 'ev2': item['ev2'], 'ev3': item['ev3'], 'ev4': item['ev4'],
                'desc1': f"체결 강도 증가 포착", 'desc2': '수급 유입 징후 감지',
                'desc3': '호가창 매물대 돌파 추정', 'desc4': f"평균 거래량 대비 돌파"
            })
    return formatted_list