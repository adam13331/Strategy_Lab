export type ParamRange = { min: number; max: number; step: number };
export type IndicatorSetting = { enabled: boolean; params: Record<string, ParamRange> };
export type Policy = {
  mode: "always" | "never" | "may";
  type: "fixed" | "atr";
  pips_min: number; pips_max: number; pips_step: number;
  atr_period_min: number; atr_period_max: number; atr_period_step: number;
  atr_mult_min: number; atr_mult_max: number; atr_mult_step: number;
};
export type Trailing = {
  mode: "none" | "use";
  atr_period_min: number; atr_period_max: number; atr_period_step: number;
  atr_mult_min: number; atr_mult_max: number; atr_mult_step: number;
};
export type EntryOrders = {
  types: ("market" | "limit" | "stop")[];
  max_orders: number;
  distance_type: "fixed" | "atr";
  pips_min: number; pips_max: number; pips_step: number;
  atr_period_min: number; atr_period_max: number; atr_period_step: number;
  atr_mult_min: number; atr_mult_max: number; atr_mult_step: number;
  expiry_bars_min: number; expiry_bars_max: number; expiry_bars_step: number;
};
export type Gates = {
  min_trades: number; min_sharpe: number; max_stagnation_days: number;
  min_return_dd: number; max_drawdown_pct: number; min_profit_factor: number;
  min_win_rate: number;
};
export type Config = {
  symbols: string[];
  timeframe: string;
  direction: "long" | "short" | "both";
  opposite_signal: "ignore" | "reverse" | "ignore_or_reverse";
  stop_loss: Policy;
  take_profit: Policy;
  orders: EntryOrders;
  trailing: Trailing;
  max_entry_indicators: number;
  max_exit_indicators: number;
  indicators: Record<string, IndicatorSetting>;
  session: { start_hour: number; end_hour: number; close_at_end: boolean; days: number[] };
  risk: {
    sizing: "fixed_lot" | "risk_percent"; fixed_lot: number; risk_percent: number;
    leverage: number; balance: number; spread_pips: number;
    commission_per_lot: number; slippage_pips: number;
  };
  acceptance: { is_gates: Gates; oos_gates: Gates };
  autocorrelation: { enabled: boolean; threshold: number; method: "pearson" | "spearman" };
  oos_pct: number;
  target_accepted: number;
  seed: number;
};

export type IndicatorDef = {
  id: string; label: string; category: string; description: string;
  params: { name: string; label: string; default: number | string; min: number; max: number; step: number; integer: boolean; choices: string[] }[];
  outputs: { key: string; label: string; kind: string; levels: number[] }[];
  warmup: number; needs_volume: boolean;
};

export type CsvFile = {
  name: string;
  path: string;
  symbol: string | null;
  error: string | null;
  size_bytes: number;
  modified: string;
  imported: boolean;
  imported_rows: number | null;
  imported_at: string | null;
};

export type SymbolInfo = {
  symbol: string; source_file: string; rows: number; start_ts: string; end_ts: string;
  tz_assumed: string; imported_at: string;
  quality: {
    rows: number; start: string; end: string; duplicate_rows: number; invalid_rows: number;
    gaps_over_1min: number; gaps_over_1day: number; inferred_tz: string;
    years: Record<string, number>; weekday_counts: Record<string, number>;
  };
  bar_caches?: { timeframe: string; rows: number; start_ts: string; end_ts: string }[];
};

export type Metrics = {
  trades: number; net_return: number; max_dd: number; return_dd: number;
  sharpe: number; stagnation_days: number; profit_factor: number;
  win_rate: number; avg_trade: number;
};

export type StrategyRow = {
  strategy_id: string; run_id: string; spec_hash: string; symbol: string;
  timeframe: string; direction: string; score: number; correlation_max: number | null;
  nearest_id: string | null; trades: number; created_at: string;
  metrics_is: Metrics; metrics_oos: Metrics;
};

export type RunInfo = {
  run_id: string; name?: string; status: string; attempts?: number; accepted?: number;
  target?: number; elapsed?: number; throughput?: number;
  funnel?: Record<string, number>;
  recent?: any[]; config?: Config; stats?: any; error?: string | null;
  created_at?: string; started_at?: string; finished_at?: string;
};

export type StrategyDetail = StrategyRow & {
  spec: any;
  rules: {
    long: string[]; short: string[]; exits: string[];
    stop_loss: string; take_profit: string; trailing: string;
    orders: string; opposite_signal: string; max_bars: number;
  };
};

export type StressStageConfig = { enabled: boolean };
export type StressParameterStage = StressStageConfig & {
  rounds: number; perturb_min_pct: number; perturb_max_pct: number;
  max_sharpe_drop_pct: number; max_profit_factor_drop_pct: number;
  min_profitable_pct: number;
};
export type StressPriceCostStage = StressStageConfig & {
  rounds: number; noise_min_pct: number; noise_max_pct: number;
  max_sharpe_drop_pct: number; cost_multiplier_min: number;
  cost_multiplier_max: number; min_profitable_pct: number;
  baseline_slippage_pips: number | null;
  baseline_commission_per_lot: number | null;
};
export type StressShuffleStage = StressStageConfig & {
  rounds: number; p95_max_dd_multiple: number; p99_max_dd_multiple: number;
};
export type StressMonteCarloStage = StressStageConfig & {
  rounds: number; method: "block_bootstrap" | "garch";
  block_size: number; min_profitable_pct: number;
};
export type StressCrossMarketStage = StressStageConfig & {
  alternate_symbols: string[] | null; min_avg_profit_factor: number;
};
export type StressConfig = {
  source_run_id: string; bank_name: string | null; seed: number;
  strategy_ids: string[] | null;
  parameter: StressParameterStage; price_cost: StressPriceCostStage;
  shuffle: StressShuffleStage; monte_carlo: StressMonteCarloStage;
  cross_market: StressCrossMarketStage;
};
export type StressCheck = {
  name: string; value: number; threshold: number; op: string;
  unit?: string; passed: boolean;
};
export type StressStageResult = {
  stage: string; label: string; status: string; passed: boolean;
  tests_requested: number; tests_run: number; checks: StressCheck[];
  checks_passed: number; checks_total: number; pass_rate?: number;
  metrics: any; extra: any;
};
export type StressResult = {
  strategy_id: string; survival: boolean;
  stages: Record<string, StressStageResult>;
  tests_requested: number; tests_run: number; pass_rate: number;
  metrics: any; created_at?: string;
};
export type StressRun = {
  stress_id: string; name?: string; source_run_id?: string; status: string;
  strategies_total?: number; strategies_done?: number; survivors?: number;
  tests_requested?: number; tests_run?: number; stages_requested?: number;
  elapsed?: number; throughput?: number; bank_id?: string | null;
  error?: string | null;
  current?: {
    strategy_id?: string; index?: number; stage?: string | null;
    stage_index?: number; stage_tests_run?: number;
    stage_tests_requested?: number; stages_done?: number;
    tests_requested?: number; stopping?: boolean;
  } | null;
  recent?: any[]; results?: StressResult[]; config?: StressConfig;
  stats?: any; created_at?: string; started_at?: string; finished_at?: string;
};

export type BankInfo = {
  bank_id: string; name: string; source_run_id?: string; rob_id?: string;
  group?: string; bank_type?: string; category?: string; kind?: string;
  count: number; created_at?: string; config?: any;
};

export type BankStrategy = {
  bank_id: string; strategy_id: string; source_run_id: string;
  symbol: string; timeframe: string; direction: string;
  metrics_is: Metrics; metrics_oos: Metrics;
  metrics_all: Record<string, { median: number; p05: number }>;
  score: number;
  created_at: string;
};
