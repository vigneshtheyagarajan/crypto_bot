from dotenv import load_dotenv
import os

load_dotenv()

API_KEY    = os.getenv("BINANCE_API_KEY")
API_SECRET = os.getenv("BINANCE_API_SECRET")
CAPITAL    = float(os.getenv("CAPITAL", 500))   # in USDT
