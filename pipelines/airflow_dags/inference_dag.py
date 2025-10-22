"""
Inference DAG

Orchestrates real-time inference and signal generation.
Runs every 15 minutes during market hours.
"""

from datetime import datetime, timedelta
from typing import Dict, Any, List

from airflow import DAG
from airflow.operators.python import PythonOperator
from airflow.operators.bash import BashOperator
from airflow.sensors.external_task import ExternalTaskSensor
from airflow.providers.postgres.operators.postgres import PostgresOperator

import logging

logger = logging.getLogger(__name__)


default_args = {
    'owner': 'chronox',
    'depends_on_past': False,
    'start_date': datetime(2024, 1, 1),
    'email': ['alerts@chronox.ai'],
    'email_on_failure': True,
    'email_on_retry': False,
    'retries': 2,
    'retry_delay': timedelta(minutes=1),
}


def fetch_latest_data(**context) -> Dict[str, Any]:
    """
    Fetch latest market data for inference.
    
    Returns:
        Dictionary with data statistics
    """
    from inference.data_fetcher import InferenceDataFetcher
    
    execution_date = context['execution_date']
    logger.info(f"Fetching latest data for inference at {execution_date}")
    
    fetcher = InferenceDataFetcher()
    
    # Fetch latest data (last 100 candles)
    data = fetcher.fetch_latest_data(
        lookback_periods=100,
        execution_date=execution_date
    )
    
    stats = {
        'symbols_fetched': len(data),
        'total_records': sum(len(v) for v in data.values()),
        'timestamp': execution_date.isoformat()
    }
    
    logger.info(f"Data fetched: {stats}")
    
    context['task_instance'].xcom_push(key='inference_data_stats', value=stats)
    
    return stats


def run_transformer_inference(**context) -> Dict[str, Any]:
    """
    Run Transformer model inference.
    
    Returns:
        Model predictions
    """
    from inference.models.transformer_inference import TransformerInference
    
    execution_date = context['execution_date']
    logger.info(f"Running Transformer inference at {execution_date}")
    
    model = TransformerInference()
    predictions = model.predict(execution_date=execution_date)
    
    logger.info(f"Transformer predictions: {len(predictions)} symbols")
    
    context['task_instance'].xcom_push(key='transformer_predictions', value=predictions)
    
    return predictions


def run_lstm_inference(**context) -> Dict[str, Any]:
    """
    Run LSTM model inference.
    
    Returns:
        Model predictions
    """
    from inference.models.lstm_inference import LSTMInference
    
    execution_date = context['execution_date']
    logger.info(f"Running LSTM inference at {execution_date}")
    
    model = LSTMInference()
    predictions = model.predict(execution_date=execution_date)
    
    logger.info(f"LSTM predictions: {len(predictions)} symbols")
    
    context['task_instance'].xcom_push(key='lstm_predictions', value=predictions)
    
    return predictions


def run_xgboost_inference(**context) -> Dict[str, Any]:
    """
    Run XGBoost model inference.
    
    Returns:
        Model predictions
    """
    from inference.models.xgboost_inference import XGBoostInference
    
    execution_date = context['execution_date']
    logger.info(f"Running XGBoost inference at {execution_date}")
    
    model = XGBoostInference()
    predictions = model.predict(execution_date=execution_date)
    
    logger.info(f"XGBoost predictions: {len(predictions)} symbols")
    
    context['task_instance'].xcom_push(key='xgboost_predictions', value=predictions)
    
    return predictions


def run_rl_inference(**context) -> Dict[str, Any]:
    """
    Run RL agent inference.
    
    Returns:
        Agent actions
    """
    from inference.rl.rl_inference import RLInference
    
    execution_date = context['execution_date']
    logger.info(f"Running RL inference at {execution_date}")
    
    agent = RLInference()
    actions = agent.get_actions(execution_date=execution_date)
    
    logger.info(f"RL actions: {len(actions)} symbols")
    
    context['task_instance'].xcom_push(key='rl_actions', value=actions)
    
    return actions


def ensemble_signals(**context) -> Dict[str, Any]:
    """
    Combine all model predictions using ensemble.
    
    Returns:
        Final trading signals
    """
    from inference.ensemble.signal_ensemble import SignalEnsemble
    
    execution_date = context['execution_date']
    logger.info(f"Generating ensemble signals at {execution_date}")
    
    # Get predictions from all models
    transformer_pred = context['task_instance'].xcom_pull(
        key='transformer_predictions',
        task_ids='run_transformer_inference'
    )
    lstm_pred = context['task_instance'].xcom_pull(
        key='lstm_predictions',
        task_ids='run_lstm_inference'
    )
    xgboost_pred = context['task_instance'].xcom_pull(
        key='xgboost_predictions',
        task_ids='run_xgboost_inference'
    )
    rl_actions = context['task_instance'].xcom_pull(
        key='rl_actions',
        task_ids='run_rl_inference'
    )
    
    # Ensemble predictions
    ensemble = SignalEnsemble()
    final_signals = ensemble.combine_predictions(
        transformer=transformer_pred,
        lstm=lstm_pred,
        xgboost=xgboost_pred,
        rl=rl_actions,
        execution_date=execution_date
    )
    
    logger.info(f"Generated {len(final_signals)} final signals")
    
    context['task_instance'].xcom_push(key='final_signals', value=final_signals)
    
    return final_signals


def apply_risk_management(**context) -> Dict[str, Any]:
    """
    Apply risk management filters to signals.
    
    Returns:
        Risk-adjusted signals
    """
    from inference.risk.risk_manager import RiskManager
    
    execution_date = context['execution_date']
    logger.info(f"Applying risk management at {execution_date}")
    
    # Get final signals
    signals = context['task_instance'].xcom_pull(
        key='final_signals',
        task_ids='ensemble_signals'
    )
    
    # Apply risk filters
    risk_manager = RiskManager()
    adjusted_signals = risk_manager.filter_signals(
        signals=signals,
        execution_date=execution_date,
        max_position_size=0.05,
        max_portfolio_risk=0.15,
        max_drawdown=0.20
    )
    
    logger.info(f"Risk-adjusted signals: {len(adjusted_signals)}")
    
    context['task_instance'].xcom_push(key='risk_adjusted_signals', value=adjusted_signals)
    
    return adjusted_signals


def execute_trades(**context) -> Dict[str, Any]:
    """
    Execute trades based on risk-adjusted signals.
    
    Returns:
        Trade execution results
    """
    from trading.trade_executor import TradeExecutor
    
    execution_date = context['execution_date']
    logger.info(f"Executing trades at {execution_date}")
    
    # Get risk-adjusted signals
    signals = context['task_instance'].xcom_pull(
        key='risk_adjusted_signals',
        task_ids='apply_risk_management'
    )
    
    # Execute trades
    executor = TradeExecutor()
    execution_results = executor.execute_signals(
        signals=signals,
        execution_date=execution_date,
        mode='paper',  # Change to 'live' for production
        slippage_model='realistic'
    )
    
    logger.info(f"Executed {len(execution_results['trades'])} trades")
    
    context['task_instance'].xcom_push(key='execution_results', value=execution_results)
    
    return execution_results


def update_portfolio_state(**context) -> None:
    """
    Update portfolio state in database.
    """
    from trading.portfolio_manager import PortfolioManager
    
    execution_date = context['execution_date']
    logger.info(f"Updating portfolio state at {execution_date}")
    
    # Get execution results
    results = context['task_instance'].xcom_pull(
        key='execution_results',
        task_ids='execute_trades'
    )
    
    # Update portfolio
    portfolio = PortfolioManager()
    portfolio.update_state(
        trades=results['trades'],
        execution_date=execution_date
    )
    
    logger.info("Portfolio state updated")


def log_inference_metrics(**context) -> None:
    """
    Log inference metrics to Prometheus.
    """
    from monitoring.metrics_logger import MetricsLogger
    
    execution_date = context['execution_date']
    logger.info(f"Logging inference metrics at {execution_date}")
    
    metrics_logger = MetricsLogger()
    
    # Get all relevant data
    data_stats = context['task_instance'].xcom_pull(
        key='inference_data_stats',
        task_ids='fetch_latest_data'
    )
    final_signals = context['task_instance'].xcom_pull(
        key='final_signals',
        task_ids='ensemble_signals'
    )
    risk_adjusted = context['task_instance'].xcom_pull(
        key='risk_adjusted_signals',
        task_ids='apply_risk_management'
    )
    execution_results = context['task_instance'].xcom_pull(
        key='execution_results',
        task_ids='execute_trades'
    )
    
    # Log metrics
    metrics_logger.log_inference_run(
        execution_date=execution_date,
        data_stats=data_stats,
        signal_count=len(final_signals),
        filtered_signal_count=len(risk_adjusted),
        trade_count=len(execution_results['trades'])
    )
    
    logger.info("Metrics logged")


# Define DAG
with DAG(
    dag_id='inference_pipeline',
    default_args=default_args,
    description='Real-time inference and signal generation',
    schedule_interval='*/15 * * * *',  # Every 15 minutes
    catchup=False,
    max_active_runs=1,
    tags=['inference', 'real-time', 'trading'],
) as dag:
    
    # Start
    start = BashOperator(
        task_id='start',
        bash_command='echo "Starting inference pipeline"'
    )
    
    # Wait for fresh data
    wait_for_data = ExternalTaskSensor(
        task_id='wait_for_data',
        external_dag_id='data_ingestion',
        external_task_id='end',
        timeout=300,
        poke_interval=30,
        mode='reschedule',
    )
    
    # Fetch latest data
    fetch_data = PythonOperator(
        task_id='fetch_latest_data',
        python_callable=fetch_latest_data,
        provide_context=True,
    )
    
    # Run model inference in parallel
    transformer_inference = PythonOperator(
        task_id='run_transformer_inference',
        python_callable=run_transformer_inference,
        provide_context=True,
    )
    
    lstm_inference = PythonOperator(
        task_id='run_lstm_inference',
        python_callable=run_lstm_inference,
        provide_context=True,
    )
    
    xgboost_inference = PythonOperator(
        task_id='run_xgboost_inference',
        python_callable=run_xgboost_inference,
        provide_context=True,
    )
    
    rl_inference = PythonOperator(
        task_id='run_rl_inference',
        python_callable=run_rl_inference,
        provide_context=True,
    )
    
    # Ensemble signals
    ensemble = PythonOperator(
        task_id='ensemble_signals',
        python_callable=ensemble_signals,
        provide_context=True,
        trigger_rule='all_done',
    )
    
    # Risk management
    risk_mgmt = PythonOperator(
        task_id='apply_risk_management',
        python_callable=apply_risk_management,
        provide_context=True,
    )
    
    # Execute trades
    execute = PythonOperator(
        task_id='execute_trades',
        python_callable=execute_trades,
        provide_context=True,
    )
    
    # Update portfolio
    update_portfolio = PythonOperator(
        task_id='update_portfolio_state',
        python_callable=update_portfolio_state,
        provide_context=True,
    )
    
    # Log metrics
    log_metrics = PythonOperator(
        task_id='log_inference_metrics',
        python_callable=log_inference_metrics,
        provide_context=True,
    )
    
    # End
    end = BashOperator(
        task_id='end',
        bash_command='echo "Inference pipeline complete"'
    )
    
    # Define dependencies
    start >> wait_for_data >> fetch_data
    fetch_data >> [transformer_inference, lstm_inference, xgboost_inference, rl_inference]
    [transformer_inference, lstm_inference, xgboost_inference, rl_inference] >> ensemble
    ensemble >> risk_mgmt >> execute >> update_portfolio >> log_metrics >> end
