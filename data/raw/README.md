# Raw data drop folder

Place broker exports here. Supported layout (same as the bundled sample):

```
Date,Time,Open,High,Low,Close,Volume
20150101,22:00:00,119.746,119.746,119.743,119.744,4500000
```

File naming convention: `{SYMBOL}-{TIMEFRAME}-{label}.csv`, e.g.
`USDJPY-M1-No Session.csv`, `EURUSD-M1-2020-2025.csv`.

The importer validates and normalizes everything into `data/parquet/` and
records a manifest. Raw CSVs are never modified or deleted.
