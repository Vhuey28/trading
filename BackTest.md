## Backtester  (backtest.py)
  
##  Replays bar data through the exact same strategy as the live bot. Usage:
  
 ##   Test using already-cached data
 ```py
    python src/backtest.py --start 2026-07-01 --end 2026-07-10
  ```

##    # Test specific symbols
```py
    python src/backtest.py --start 2026-07-01 --end 2026-07-10 --symbols QQQ SPY IWM SCHD BND
  ```

##    # Auto-download missing days from Alpaca, then test
```py
    python src/backtest.py --start 2026-07-01 --end 2026-07-10 --fetch
 ```
  
##    # Show every buy/sell signal
```py
    python src/backtest.py --start 2026-07-01 --end 2026-07-10 --verbose
 ```   

 # Restart the service
  launchctl unload ~/Library/LaunchAgents/com.vaughnhuey.trend-bot.plist
  launchctl load ~/Library/LaunchAgents/com.vaughnhuey.trend-bot.plist