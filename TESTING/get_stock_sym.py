import pandas as pd

df = pd.read_csv("2025-01-02.csv")
tickers = df['ticker'].unique().tolist()
print(tickers)
